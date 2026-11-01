"""浮雕 3D 曲面的 GPU 渲染器（ModernGL / OpenGL）。

把一张高度场网格（顶点位置 + 顶点法线 + 逐顶点底色）一次性上传到显存，
每帧仅更新 MVP 矩阵与相机位置 uniform，由 GPU 做深度测试 + 片元 Phong 光照。
复杂度从“逐像素 Python 循环”降到“一次 draw call”，旋转/翻面丝滑无卡顿。

着色与 :mod:`core.relief.mesh3d` 的 ``colorFaces`` 等价，但改为：
- 逐顶点法线（相邻面法线平均）→ 片元插值出平滑法线（无网格纹）；
- 漫反射/高光在片元着色器逐像素计算（Phong），高光随视线实时变化；
底色（未打光）由 CPU 按显示模式/材质算一次（随模型变化，不随相机变化），
因此旋转时无需重建顶点缓冲，只重传 MVP。
"""
from __future__ import annotations

import math

import numpy as np
import moderngl

from ._renderdbg import dbg

VERT = """#version 330 core
in vec3 a_pos;
in vec3 a_normal;
in vec3 a_base;
uniform mat4 u_mvp;
out vec3 v_base;
out vec3 v_normal;
out vec3 v_world;
void main() {
  v_world = a_pos;
  v_normal = a_normal;
  v_base = a_base;
  gl_Position = u_mvp * vec4(a_pos, 1.0);
}
"""

FRAG = """#version 330 core
in vec3 v_base;
in vec3 v_normal;
in vec3 v_world;
uniform vec3 u_eye;
uniform vec3 u_light;
uniform int u_mode;        // 0=map 1=color 2=clay 3=wood 4=stone 5=plastic 6=metal
uniform float u_ambient;
uniform float u_kd;
uniform float u_add_amb;
uniform float u_spec_gain;
uniform float u_spec_pow;
uniform float u_opacity;
// 参数驱动 PBR（u_mode==8）：与 mesh3d._pbrShade 严格一致
uniform float u_metallic;
uniform float u_f0d;
uniform float u_rough;
uniform float u_sss;
uniform float u_trans;
uniform vec3 u_emis;
uniform int u_quality;        // 0=粗糙(basic) 1=精致(fine, IBL+GGX+ACES)
uniform int u_tex_id;         // 纹理图案（0 clay/1 wood/2 plastic/3 stone/4 metal）
uniform float u_tex_scale;    // 纹理尺度 mm
uniform float u_tex_amount;   // 纹理强度（兼 bump 幅度）
// HDRI 环境贴图 IBL（u_env_on=1 时取代程序化环境）：等距柱状、带 mipmap
uniform sampler2D u_env;
uniform int u_env_on;
uniform float u_env_mips;     // 最大 mip 层（= log2(min(W,H))）
out vec4 frag;
// 程序化摄影棚环境（与 mesh3d._envSample 严格一致）：渐变天空/地面 + 4 耚 HDR 柔光箱
vec3 envSample(vec3 d){
  vec3 zen = vec3(0.36, 0.40, 0.48), hor = vec3(0.60, 0.60, 0.62), grd = vec3(0.10, 0.09, 0.08);
  float t = pow(clamp(d.z, 0.0, 1.0), 0.6);
  float gt = pow(clamp(-d.z, 0.0, 1.0), 0.5);
  vec3 base = mix(mix(hor, zen, t), grd, gt);
  vec3 v1 = d - normalize(vec3(0.45, 0.30, 0.84));
  vec3 vs = d - normalize(vec3(0.50, -0.35, 0.72));
  vec3 v2 = d - normalize(vec3(-0.72, 0.20, 0.62));
  vec3 v3 = d - normalize(vec3(0.10, -0.85, 0.40));
  base += vec3(3.00, 2.95, 2.85) * exp(-dot(v1, v1) / (2.0 * 0.28 * 0.28));
  base += vec3(7.00, 6.80, 6.30) * exp(-dot(vs, vs) / (2.0 * 0.07 * 0.07));
  base += vec3(0.45, 0.65, 1.05) * exp(-dot(v2, v2) / (2.0 * 0.55 * 0.55));
  base += vec3(1.30, 1.00, 0.68) * exp(-dot(v3, v3) / (2.0 * 0.45 * 0.45));
  return base;
}
// 按方向采样 HDRI 环境（等距柱状 uv）；lod 选 mip 层→粗糙模糊（IBL 预过滤近似）
vec3 envTex(vec3 d, float lod){
  float u = atan(d.y, d.x) * 0.15915494 + 0.5;        // 1/(2π)
  float v = acos(clamp(d.z, -1.0, 1.0)) * 0.31830989; // 1/π
  return textureLod(u_env, vec2(u, v), lod).rgb;
}
// ACES 近似色调映射（与 mesh3d._acesFilmic 一致）
vec3 aces(vec3 x){
  float a = 2.51, b = 0.03, c = 2.43, d = 0.59, e = 0.14;
  return clamp((x * (a * x + b)) / (x * (c * x + d) + e), 0.0, 1.0);
}
// 值噪声 + fBm（有机、不重复）；GPU 逐像素 bump 用（高频细节）
float h21(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123); }
float vnoise(vec2 p){
  vec2 i = floor(p), f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(h21(i), h21(i + vec2(1.0, 0.0)), u.x),
             mix(h21(i + vec2(0.0, 1.0)), h21(i + vec2(1.0, 1.0)), u.x), u.y);
}
float fbm(vec2 p){
  float t = 0.0, a = 0.5, n = 0.0;
  for (int k = 0; k < 4; k++){ t += a * vnoise(p); n += a; p *= 2.0; a *= 0.5; }
  return t / n;
}
// 程序化表面纹理高度 h≈[-1,1]（fBm 有机噪声，取代规则正弦）；q 已按 tex_scale 归一
float matHeight(int id, vec2 q){
  if (id == 1) return clamp(sin(q.x * 6.2831853 + 3.0 * fbm(q * 0.6)), -1.0, 1.0); // wood 不规则年轮
  if (id == 2) return (fbm(q * 3.0) - 0.5) * 1.4;                                   // plastic 橘皮
  if (id == 3) return (fbm(q * 2.2) - 0.5) * 2.0;                                   // stone 颗粒
  if (id == 4) return (fbm(vec2(q.x * 0.8, q.y * 9.0)) - 0.5) * 2.0;                // metal 拉丝
  return (fbm(q * 2.0) - 0.5) * 2.0;                                                // clay 斑驳
}
// 有限差分求高度场梯度，扰动法线（浅浮雕切平面≈XY，直接用世界 xy 梯度）
vec3 bumpNormal(vec3 N, vec3 wp){
  vec2 uv = wp.xy / max(u_tex_scale, 0.5);
  float e = 0.02;
  float h0 = matHeight(u_tex_id, uv);
  vec2 g = vec2(matHeight(u_tex_id, uv + vec2(e, 0.0)) - h0,
                matHeight(u_tex_id, uv + vec2(0.0, e)) - h0) / e;
  g = clamp(g, vec2(-8.0), vec2(8.0));
  return normalize(N - u_tex_amount * 0.06 * vec3(g.x, g.y, 0.0));
}
void main() {
  vec3 N = normalize(v_normal);
  if (u_mode == 8 && u_tex_amount > 0.0) N = bumpNormal(N, v_world);   // 表面纹理凹凸
  vec3 L = normalize(u_light);
  vec3 V = normalize(u_eye - v_world);
  vec3 Hh = normalize(L + V);
  float diff = max(dot(N, L), 0.0);
  float diff2 = max(dot(N, -L), 0.0);         // 反方向填充光：照亮背光面
  float head = max(dot(N, V), 0.0);          // 头灯：光随视线，正面恒亮
  float sp = max(dot(N, Hh), 0.0);
  float spec = u_spec_gain * pow(sp, u_spec_pow);
  // 主光与头灯取亮者，填充光照亮侧面，再抬高下限消除死黑
  float d = max(diff, head * 0.90);
  float soft = 0.45 + 0.45 * d + 0.20 * diff2;
  vec3 base = v_base;
  vec3 col;
  if (u_mode == 8) {                         // PBR 材质：环境IBL(按反射向量) + GGX + ACES
    float metallic = u_metallic;
    float rough = clamp(u_rough, 0.03, 1.0);
    float f0d = min(u_f0d, 1.0);
    vec3 F0 = mix(vec3(f0d), base, metallic);
    vec3 diffAlb = base * (1.0 - metallic) * (1.0 - u_trans);
    vec3 keyC = vec3(1.00, 0.98, 0.95) * 1.70;
    float nl = max(dot(N, L), 0.0);
    float nv = clamp(dot(N, V), 0.0001, 1.0);
    vec3 fr = F0 + (1.0 - F0) * pow(1.0 - nv, 5.0);
    if (u_quality == 1) {                     // 精致：环境 IBL(按反射向量) + GGX + ACES
      vec3 envN, envR;
      vec3 R = reflect(-V, N);
      if (u_env_on == 1) {                     // 真实 HDRI：漫反射取最模糊 mip、镜面按粗糙选 mip
        envN = envTex(N, u_env_mips);
        envR = envTex(R, rough * u_env_mips);
      } else {                                 // 内置程序化摄影棚环境
        envN = envSample(N);
        envR = mix(envSample(R), envN, rough);
      }
      float a = rough * rough; float a2 = a * a;
      float NoH = max(sp, 0.0);
      float den = NoH * NoH * (a2 - 1.0) + 1.0;
      float D = a2 / (3.14159265 * den * den + 1e-7);
      float k = a * 0.5;
      float G = (nv / (nv * (1.0 - k) + k)) * (nl / (nl * (1.0 - k) + k));
      float VoH = max(dot(V, Hh), 0.0);
      vec3 Fdir = F0 + (1.0 - F0) * pow(1.0 - VoH, 5.0);
      float ggx = (D * G) / (4.0 * nv * nl + 1e-5);
      vec3 specDir = Fdir * ggx * keyC * nl;
      vec3 diff = diffAlb * (keyC * (nl * 0.5) + envN * 0.20);
      vec3 specIbl = fr * envR * (1.0 - rough) * 1.35;
      col = diff + specDir + specIbl;
      col += base * envN * (u_sss * 0.4);
      col += u_emis;
      col = aces(col * 1.05);
    } else {                                  // 粗糙：半球环境光 + Blinn 高光（无逐像素采样/ACES）
      float hemi = 0.5 + 0.5 * N.z;
      vec3 zen = vec3(0.36, 0.40, 0.48), grd = vec3(0.10, 0.09, 0.08);
      vec3 ambient = mix(grd, zen, hemi) * 0.55;
      float shin = clamp(2.0 / (rough * rough) - 2.0, 2.0, 1200.0);
      vec3 keySpec = fr * pow(sp, shin) * keyC * (0.3 + 0.7 * (1.0 - rough)) * nl;
      col = diffAlb * (keyC * nl + ambient) + keySpec + fr * ambient * 0.6;
      col += base * ambient * (u_sss * 0.4);
      col += u_emis;
    }
  } else if (u_mode == 5) {                       // plastic: 显式环境 + 漫反射
    col = base * (u_add_amb + u_kd * soft) + vec3(spec);
  } else if (u_mode == 6) {                // metal: + 边缘菲涅尔
    float fres = pow(1.0 - clamp(N.z, 0.0, 1.0), 2.0);
    col = base * (u_add_amb + u_kd * soft) + base * 0.5 * fres + vec3(spec);
  } else if (u_mode == 7) {                 // brep: 实体着色（明亮清晰）
    // 主光 + 反方向填充光，抬高环境下限：侧面不再死黑、明快清晰，
    // 受光面保持亮部对比（真实感）——与 CPU 光栅化路径
    // （_paintBrepZBuffer）公式一致；轻微高光提升质感
    float lit = 0.36 + 0.68 * diff + 0.22 * diff2;
    col = base * lit + vec3(spec);
  } else {                                 // map/color/clay/wood/stone: 基础明暗 lit
    float lit = u_ambient + (1.0 - u_ambient) * soft;
    col = base * lit + vec3(spec);
  }
  col = clamp(col, 0.0, 1.0);
  frag = vec4(col, u_opacity);
}
"""


