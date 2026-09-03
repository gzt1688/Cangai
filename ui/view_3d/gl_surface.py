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
out vec4 frag;
void main() {
  vec3 N = normalize(v_normal);
  vec3 L = normalize(u_light);
  vec3 V = normalize(u_eye - v_world);
  vec3 Hh = normalize(L + V);
  float diff = max(dot(N, L), 0.0);
  float head = max(dot(N, V), 0.0);          // 头灯：光随视线，正面恒亮
  float sp = max(dot(N, Hh), 0.0);
  float spec = u_spec_gain * pow(sp, u_spec_pow);
  // 主光与头灯取亮者，再抬高下限消除死黑（与 core.relief.mesh3d 的 _HEAD/_FLOOR 一致）
  float d = max(diff, head * 0.90);
  float soft = 0.50 + 0.50 * d;
  vec3 base = v_base;
  vec3 col;
  if (u_mode == 5) {                       // plastic: 显式环境 + 漫反射
    col = base * (u_add_amb + u_kd * soft) + vec3(spec);
  } else if (u_mode == 6) {                // metal: + 边缘菲涅尔
    float fres = pow(1.0 - clamp(N.z, 0.0, 1.0), 2.0);
    col = base * (u_add_amb + u_kd * soft) + base * 0.5 * fres + vec3(spec);
  } else {                                 // map/color/clay/wood/stone: 基础明暗 lit
    float lit = u_ambient + (1.0 - u_ambient) * soft;
    col = base * lit + vec3(spec);
  }
  col = clamp(col, 0.0, 1.0);
  frag = vec4(col, u_opacity);
}
"""


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


class GLSurface:
    """GPU 浮雕曲面渲染器（ModernGL）。

    同一网格只在 key 变化时重建顶点缓冲；旋转期间只重传 MVP/相机 uniform。
    """

    def __init__(self) -> None:
        self.ctx: moderngl.Context | None = None
        self.prog: moderngl.Program | None = None
        self.vao: moderngl.VertexArray | None = None
        self._pos = self._nrm = self._base = self._idx = None
        self._key = None

    def ensure(self) -> moderngl.Context:
        if self.ctx is None:
            # 绑定当前已激活的 Qt GL 上下文（QOpenGLWidget.paintGL 中）
            self.ctx = moderngl.create_context()
            self.prog = self.ctx.program(vertex_shader=VERT, fragment_shader=FRAG)
            dbg(f"GLSurface.ensure: created ctx id={id(self.ctx)} "
                f"renderer={self.ctx.info.get('GL_RENDERER')}")
        return self.ctx

    def frame(self, W: int, H: int, bg, fbo: int = 0) -> None:
        """清屏（背景色 + 深度），为叠加层之外的纯清空帧准备。"""
        self.ensure()
        self.ctx.viewport = (0, 0, W, H)
        self.ctx.clear(color=bg, depth=1.0)

    def draw(self, key, pos, nrm, base, idx, mvp, eye, light, params,
             W: int, H: int, bg, fbo: int = 0, opacity: float = 1.0) -> None:
        """绘制浮雕曲面。

        ``opacity < 1`` 时开启混合（半透明 X-Ray：浮雕编辑态下让参考曲线透过
        模型清晰可见），否则按不透明渲染。
        """
        self.ensure()
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

        # 每帧重建 GPU 缓冲/VAO：本机 Qt(QOpenGLWidget)+ModernGL+MSAA 组合下，
        # 跨帧复用同一 VAO 时 render() 会访问违例闪退；而“同帧新建再渲染”始终稳定
        # （见 render_debug.log：REBUILD 帧 post-render OK，REUSE 帧崩在 render）。
        # CPU 侧网格/顶点色已由视口缓存，这里只做 ~3MB/帧的缓冲上传（独显无压力）。
        if self.vao is not None:
            self.vao.release()
            self.vao = None
        for _b in (self._pos, self._nrm, self._base, self._idx):
            if _b is not None:
                _b.release()
        self._pos = ctx.buffer(np.ascontiguousarray(pos.astype("f4")))
        self._nrm = ctx.buffer(np.ascontiguousarray(nrm.astype("f4")))
        self._base = ctx.buffer(np.ascontiguousarray(base.astype("f4")))
        self._idx = ctx.buffer(np.ascontiguousarray(idx.astype("u4")))
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
        self.vao.render(moderngl.TRIANGLES)

    def release(self) -> None:
        if self.vao is not None:
            self.vao.release()
        self.vao = None
        for _b in (self._pos, self._nrm, self._base, self._idx):
            if _b is not None:
                _b.release()
        self._pos = self._nrm = self._base = self._idx = None
        self._key = None