EDGE_VERT = """#version 330 core
in vec3 a_pos;
uniform mat4 u_mvp;
void main() {
  gl_Position = u_mvp * vec4(a_pos, 1.0);
}
"""

EDGE_FRAG = """#version 330 core
uniform vec4 u_color;
out vec4 frag;
void main() {
  frag = u_color;
}
"""


# 路径折线着色器：逐顶点颜色（进给/快速/已走分段统一），每帧只更新 MVP。
PATH_VERT = """#version 330 core
in vec3 a_pos;
in vec4 a_color;
uniform mat4 u_mvp;
out vec4 v_color;
void main() {
  v_color = a_color;
  gl_Position = u_mvp * vec4(a_pos, 1.0);
}
"""

PATH_FRAG = """#version 330 core
in vec4 v_color;
out vec4 frag;
void main() {
  frag = v_color;
}
"""

# 屏幕空间线段（浮雕模式密集曲线攒批）：顶点只有 2f 坐标，颜色走 uniform。
# 相比逐顶点 3f+4f 的 PATH_VERT，每顶点上传字节从 28 降到 8（18 万点的
# 描图曲线从 12MB/帧降到 3.4MB/帧），且同色曲线合成单次 draw call。
LINE_VERT = """#version 330 core
in vec2 a_pos;
uniform mat4 u_mvp;
void main() {
  gl_Position = u_mvp * vec4(a_pos, 0.0, 1.0);
}
"""

LINE_FRAG = """#version 330 core
uniform vec4 u_color;
out vec4 frag;
void main() {
  frag = u_color;
}
"""


def _dash_segments(pts, dash):
    """沿折线按 dash=(on_len, off_len) 拆出实线段列表（GL_LINES 用）。

    快速路径在 GPU 上无法用固定管线虚线，CPU 一次性拆成 on/off 交替的
    实线段，上传一次后每帧零计算。
    """
    if len(pts) < 2:
        return []
    on_len, off_len = dash
    out = []
    drawing = True
    seg_start = pts[0]
    seg_len = 0.0
    total = on_len + off_len if on_len + off_len > 0 else on_len
    for a, b in zip(pts[:-1], pts[1:]):
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        dz = (b[2] - a[2]) if len(a) > 2 and len(b) > 2 else 0.0
        L = math.sqrt(dx * dx + dy * dy + dz * dz)
        if L <= 1e-9:
            continue
        ux, uy, uz = dx / L, dy / L, dz / L
        remain = L
        cx, cy, cz = a[0], a[1], (a[2] if len(a) > 2 else 0.0)
        while remain > 1e-9:
            if drawing:
                take = min(remain, max(on_len - seg_len, 0.0) or on_len)
            else:
                take = min(remain, max(off_len - seg_len, 0.0) or off_len)
            take = max(take, 1e-9)
            nx, ny, nz = cx + ux * take, cy + uy * take, cz + uz * take
            if drawing:
                out.append(((cx, cy, cz), (nx, ny, nz)))
            seg_len += take
            if seg_len >= (on_len if drawing else off_len) - 1e-9:
                drawing = not drawing
                seg_len = 0.0
            cx, cy, cz = nx, ny, nz
            remain -= take
    return out


# ---------------- 矩阵（标准数学约定，行主序） ----------------
def look_at(eye, target, up) -> np.ndarray:
    """世界 → 视空间 4x4（行主序）。与 viewport 的 _project 投影一致。"""
    eye = np.asarray(eye, dtype=np.float64)
    f = np.asarray(target, dtype=np.float64) - eye
    f /= np.linalg.norm(f)
    r = np.cross(f, up)
    r /= np.linalg.norm(r)
    u = np.cross(r, f)
    m = np.eye(4, dtype=np.float64)
    m[0, :3] = r
    m[1, :3] = u
    m[2, :3] = -f
    m[:3, 3] = -(m[:3, :3] @ eye)
    return m


def view_from_basis(eye, right, up, fwd) -> np.ndarray:
    """直接用视口相机的 (right, up, fwd) 基构造视矩阵（与 _project 严格一致）。

    look_at 用 cross(fwd,up) 推导 right；但视口正交前视(mode 2)把 right 硬编码为
    (1,0,0)，而 cross(fwd,up)=(-1,0,0)，两者 X 轴相反，会令 GPU 曲面与前视叠加层
    镜像错位。故直接用相机基向量，避免推导。
    """
    eye = np.asarray(eye, dtype=np.float64)
    right = np.asarray(right, dtype=np.float64)
    up = np.asarray(up, dtype=np.float64)
    fwd = np.asarray(fwd, dtype=np.float64)
    m = np.eye(4, dtype=np.float64)
    m[0, :3] = right
    m[1, :3] = up
    m[2, :3] = -fwd
    m[:3, 3] = -(m[:3, :3] @ eye)
    return m


def perspective(fovy: float, aspect: float, near: float, far: float) -> np.ndarray:
    """标准 OpenGL 透视投影（行主序），与 _project 的 focal 映射一致。"""
    f = 1.0 / math.tan(fovy / 2.0)
    m = np.zeros((4, 4), dtype=np.float64)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = (2.0 * far * near) / (near - far)
    m[3, 2] = -1.0
    return m


def ortho_proj(scale: float, w: float, h: float, near: float, far: float) -> np.ndarray:
    """对称正交投影（行主序），映射 world→像素，与 _orthoScale 一致。"""
    m = np.zeros((4, 4), dtype=np.float64)
    m[0, 0] = 2.0 * scale / w
    m[1, 1] = 2.0 * scale / h
    m[2, 2] = -2.0 / (far - near)
    m[2, 3] = -(far + near) / (far - near)
    m[3, 3] = 1.0
    return m



def _base_arr(base, n: int):
    """顶点基础色 (n,3) float32 0-1：支持单一 (r,g,b)（平铺）或逐顶点
    (n,3) 数组（实体模拟的坯料双色：未切面坯料色、切过的面金属色）。"""
    a = np.asarray(base, dtype="f4")
    if a.ndim == 2 and a.shape == (n, 3):
        return np.ascontiguousarray(a / 255.0)
    return np.ascontiguousarray(np.tile(a / 255.0, (n, 1)))



# ------------------------------------------------------------------ #
# 实体模拟坯料：高度场纹理位移渲染（CAM 仿真专业方案）
# ------------------------------------------------------------------ #
# 高度场只作为 R32F 纹理上传（200×160 = 128KB），网格几何静态复用；
# 法线、切削/未切配色、深度阴影、镜面高光全部在片元着色器逐像素计算。
# 纹理双线性插值让曲面连续平滑，无三角网格的格子台阶；每 tick 只更新
# 纹理数据，不再重建/上传几十万顶点。
STOCK_VERT = """#version 330 core
in vec2 a_uv;              // 网格参数 (u,v)∈[0,1]²
in float a_kind;           // 0=顶面 1=裙边底(z=0) 2=裙边顶(z=h)
in vec3 a_nrm;             // 裙边固定法线；顶面顶点为 0（片元算）
uniform mat4 u_mvp;
uniform sampler2D u_h;     // (ny,nx) R32F：材料顶面 z（毛坯局部系）
uniform vec2 u_origin;     // 左下角世界 xy
uniform vec2 u_size;       // (宽, 高) 世界尺度
uniform float u_zoff;      // 世界 z = 局部 z - u_zoff（居中）
out vec2 v_uv;
out float v_kind;
out vec3 v_wnrm;
out vec3 v_wpos;
void main() {
  float h = texture(u_h, a_uv).r;
  float z = (a_kind > 0.5 && a_kind < 1.5) ? 0.0 : h;
  vec3 p = vec3(u_origin.x + a_uv.x * u_size.x,
                u_origin.y + a_uv.y * u_size.y, z - u_zoff);
  v_uv = a_uv;
  v_kind = a_kind;
  v_wnrm = a_nrm;
  v_wpos = p;
  gl_Position = u_mvp * vec4(p, 1.0);
}
"""

STOCK_FRAG = """#version 330 core
in vec2 v_uv;
in float v_kind;
in vec3 v_wnrm;
in vec3 v_wpos;
uniform sampler2D u_h;
uniform vec2 u_step;       // uv 步进一格
uniform vec2 u_res;        // (res_x, res_y) 世界尺度
uniform vec3 u_eye;
uniform vec3 u_light;
uniform float u_ztop;      // 未切顶面世界 z
uniform float u_thick;
uniform float u_resz;
out vec4 frag;
void main() {
  vec3 N;
  if (v_kind > 0.5) {
    N = normalize(v_wnrm);                  // 外裙边：固定法线
  } else {
    // 高度场中央差分法线（双线性插值 → 连续平滑曲面）
    float hC = texture(u_h, v_uv).r;
    float hX = texture(u_h, v_uv + vec2(u_step.x, 0.0)).r;
    float hY = texture(u_h, v_uv + vec2(0.0, u_step.y)).r;
    N = normalize(vec3((hC - hX) / u_res.x, (hC - hY) / u_res.y, 1.0));
  }
  float cutD = u_ztop - v_wpos.z;           // 切深（世界系）
  float t = clamp(cutD / u_thick, 0.0, 1.0);
  vec3 stockC = vec3(0.376, 0.431, 0.518);  // 未切坯料：石板蓝
  vec3 floorC = vec3(0.871, 0.886, 0.910);  // 切削底面：银白
  vec3 wallC  = vec3(0.635, 0.620, 0.580);  // 腔壁：暖灰
  vec3 base = stockC;
  if (v_kind > 0.5) {
    base = wallC * (1.0 - 0.30 * t);        // 外壁深度渐暗
  } else if (cutD > 0.5 * u_resz) {
    // 陡坡（法线接近水平）按腔壁色，平缓底面按银白——连续过渡
    base = mix(wallC, floorC, smoothstep(0.55, 0.85, N.z));
    base *= 1.0 - 0.45 * t;                 // 深度阴影（近似 AO）
  }
  vec3 L = normalize(u_light);
  vec3 V = normalize(u_eye - v_wpos);
  vec3 Hh = normalize(L + V);
  float diff = max(dot(N, L), 0.0);
  float diff2 = max(dot(N, -L), 0.0);       // 反向填充光
  float head = max(dot(N, V), 0.0);         // 头灯
  float sp = max(dot(N, Hh), 0.0);
  float spec = 0.55 * pow(sp, 22.0);        // 金属镜面高光
  float d = max(diff, head * 0.90);
  float soft = 0.45 + 0.45 * d + 0.20 * diff2;
  float lit = 0.36 + (1.0 - 0.36) * soft;
  vec3 col = base * lit + vec3(spec);
  frag = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""

class GLSurface:
    """GPU 浮雕曲面渲染器（ModernGL）。

    同一网格只在 key 变化时重建顶点缓冲；旋转期间只重传 MVP/相机 uniform。
    """

    def __init__(self) -> None:
        self.ctx: moderngl.Context | None = None
        self.prog: moderngl.Program | None = None
        self.vao: moderngl.VertexArray | None = None
        self._pos = self._nrm = self._base = self._idx = None
        self._idxSrc = None
        # 上次上传的源数组身份（纯相机帧跳过整份重传的判定，见 draw）
        self._posSrc = self._nrmSrc = self._baseSrc = None
        self._roiPartial = False
        self._key = None
        # B-Rep 边线着色器（GL_LINES）
        self._edgeProg = None
        # 路径折线（GL_LINES，逐顶点颜色）——key -> (vao, buffer, 顶点数, lines 引用)
        self._pathProg = None
        # 屏幕空间线段（浮雕密集曲线攒批，2f 坐标 + uniform 颜色）
        self._lineProg = None
        # 常驻 VBO（跨帧复用，容量不足才重建；VAO 每帧新建，见 draw_screen_lines）
        self._lineBuf = None
        self._path_cache = {}
        self._stockProg = None
        self._stockTexs = {}
        self._stockGrids = {}
        # B-Rep 深度回读 FBO（隐藏线消除用）
        self._bzFbo = None
        self._bzDepth = None
        self._bzW = self._bzH = 0
        # HDRI 环境纹理（IBL）：_envData 为待上传 float32 (H,W,3) 或 None（程序化环境）
        self._envTex: moderngl.Texture | None = None
        self._envData = None
        self._envDirty = False
        self._envMips = 0.0

    def set_env(self, rgb) -> None:
        """设置/清除 HDRI 环境纹理（(H,W,3) float32 或 None）。延到 draw 时重建。"""
        self._envData = rgb
        self._envDirty = True

    def _rebuild_env(self) -> None:
        ctx = self.ctx
        if self._envTex is not None:
            self._envTex.release()
            self._envTex = None
            self._envMips = 0.0
        rgb = self._envData
        if rgb is None:
            return
        h, w = int(rgb.shape[0]), int(rgb.shape[1])
        tex = ctx.texture((w, h), 3, np.ascontiguousarray(rgb, dtype="f4").tobytes(),
                          dtype="f4")
        try:
            tex.build_mipmaps(base=0)
        except Exception:
            pass
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR_MIPMAP_LINEAR)
        tex.repeat_x = True            # 经度环绕（接缝）
        tex.repeat_y = False           # 纬度夹紧（不环绕极点）
        self._envTex = tex
        self._envMips = float(max(1, int(math.floor(math.log2(min(w, h))))))

    def _asUpload(self, arr: np.ndarray, dtype) -> np.ndarray:
        """转成可上传的连续 dtype 数组。

        绝不缓存：曾有版本用 ``id(arr)`` 做转换缓存键——id 在对象释放后会被
        新数组复用，笔画期顶点/法线/颜色临时数组高频生死，撞键时会返回
        上一网格的错误缓冲（法线被当颜色画出——整屏蓝底彩虹条纹的"闪"）。
        调用方已在缓存层把数组预转成 f4/u4 连续格式，此处通常零拷贝直通。
        """
        return np.ascontiguousarray(arr, dtype=dtype)

    def ensure(self) -> moderngl.Context:
        if self.ctx is None:
            # 绑定当前已激活的 Qt GL 上下文（QOpenGLWidget.paintGL 中）
            self.ctx = moderngl.create_context()
            self.prog = self.ctx.program(vertex_shader=VERT, fragment_shader=FRAG)
            self._edgeProg = self.ctx.program(vertex_shader=EDGE_VERT,
                                              fragment_shader=EDGE_FRAG)
            dbg(f"GLSurface.ensure: created ctx id={id(self.ctx)} "
                f"renderer={self.ctx.info.get('GL_RENDERER')}")
        return self.ctx

    def frame(self, W: int, H: int, bg, fbo: int = 0) -> None:
        """清屏（背景色 + 深度），为叠加层之外的纯清空帧准备。"""
        self.ensure()
        self.ctx.viewport = (0, 0, W, H)
        self.ctx.clear(color=bg, depth=1.0)

    @staticmethod
    def _writeRoi(buf, arr: np.ndarray, r0, r1, c0, c1, gnx) -> None:
        """把顶点块 ``[r0..r1] × [c0..c1]``（含端）按行连续写入 GPU 缓冲。

        顶点按行主序排列（每个 3×float32），同一行的 [c0..c1] 在内存里是
        一段连续数据 → 每行一次 write。167 行实测 0.12ms（整份 63MB 是 23ms）。
        """
        for r in range(r0, r1 + 1):
            a = r * gnx + c0
            b = r * gnx + c1 + 1
            seg = arr[a:b]
            buf.write(np.ascontiguousarray(seg, dtype="f4").tobytes(),
                      offset=a * 12)

    def draw(self, key, pos, nrm, base, idx, mvp, eye, light, params,
             W: int, H: int, bg, fbo: int = 0, opacity: float = 1.0,
             roi=None, force_upload: bool = False) -> None:
        """绘制浮雕曲面。

        ``opacity < 1`` 时开启混合（半透明 X-Ray：浮雕编辑态下让参考曲线透过
        模型清晰可见），否则按不透明渲染。
        ``roi`` 为 ``(r0, r1, c0, c1, gnx)`` 顶点块（含端）：笔画期只把这一块
        的 XYZ/法线/底色写进**已存在**的 GPU 缓冲，其余数据原地复用。
        ``force_upload``：调用方**就地改过**上次传进来的数组（缓存层原地刷新）
        时必须置真，强制整份重传，避免显示落后于数据。
        纯相机变化（缩放/旋转/平移）时数组对象与上次完全相同且没有 ROI——
        此时**跳过上传**：1024 显示网格整份 pos/nrm/base = 36MB/帧，PCIe
        拷贝是缩放卡顿的主要开销之一，而几何一个字节都没变。
        ``fbo`` 仅为兼容调用方签名保留：渲染目标是 Qt 当前绑定的 FBO，
        本方法不做绑定。
        """
        self.ensure()
        if self._envDirty:
            self._rebuild_env()
            self._envDirty = False
        ctx = self.ctx
        ctx.viewport = (0, 0, W, H)
        ctx.clear(color=bg, depth=1.0)
        ctx.enable(ctx.DEPTH_TEST)
        ctx.disable(ctx.CULL_FACE)
        if opacity < 0.999:
            ctx.enable(ctx.BLEND)
            # moderngl 的 blend_func 是只写属性：以元组赋值（读它会抛 NotImplementedError）
            ctx.blend_func = (ctx.SRC_ALPHA, ctx.ONE_MINUS_SRC_ALPHA)
        else:
            ctx.disable(ctx.BLEND)

        # VAO 仍每帧重建（本机 Qt(QOpenGLWidget)+ModernGL+MSAA 组合下，跨帧
        # 复用同一 VAO 时 render() 会访问违例闪退；"同帧新建再渲染"始终稳定）。
        # 但**缓冲本身**跨帧保留：此前每帧新建 4 个缓冲并整份上传（1024²
        # 网格 63MB，实测 23ms/帧）是拖动塑形卡顿的最大单项开销。笔画期
        # 顶点里只有 z 列与少量法线/底色真正变化 → 按 ROI 行块局部 write，
        # 上传量从 63MB 降到 KB 级（实测 100 行 0.12ms）。
        n_pos, n_nrm, n_base = pos.nbytes, nrm.nbytes, base.nbytes
        reuse = (self._pos is not None and self._nrm is not None
                 and self._base is not None
                 and self._pos.size == n_pos and self._nrm.size == n_nrm
                 and self._base.size == n_base)
        if not reuse:
            for _b in (self._pos, self._nrm, self._base):
                if _b is not None:
                    _b.release()
            self._pos = ctx.buffer(self._asUpload(pos, "f4"))
            self._nrm = ctx.buffer(self._asUpload(nrm, "f4"))
            self._base = ctx.buffer(self._asUpload(base, "f4"))
        # 源数组身份：缓存层（viewport._glCache）跨帧持有同一批数组，纯相机帧
        # 三者身份都不变；模型数据变化时要么换新数组、要么由调用方置
        # force_upload / 给 ROI。故"身份相同 + 无 ROI + 未强制"= 无需上传。
        same_src = (reuse and self._posSrc is pos and self._nrmSrc is nrm
                    and self._baseSrc is base)
        self._posSrc, self._nrmSrc, self._baseSrc = pos, nrm, base
        idx_new = (self._idx is None or self._idxSrc is not idx
                   or self._idx.size != idx.nbytes)
        if idx_new:
            if self._idx is not None:
                self._idx.release()
            self._idx = ctx.buffer(self._asUpload(idx, "u4"))
            self._idxSrc = idx
        if reuse:
            if not idx_new and roi is not None \
                    and roi[0] <= roi[1] and roi[2] <= roi[3]:
                r0, r1, c0, c1, gnx = roi
                self._writeRoi(self._pos, pos, r0, r1, c0, c1, gnx)
                self._writeRoi(self._nrm, nrm, r0, r1, c0, c1, gnx)
                self._writeRoi(self._base, base, r0, r1, c0, c1, gnx)
            elif force_upload or idx_new or not same_src:
                self._pos.write(self._asUpload(pos, "f4"))
                self._nrm.write(self._asUpload(nrm, "f4"))
                self._base.write(self._asUpload(base, "f4"))
        if self.vao is not None:
            self.vao.release()
            self.vao = None
        self.vao = ctx.vertex_array(
            self.prog,
            [
                (self._pos, "3f", "a_pos"),
                (self._nrm, "3f", "a_normal"),
                (self._base, "3f", "a_base"),
            ],
            self._idx,
        )
        self._key = key

        p = self.prog
        p["u_mvp"].write(np.ascontiguousarray(mvp.T, dtype="f4"))   # 行主序→列主序(float32)上传
        p["u_eye"].value = (float(eye[0]), float(eye[1]), float(eye[2]))
        p["u_light"].value = (float(light[0]), float(light[1]), float(light[2]))
        p["u_mode"].value = int(params["mode"])
        p["u_ambient"].value = float(params["ambient"])
        p["u_kd"].value = float(params["kd"])
        p["u_add_amb"].value = float(params["add_amb"])
        p["u_spec_gain"].value = float(params["spec_gain"])
        p["u_spec_pow"].value = float(params["spec_pow"])
        p["u_opacity"].value = float(opacity)
        # 参数驱动 PBR（mode==8）：其余 mode 不引用这些 uniform，设默认无害
        p["u_metallic"].value = float(params.get("metallic", 0.0))
        p["u_f0d"].value = float(params.get("f0d", 0.04))
        p["u_rough"].value = float(params.get("rough", 0.4))
        p["u_sss"].value = float(params.get("sss", 0.0))
        p["u_trans"].value = float(params.get("trans", 0.0))
        em = params.get("emis", (0.0, 0.0, 0.0))
        p["u_emis"].value = (float(em[0]), float(em[1]), float(em[2]))
        p["u_quality"].value = 1 if str(params.get("quality", "fine")) == "fine" else 0
        p["u_tex_id"].value = int(params.get("tex_id", 0))
        p["u_tex_scale"].value = float(params.get("tex_scale", 6.0))
        p["u_tex_amount"].value = float(params.get("tex_amount", 0.0))
        # HDRI 环境（仅 mode==8 精致档引用）：绑到单元 0；无图时走程序化环境
        if self._envTex is not None:
            self._envTex.use(0)
            p["u_env"].value = 0
            p["u_env_on"].value = 1
            p["u_env_mips"].value = float(self._envMips)
        else:
            p["u_env_on"].value = 0
        self.vao.render(moderngl.TRIANGLES)

    def draw_brep_edges(self, groups, mvp, W: int, H: int) -> None:
        """GPU 绘制 B-Rep 特征边（GL_LINES，深度测试按组控制）。

        groups: [(verts (N,3) float32 连续, (r,g,b,a) 0-1, depth_test), ...]
        depth_test=True 时走深度测试（实体模式：被面遮挡的背面线自动隐藏）；
        False 时全部显示（线框 / X-Ray 半透明）。画完关闭深度/混合，交还
        QPainter 叠加层（不清颜色、不清深度，由调用方统一清深度）。"""
        self.ensure()
        ctx = self.ctx
        if self._edgeProg is None:
            self._edgeProg = ctx.program(vertex_shader=EDGE_VERT,
                                         fragment_shader=EDGE_FRAG)
        ctx.viewport = (0, 0, W, H)
        p = self._edgeProg
        p["u_mvp"].write(np.ascontiguousarray(mvp.T, dtype="f4"))
        for verts, color, depth_test in groups:
            if depth_test:
                ctx.enable(ctx.DEPTH_TEST)
            else:
                ctx.disable(ctx.DEPTH_TEST)
            p["u_color"].value = (float(color[0]), float(color[1]),
                                  float(color[2]), float(color[3]))
            pb = ctx.buffer(np.ascontiguousarray(verts, dtype="f4"))
            vao = ctx.vertex_array(p, [(pb, "3f", "a_pos")])
            vao.render(moderngl.LINES)
            vao.release()
            pb.release()
        ctx.disable(ctx.DEPTH_TEST)
        ctx.disable(ctx.BLEND)

    def render_brep(self, ents, mvp, eye, light, W, H, bg,
                    ortho: bool, near: float = 0.1, far: float = 4000.0,
                    ambient: float = None, spec_gain: float = 0.10,
                    spec_pow: float = 32.0):
        """GPU 光栅化 B-Rep 实体到自有 FBO，回读 (RGBA bytes, 视距离图)。

        返回 (rgba_bytes, eye_depth (H,W) float32)；像素值 = 沿视线到最近
        表面的世界距离，无表面处 inf（隐藏线消除用）。主线程只做一次
        2MB 回读 + QImage 包装，比软件逐三角形光栅化快一个数量级。"""
        self.ensure()
        ctx = self.ctx
        if (self._bzW, self._bzH) != (W, H) or self._bzFbo is None:
            if self._bzFbo is not None:
                self._bzFbo.release()
            if self._bzDepth is not None:
                self._bzDepth.release()
            self._bzDepth = ctx.depth_texture((W, H))
            color = ctx.texture((W, H), 4)
            self._bzFbo = ctx.framebuffer(
                color_attachments=[color], depth_attachment=self._bzDepth)
            color.release()
            self._bzW, self._bzH = W, H
        fbo = self._bzFbo
        fbo.use()
        ctx.viewport = (0, 0, W, H)
        # 背景透明：叠加回 Qt 时不遮住先绘制的地面网格
        ctx.clear(red=bg[0], green=bg[1], blue=bg[2], alpha=0.0, depth=1.0)
        ctx.enable(ctx.DEPTH_TEST)
        ctx.disable(ctx.CULL_FACE)
        p = self.prog
        p["u_mvp"].write(np.ascontiguousarray(mvp.T, dtype="f4"))
        p["u_eye"].value = (float(eye[0]), float(eye[1]), float(eye[2]))
        p["u_light"].value = (float(light[0]), float(light[1]),
                              float(light[2]))
        p["u_mode"].value = 0
        p["u_ambient"].value = 0.30 if ambient is None else float(ambient)
        p["u_kd"].value = 1.0
        p["u_add_amb"].value = 0.0
        p["u_spec_gain"].value = float(spec_gain)
        p["u_spec_pow"].value = float(spec_pow)
        for item in ents:
            pos, nrm, base, alpha = item[0], item[1], item[2], item[3]
            pb = ctx.buffer(np.ascontiguousarray(pos, dtype="f4"))
            nb = ctx.buffer(np.ascontiguousarray(nrm, dtype="f4"))
            bb = ctx.buffer(np.ascontiguousarray(
                _base_arr(base, pos.shape[0])))
            vao = ctx.vertex_array(self.prog,
                                   [(pb, "3f", "a_pos"),
                                    (nb, "3f", "a_normal"),
                                    (bb, "3f", "a_base")])
            if alpha < 0.999:
                ctx.enable(ctx.BLEND)
                ctx.blend_func = (ctx.SRC_ALPHA, ctx.ONE_MINUS_SRC_ALPHA)
                p["u_opacity"].value = float(alpha)
            else:
                ctx.disable(ctx.BLEND)
                p["u_opacity"].value = 1.0
            vao.render(moderngl.TRIANGLES)
            vao.release()
            pb.release()
            nb.release()
            bb.release()
        ctx.disable(ctx.BLEND)
        rgba = fbo.read(components=4)
        raw = self._bzDepth.read()
        d = np.frombuffer(raw, dtype="f4").reshape(H, W).copy()
        # 关键：恢复 Qt 默认帧缓冲绑定——否则后续 QPainter 叠加层画进
        # 辅助 FBO（不可见），表现为花屏/模型丢失
        if ctx.screen is not None:
            ctx.screen.use()
        ndc = d * 2.0 - 1.0
        if ortho:
            eye_d = (ndc * (far - near) + (far + near)) * 0.5
        else:
            eye_d = 2.0 * near * far / ((far + near) - ndc * (far - near))
        eye_d[d >= 1.0] = np.inf
        return rgba, eye_d.astype(np.float32)

    def draw_brep(self, ents, mvp, eye, light, W, H,
                  spec_gain: float = 0.0, spec_pow: float = 16.0,
                  ambient: float = None) -> None:
        """绘制 B-Rep 实体表面（非索引三角形，逐对象缓冲，不清屏）。

        ents: [(pos (N,3) float, nrm (N,3) float, base (3,) 0-255, alpha)]。
        着色走 u_mode=7 强对比实体分支（侧面暗、受光面亮，好查看
        立体感），顶点法向为 OCC 曲面解析法向——面内平滑、面间硬边。"""
        self.ensure()
        ctx = self.ctx
        ctx.viewport = (0, 0, W, H)
        # 状态硬化：本方法在 QPainter.beginNativePainting 括号内调用，
        # QPainter 可能遗留 scissor / polygon mode / depth func / 写入掩码
        # 等 GL 状态（尤其 QOpenGLWidget 部分更新 + MSAA FBO），不清理会导致
        # 三角形被裁剪/只画点线/深度全拒——表现为"面消失只剩线条/杂点"。
        ctx.disable(0x0C11)  # GL_SCISSOR_TEST（moderngl 无命名常量，用枚举）
        ctx.disable(ctx.CULL_FACE)
        ctx.enable(ctx.DEPTH_TEST)
        ctx.depth_func = "<="
        ctx.color_mask = (True, True, True, True)
        ctx.depth_mask = True
        # 深度缓冲可能残留旧值（Qt 不一定每帧清深度位）：由调用方在
        # draw_brep 之前用原生 glClear(GL_DEPTH_BUFFER_BIT) 只清深度位。
        # 这里不能用 moderngl 的 ctx.clear(depth=...)：其 clear 参数带默认
        # 值，只传 depth 会把颜色一并清成黑色，覆盖 QPainter 刚画的背景。
        p = self.prog
        p["u_mvp"].write(np.ascontiguousarray(mvp.T, dtype="f4"))
        p["u_eye"].value = (float(eye[0]), float(eye[1]), float(eye[2]))
        p["u_light"].value = (float(light[0]), float(light[1]),
                              float(light[2]))
        p["u_mode"].value = 7      # BRep 专用实体着色（明亮清晰）
        p["u_ambient"].value = 0.30
        p["u_kd"].value = 1.0
        p["u_add_amb"].value = 0.0
        p["u_spec_gain"].value = 0.10
        p["u_spec_pow"].value = 32.0
        for item in ents:
            pos, nrm, base, alpha = item[0], item[1], item[2], item[3]
            pb = ctx.buffer(np.ascontiguousarray(pos, dtype="f4"))
            nb = ctx.buffer(np.ascontiguousarray(nrm, dtype="f4"))
            b = np.asarray(base, dtype="f4")
            if b.ndim == 1:
                # 单色 (3,) 0-255：复制到每个顶点
                b = np.tile(b / 255.0, (pos.shape[0], 1))
            else:
                # 逐顶点 (N,3) [0,1]（浮雕地形色带 / 实体着色因子色）：
                # 约定恒为 0-1，绝不按 max>1 猜测缩放——亮度因子×底色
                # 合法地超过 1.0 时会被误除 255 渲染成全黑（实测）
                b = np.clip(b, 0.0, 1.0)
            bb = ctx.buffer(np.ascontiguousarray(b))
            vao = ctx.vertex_array(self.prog,
                                   [(pb, "3f", "a_pos"),
                                    (nb, "3f", "a_normal"),
                                    (bb, "3f", "a_base")])
            if alpha < 0.999:
                ctx.enable(ctx.BLEND)
                ctx.blend_func = (ctx.SRC_ALPHA, ctx.ONE_MINUS_SRC_ALPHA)
                p["u_opacity"].value = float(alpha)
            else:
                ctx.disable(ctx.BLEND)
                p["u_opacity"].value = 1.0
            vao.render(moderngl.TRIANGLES)
            vao.release()
            pb.release()
            nb.release()
            bb.release()
        ctx.disable(ctx.BLEND)

    def depth_brep(self, ents, mvp, W, H, ortho: bool,
                   near: float = 0.1, far: float = 4000.0):
        """渲染 B-Rep 深度并回读为视距离图 (H, W) float32（隐藏线消除）。

        像素值 = 该像素沿视线到最近表面的世界距离；无表面处 = inf。
        """
        self.ensure()
        ctx = self.ctx
        if (self._bzW, self._bzH) != (W, H) or self._bzFbo is None:
            if self._bzFbo is not None:
                self._bzFbo.release()
            if self._bzDepth is not None:
                self._bzDepth.release()
            self._bzDepth = ctx.depth_texture((W, H))
            color = ctx.texture((W, H), 3)
            self._bzFbo = ctx.framebuffer(
                color_attachments=[color], depth_attachment=self._bzDepth)
            color.release()
            self._bzW, self._bzH = W, H
        fbo = self._bzFbo
        fbo.use()
        ctx.viewport = (0, 0, W, H)
        ctx.clear(depth=1.0)
        ctx.enable(ctx.DEPTH_TEST)
        ctx.disable(ctx.CULL_FACE)
        ctx.disable(ctx.BLEND)
        p = self.prog
        p["u_mvp"].write(np.ascontiguousarray(mvp.T, dtype="f4"))
        p["u_eye"].value = (0.0, 0.0, 0.0)
        p["u_light"].value = (0.0, 1.0, 0.0)
        p["u_mode"].value = 0
        p["u_ambient"].value = 1.0
        p["u_opacity"].value = 1.0
        for item in ents:
            pos, nrm, base, _alpha = item[0], item[1], item[2], item[3]
            pb = ctx.buffer(np.ascontiguousarray(pos, dtype="f4"))
            nb = ctx.buffer(np.ascontiguousarray(nrm, dtype="f4"))
            bb = ctx.buffer(np.ascontiguousarray(
                _base_arr(base, pos.shape[0])))
            vao = ctx.vertex_array(self.prog,
                                   [(pb, "3f", "a_pos"),
                                    (nb, "3f", "a_normal"),
                                    (bb, "3f", "a_base")])
            vao.render(moderngl.TRIANGLES)
            vao.release()
            pb.release()
            nb.release()
            bb.release()
        raw = self._bzDepth.read()
        d = np.frombuffer(raw, dtype="f4").reshape(H, W).copy()
        # 归还默认帧缓冲（Qt 后续叠加层仍绑默认 FBO）；standalone 上下文无 screen
        if ctx.screen is not None:
            ctx.screen.use()
        # GL 底左原点 → Qt 左上原点（与 render_brep 返回的视距图同约定，
        # 消费方 _filterHiddenLines/_drawObjects 用屏幕 y 直接查询）
        d = np.flipud(d)
        ndc = d * 2.0 - 1.0
        if ortho:
            eye_d = (ndc * (far - near) + (far + near)) * 0.5
        else:
            eye_d = 2.0 * near * far / ((far + near) - ndc * (far - near))
        eye_d[d >= 1.0] = np.inf
        return eye_d.astype(np.float32)

    def upload_paths(self, key, lines) -> None:
        """把一组路径折线预转为 GPU 顶点数组（Python 侧缓存）。

        lines: [(pts (N,3) 世界坐标, color (r,g,b,a) 0-1, dash 或 None)]。
        同 key 且 lines 引用不变时复用顶点数组，路径几何不变就零重建；
        顶点变换本身在 draw_paths 每帧由 GPU 完成。

        注：本机 Qt(QOpenGLWidget)+ModernGL+MSAA 组合下**跨帧复用同一 VAO
        会访问违例闪退**（见 render_brep 注释），因此这里只缓存顶点数组，
        draw_paths 每帧同帧新建 buffer/VAO 再渲染，规避该驱动问题。
        """
        cached = self._path_cache.get(key)
        if cached is not None and cached[1] is lines:
            return
        verts = []
        for pts, color, dash in lines:
            if len(pts) < 2:
                continue
            if dash:
                segs = _dash_segments(pts, dash)
            else:
                segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
            for a, b in segs:
                az = a[2] if len(a) > 2 else 0.0
                bz = b[2] if len(b) > 2 else 0.0
                verts.append((a[0], a[1], az, color[0], color[1], color[2], color[3]))
                verts.append((b[0], b[1], bz, color[0], color[1], color[2], color[3]))
        self._path_cache[key] = (
            np.ascontiguousarray(verts, dtype="f4") if verts else None, lines)


    # ---------------- 实体模拟坯料：高度场纹理位移渲染 ----------------

    def _stock_grid(self, nx: int, ny: int):
        """静态网格（numpy 缓存）：顶面规则网格 + 四周裙边条带。

        顶点属性 (a_uv, a_kind, a_nrm)：顶面 kind=0（法线片元算）；
        裙边 kind=1（底 z=0）/ 2（顶 z=h），法线固定朝外。
        """
        key = (nx, ny)
        hit = self._stockGrids.get(key)
        if hit is not None:
            return hit
        us = np.linspace(0.0, 1.0, nx, dtype=np.float32)
        vs = np.linspace(0.0, 1.0, ny, dtype=np.float32)
        verts = []       # (u, v, kind, nx, ny, nz)
        idx = []

        def _add(u, v, kind, n):
            verts.append((u, v, kind, n[0], n[1], n[2]))
            return len(verts) - 1

        # 顶面规则网格（顶点 id = i*nx + j）
        base = len(verts)
        for i in range(ny):
            for j in range(nx):
                verts.append((us[j], vs[i], 0.0, 0.0, 0.0, 0.0))
        for i in range(ny - 1):
            for j in range(nx - 1):
                a = base + i * nx + j
                b = a + 1
                c = a + nx + 1
                d = a + nx
                idx += [a, b, c, a, c, d]
        # 裙边：south (v=0, n=-Y) / north (v=1, +Y) / west (u=0, -X) / east (+X)
        for (axis, fix, n) in ((0, 0.0, (0.0, -1.0, 0.0)),
                               (0, 1.0, (0.0, 1.0, 0.0)),
                               (1, 0.0, (-1.0, 0.0, 0.0)),
                               (1, 1.0, (1.0, 0.0, 0.0))):
            k = nx if axis == 0 else ny
            prev_b = prev_t = -1
            for q in range(k):
                f = us[q] if axis == 0 else vs[q]
                uv0 = (f, fix) if axis == 0 else (fix, f)
                bt = _add(uv0[0], uv0[1], 1.0, n)   # 底（z=0）
                tp = _add(uv0[0], uv0[1], 2.0, n)   # 顶（z=h）
                if q > 0:
                    idx += [prev_b, prev_t, tp, prev_b, tp, bt]
                prev_b, prev_t = bt, tp
        va = np.ascontiguousarray(verts, dtype=np.float32)
        ia = np.ascontiguousarray(idx, dtype=np.uint32)
        self._stockGrids[key] = (va, ia)
        return va, ia

    def draw_stock(self, key, heights, x0, y0, wx, wy, z_off, z_top,
                   thick, resx, resy, resz, mvp, eye, light, W, H) -> None:
        """高度场坯料 GPU 渲染（实体模拟专用，见 STOCK_VERT/FRAG 注释）。

        heights: (ny,nx) float32 材料顶面高度（毛坯局部系 0..t）。
        网格/纹理按 (key,nx,ny) 缓存复用，每帧仅更新纹理数据 + 重建
        VAO（本机 Qt+ModernGL 跨帧复用 VAO 会闪退，与路径渲染同策略）。
        """
        self.ensure()
        ctx = self.ctx
        h = np.ascontiguousarray(heights, dtype="f4")
        ny, nx = h.shape
        if nx < 2 or ny < 2:
            return
        if self._stockProg is None:
            self._stockProg = ctx.program(vertex_shader=STOCK_VERT,
                                          fragment_shader=STOCK_FRAG)
        p = self._stockProg
        tex = self._stockTexs.get(key)
        if tex is not None and tex.size != (nx, ny):
            tex.release()
            tex = None
        if tex is None:
            tex = ctx.texture((nx, ny), 1, h, dtype="f4")
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = tex.repeat_y = False
            self._stockTexs[key] = tex
        else:
            tex.write(h)
        va, ia = self._stock_grid(nx, ny)
        # 状态硬化（同 draw_brep/draw_paths）
        ctx.viewport = (0, 0, W, H)
        ctx.disable(0x0C11)
        ctx.disable(ctx.CULL_FACE)
        ctx.enable(ctx.DEPTH_TEST)
        ctx.depth_func = "<="
        ctx.color_mask = (True, True, True, True)
        ctx.depth_mask = True
        ctx.disable(ctx.BLEND)
        p["u_mvp"].write(np.ascontiguousarray(mvp.T, dtype="f4"))
        p["u_origin"].value = (float(x0), float(y0))
        p["u_size"].value = (float(wx), float(wy))
        p["u_zoff"].value = float(z_off)
        p["u_ztop"].value = float(z_top)
        p["u_thick"].value = float(thick)
        p["u_res"].value = (float(resx), float(resy))
        p["u_resz"].value = float(resz)
        p["u_step"].value = (1.0 / (nx - 1), 1.0 / (ny - 1))
        p["u_eye"].value = (float(eye[0]), float(eye[1]), float(eye[2]))
        p["u_light"].value = (float(light[0]), float(light[1]),
                              float(light[2]))
        # 纹理绑定到采样单元（u_h=0）：前面 draw_brep/depth_brep 可能把
        # 别的纹理留在 0 号单元，不显式绑定会采到空数据——坯料高度全错
        tex.use(0)
        p["u_h"].value = 0
        vb = ctx.buffer(va)
        ib = ctx.buffer(ia)
        vao = ctx.vertex_array(p, [(vb, "2f 1f 3f", "a_uv", "a_kind", "a_nrm")],
                               ib)
        try:
            vao.render(moderngl.TRIANGLES)
        finally:
            vao.release()
            vb.release()
            ib.release()

    def draw_paths(self, key, mvp, W: int, H: int,
                   count: int = None) -> None:
        """绘制路径：每帧从缓存取顶点数组，新建 buffer/VAO 渲染一次。

        count：只渲染前 count 个顶点（None=全部）。顶点按线段对连续排布，
        画前 2*i 个顶点 = 画前 i 段——模拟"已走路径"高亮只需每帧改 count，
        顶点数据零重建。
        """
        cached = self._path_cache.get(key)
        if cached is None or cached[0] is None:
            return
        self.ensure()
        ctx = self.ctx
        data = cached[0]
        ctx.viewport = (0, 0, W, H)
        if self._pathProg is None:
            self._pathProg = ctx.program(vertex_shader=PATH_VERT,
                                         fragment_shader=PATH_FRAG)
        p = self._pathProg
        p["u_mvp"].write(np.ascontiguousarray(mvp.T, dtype="f4"))
        # 状态硬化（同 draw_brep）：QPainter 可能遗留 scissor / 面剔除 /
        # 深度状态，不清会把路径裁剪掉——表现为旋转视图后路径消失。
        ctx.disable(0x0C11)   # GL_SCISSOR_TEST
        ctx.disable(ctx.CULL_FACE)
        ctx.disable(ctx.DEPTH_TEST)
        ctx.depth_func = "<="
        ctx.enable(ctx.BLEND)
        ctx.blend_func = (ctx.SRC_ALPHA, ctx.ONE_MINUS_SRC_ALPHA)
        pb = ctx.buffer(data)
        vao = ctx.vertex_array(self._pathProg, [(pb, "3f 4f", "a_pos", "a_color")])
        try:
            if count is not None:
                vao.render(moderngl.LINES, vertices=int(count))
            else:
                vao.render(moderngl.LINES)
        finally:
            vao.release()
            pb.release()
        ctx.disable(ctx.BLEND)

    def draw_screen_polyline(self, pts, mvp, W: int, H: int,
                             color=(1.0, 0.6, 0.0, 1.0),
                             width: float = 1.0,
                             z=None, depth_test: bool = False) -> None:
        """屏幕空间闭合折线（浮雕外轮廓），点已是像素坐标，逐帧重建顶点。

        为什么不用 QPainter：Qt 光栅引擎画**抗锯齿宽折线**是"按路径包围盒
        离屏光栅化再合成"，一条满屏边框就要 6~9ms（2560×1600 实测 6.4ms，
        4K 更高），是浮雕模式帧成本第一大项；GL 画线走 MSAA，同样宽度
        实测 <0.2ms。顶点只有几千个（269KB），每帧重建上传可忽略。

        ``pts``: (N,2) 逻辑像素坐标（与视口 w/h 同一坐标系，自动闭合）；
        ``mvp``: 行主序 4×4，把像素坐标直接映射到 NDC（见调用方）。
        ``z``: (N,) 逐点 NDC 深度（``mvp`` 第三行须透传 z）；缺省 0。
        ``depth_test``: 真时与已渲染曲面做深度测试（外轮廓被模型自身遮挡，
        侧视时背面边框不再"穿模"）；假时无条件置顶（叠加层语义）。
        """
        p = np.asarray(pts, dtype=np.float32)
        if p.ndim != 2 or p.shape[0] < 3:
            return
        n = p.shape[0]
        verts = np.empty((2 * n, 7), dtype="f4")
        verts[0::2, :2] = p
        verts[1::2, :2] = np.roll(p, -1, axis=0)   # 闭合：末点 → 首点
        if z is None:
            verts[:, 2] = 0.0
        else:
            zv = np.asarray(z, dtype=np.float32).reshape(-1)
            if zv.size != n:
                return
            verts[0::2, 2] = zv
            verts[1::2, 2] = np.roll(zv, -1)
        verts[:, 3:] = np.asarray(color, dtype=np.float32)
        self.ensure()
        ctx = self.ctx
        ctx.viewport = (0, 0, W, H)
        if self._pathProg is None:
            self._pathProg = ctx.program(vertex_shader=PATH_VERT,
                                         fragment_shader=PATH_FRAG)
        prog = self._pathProg
        prog["u_mvp"].write(
            np.ascontiguousarray(np.asarray(mvp, dtype=np.float64).T,
                                 dtype="f4"))
        # 状态硬化（同 draw_paths/draw_brep）：QPainter 可能遗留 scissor /
        # 面剔除 / 深度测试，不清会把线裁掉或挡在曲面后面。
        ctx.disable(0x0C11)   # GL_SCISSOR_TEST
        ctx.disable(ctx.CULL_FACE)
        if depth_test:
            # 与曲面同一深度比较函数（曲面写入用 "<="）：边框落在模型边界上，
            # 只有更近或等深才可见——背面边框被模型自身遮住。
            ctx.enable(ctx.DEPTH_TEST)
            ctx.depth_func = "<="
            ctx.depth_mask = False
        else:
            ctx.disable(ctx.DEPTH_TEST)
        ctx.enable(ctx.BLEND)
        ctx.blend_func = (ctx.SRC_ALPHA, ctx.ONE_MINUS_SRC_ALPHA)
        try:
            ctx.line_width = float(width)
        except Exception:
            pass          # 驱动只支持 1px：退回细线，不影响正确性
        pb = ctx.buffer(verts.tobytes())
        vao = ctx.vertex_array(prog, [(pb, "3f 4f", "a_pos", "a_color")])
        try:
            vao.render(moderngl.LINES)
        finally:
            vao.release()
            pb.release()
            ctx.disable(ctx.BLEND)

    def draw_screen_lines(self, pts, mvp, W: int, H: int,
                          color=(1.0, 1.0, 1.0, 1.0),
                          width: float = 1.0) -> None:
        """屏幕空间线段批量绘制（浮雕模式下密集曲线折线的攒批通道）。

        ``pts``: (K,2) float32 像素坐标（与视口 w/h 同一坐标系），相邻两点是
        一段线（GL_LINES）；``color`` 为整批统一的着色（调用方按颜色分组）。

        为什么单独开这个入口：QPainter 画折线要逐点构造 QPointF/QPolygonF
        再抗锯齿光栅化，实测 6 万点 275ms/帧、18 万点 823ms/帧，描图曲线多
        时整帧被它独占；GL 一次 draw call 画完整批，量级降到 0.06µs/点。

        顶点格式刻意只放坐标（2f）+ uniform 颜色：描图曲线的颜色按对象
        分组，逐顶点存 4 个颜色浮点纯属浪费带宽（28→8 字节/顶点）。
        """
        # ascontiguousarray：下面走 ``p.data``（buffer protocol）直接上传，
        # 非连续视图（切片/转置）在 buffer protocol 下会直接抛 BufferError。
        p = np.ascontiguousarray(pts, dtype=np.float32)
        if p.ndim != 2 or p.shape[1] != 2 or p.shape[0] < 2:
            return
        self.ensure()
        ctx = self.ctx
        ctx.viewport = (0, 0, W, H)
        if self._lineProg is None:
            self._lineProg = ctx.program(vertex_shader=LINE_VERT,
                                         fragment_shader=LINE_FRAG)
        prog = self._lineProg
        prog["u_mvp"].write(
            np.ascontiguousarray(np.asarray(mvp, dtype=np.float64).T,
                                 dtype="f4"))
        prog["u_color"].write(
            np.asarray(color, dtype="f4").tobytes())
        # 状态硬化（同 draw_screen_polyline）：QPainter 可能遗留 scissor /
        # 面剔除 / 深度测试，不清会把线裁掉或挡在曲面后面。
        ctx.disable(0x0C11)   # GL_SCISSOR_TEST
        ctx.disable(ctx.CULL_FACE)
        ctx.disable(ctx.DEPTH_TEST)
        ctx.enable(ctx.BLEND)
        ctx.blend_func = (ctx.SRC_ALPHA, ctx.ONE_MINUS_SRC_ALPHA)
        try:
            ctx.line_width = float(width)
        except Exception:
            pass          # 驱动只支持 1px：退回细线，不影响正确性
        # 常驻 VBO（跨帧复用，只在容量不足时重建）+ **每帧新建 VAO**：
        # 本机 Qt(QOpenGLWidget)+ModernGL+MSAA 组合下，VAO 跨帧复用会让
        # render() 访问违例/静默不画（同一教训见 draw() 里的注释），故 VAO
        # 每次调用新建、渲染后立即释放；缓冲本身跨帧复用以省分配。
        nbytes = p.nbytes
        if self._lineBuf is None or self._lineBuf.size < nbytes:
            if self._lineBuf is not None:
                try:
                    self._lineBuf.release()
                except Exception:
                    pass
            self._lineBuf = ctx.buffer(reserve=max(nbytes, 1 << 16))
        try:
            # numpy → GL 走 buffer protocol，不经 bytes 中转（省一次全量拷贝）
            self._lineBuf.write(p.data)
        except TypeError:
            self._lineBuf.write(p.tobytes())
        vao = ctx.vertex_array(prog, [(self._lineBuf, "2f", "a_pos")])
        try:
            vao.render(moderngl.LINES, vertices=p.shape[0])
        finally:
            vao.release()
            ctx.disable(ctx.BLEND)

    def release(self) -> None:
        for _attr in ("_lineBuf",):
            _o = getattr(self, _attr, None)
            if _o is not None:
                try:
                    _o.release()
                except Exception:
                    pass
            setattr(self, _attr, None)
        if self._lineProg is not None:
            try:
                self._lineProg.release()
            except Exception:
                pass
            self._lineProg = None
        if self._pathProg is not None:
            try:
                self._pathProg.release()
            except Exception:
                pass
            self._pathProg = None
        if self._stockProg is not None:
            try:
                self._stockProg.release()
            except Exception:
                pass
            self._stockProg = None
        for _t in self._stockTexs.values():
            try:
                _t.release()
            except Exception:
                pass
        self._stockTexs = {}
        self._stockGrids = {}
        # 路径缓存只存 numpy 顶点数组，无 GPU 资源，直接清空
        self._path_cache.clear()
        if self._edgeProg is not None:
            try:
                self._edgeProg.release()
            except Exception:
                pass
            self._edgeProg = None
        if self.vao is not None:
            self.vao.release()
        self.vao = None
        for _b in (self._pos, self._nrm, self._base, self._idx):
            if _b is not None:
                _b.release()
        self._pos = self._nrm = self._base = self._idx = None
        self._idxSrc = None
        self._posSrc = self._nrmSrc = self._baseSrc = None
        self._roiPartial = False
        self._key = None
        if self._bzFbo is not None:
            self._bzFbo.release()
        if self._bzDepth is not None:
            self._bzDepth.release()
        self._bzFbo = None
        self._bzDepth = None
        self._bzW = self._bzH = 0
