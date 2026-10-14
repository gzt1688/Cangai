"""图片转浮雕模型核心算法（复刻 Relifto3D）。

完整流程:
1. 导入灰度图 → 2. 识别黑色背景 → 3. 直方图自动拉伸 → 4. 生成归一化高度场
→ 5. 边缘保护式曲面磨光 → 6. 细节增强(CLAHE+锐化) → 7. 分层高度重映射
→ 8. 输出 HeightField(mm)，供 cangai_relief 的 ReliefModelObject 直接使用。

坐标约定 (与 cangai_relief HeightField 一致):
- X = col * spacing   (mm)
- Y = row * spacing   (mm)
- Z = 高度 (mm, 由归一化高度 * depthMm 得到)
- 行 0 = minY (y=origin.y), 列 0 = minX (x=origin.x)
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
import time
import numpy as np
import cv2

from .height_field import HeightField, MAX_PIXELS


def _max_field_pixels() -> int:
    """与 HeightField.grid_size 同源的栅格总像素上限（设置页可调）。"""
    try:
        from core import app_settings as _s
        return max(1, int(_s.max_field_pixels()))
    except Exception:
        return MAX_PIXELS


SUPPORTED_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")


# ---------------------------------------------------------------------------
# 参数定义
# ---------------------------------------------------------------------------
@dataclass
class ImageToModelParams:
    """图片转模型可调参数。

    所有单位约定:
        - depthMm, highAdjust, lowAdjust: 毫米 (mm)
        - pixelSizeMm: 毫米/像素 (mm/px)
        - 强度类 (smoothStrength / detailEnhance / edgeThreshold / clipPercent /
          blackThreshold): 0~100 或 0~255 的整数档位
    """
    # --- 尺寸与深度 ---
    depthMm: float = 2.0              # 最大凸起高度 (浮雕总深度) 1~20 mm
    pixelSizeMm: float = 0.1          # XY 像素间距 mm (决定生成模型的世界尺寸)
    baseHeight: float = 0.0           # 基础高度 mm (整块模型统一垫高，用于有厚度底座)

    # --- 图像预处理 ---
    scale: int = 1                    # 放大倍率 1~4 (1=不放大)
    clipPercent: float = 0.5          # 直方图两端裁剪 %  (0~5)
    blackThreshold: int = 8           # 背景黑度阈值 0~30 (灰度<=此值视为背景)

    # --- 曲面磨光 (边缘保护) ---
    smoothStrength: int = 30          # 磨光强度 0~100 (0=关闭)
    edgeThreshold: int = 8            # 边缘保护阈值 1~30 (越大越保护边缘)

    # --- 细节增强 (CLAHE + Unsharp Masking) ---
    detailEnhance: int = 0            # 0~100 (0=关闭)

    # --- 分层高度重映射 ---
    midPos: float = 0.5               # 中层位置 0~1 (默认正中)
    highAdjust: float = 0.0           # 高层(凸起部分)调整 mm (正=再拉高)
    lowAdjust: float = 0.0            # 底层调整 mm (正=再拉高)

    # --- 其它 ---
    invert: bool = False              # 是否反色 (黑凸白凹，用于特殊灰度图)
    autoCenter: bool = True           # 是否自动把模型置于 (0,0) 附近居中


# ---------------------------------------------------------------------------
# 1. 图片加载
# ---------------------------------------------------------------------------
def is_supported_image(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in SUPPORTED_EXTS


def load_image_gray(path: str) -> np.ndarray:
    """读取图片并返回单通道 uint8 灰度矩阵 (H, W)。

    有 alpha 通道时，透明像素视作黑色背景，与 Relifto3D 一致。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"图片不存在: {path}")
    if not is_supported_image(path):
        raise ValueError(f"不支持的图片格式: {os.path.splitext(path)[1]}")

    raw = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise ValueError(f"无法解码图片: {path}")

    if raw.ndim == 3 and raw.shape[2] == 4:
        bgr = raw[:, :, :3].copy()
        alpha = raw[:, :, 3]
        bgr[alpha == 0] = 0
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    elif raw.ndim == 3:
        gray = cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY)
    elif raw.ndim == 2:
        gray = raw
    else:
        raise ValueError(f"不支持的通道数: {raw.ndim}")
    return gray


# ---------------------------------------------------------------------------
# 2. 背景识别
# ---------------------------------------------------------------------------
def compute_background_mask(gray: np.ndarray,
                            black_threshold: int = 8) -> np.ndarray:
    """识别黑色背景：灰度 <= threshold 的像素视作背景 (True=背景)。"""
    if black_threshold <= 0:
        return np.zeros(gray.shape, dtype=bool)
    return gray <= int(black_threshold)


# ---------------------------------------------------------------------------
# 3. 超分放大 (OpenCV LANCZOS4 高质量插值回退)
# ---------------------------------------------------------------------------
def upscale_gray(gray: np.ndarray, scale: int) -> np.ndarray:
    """用 LANCZOS4 放大灰度图。scale <= 1 返回原数组。

    内存护栏：放大后总像素不得超过 ``max_field_pixels``（与 HeightField
    同源上限），超限时自动降低倍率——大图 ×4 超分会把后续磨光/增强的
    全场临时数组推到 GB 级（32GB 机器死机教训），这里直接掐断。
    """
    scale = int(max(1, scale))
    if scale == 1:
        return gray
    h, w = gray.shape[:2]
    mp = float(_max_field_pixels())
    if h * w * scale * scale > mp:
        s = int(math.sqrt(mp / max(1.0, float(h) * float(w))))
        s = max(1, min(scale, s))
        if s == 1:
            return gray
        scale = s
    return cv2.resize(gray, (w * scale, h * scale),
                      interpolation=cv2.INTER_LANCZOS4)


def upscale_mask(mask: np.ndarray, target_shape: tuple) -> np.ndarray:
    """最近邻放大 bool 背景掩码到目标形状 (th, tw)。"""
    th, tw = int(target_shape[0]), int(target_shape[1])
    if mask.shape == (th, tw):
        return mask.astype(bool)
    out = cv2.resize(mask.astype(np.uint8), (tw, th),
                     interpolation=cv2.INTER_NEAREST)
    return out > 0


# ---------------------------------------------------------------------------
# 4. 直方图自动拉伸 (两端裁剪噪声后线性拉到 0~255)
# ---------------------------------------------------------------------------
def auto_stretch(gray: np.ndarray, clip_percent: float = 0.5,
                 bg_mask: np.ndarray | None = None) -> np.ndarray:
    """直方图自动拉伸。

    - 背景像素不参与统计，拉伸后仍保持 0 (纯黑)。
    - clip_percent: 低端/高端各裁剪的像素比例 (%)。默认 0.5%，能去掉
      JPEG 压缩噪声与极端孤立像素导致的灰度压缩。
    """
    if gray.dtype != np.uint8:
        # 公开 API 可能收到 float [0,1] 输入：直接 astype 会全部截断为 0
        gray = np.clip(gray, 0, 255).astype(np.uint8)
    if bg_mask is not None:
        fg = gray[~bg_mask]
    else:
        fg = gray.ravel()
    if fg.size == 0:
        return gray.copy()

    clip = max(0.0, min(49.0, float(clip_percent)))
    lo = float(np.percentile(fg, clip))
    hi = float(np.percentile(fg, 100.0 - clip))

    if hi - lo < 1.0:
        out = gray.copy()
    else:
        f = gray.astype(np.float32)
        f = (f - lo) * (255.0 / (hi - lo))
        out = np.clip(f, 0.0, 255.0).astype(np.uint8)

    if bg_mask is not None:
        out[bg_mask] = 0
    return out


# ---------------------------------------------------------------------------
# 5. 灰度 → 归一化高度场 [0,1]
# ---------------------------------------------------------------------------
def gray_to_height_normalized(gray: np.ndarray,
                              bg_mask: np.ndarray | None = None,
                              invert: bool = False) -> np.ndarray:
    """uint8 灰度 → float32 归一化高度 [0,1]。背景清零。

    invert=True 时翻转高度 (白→低、黑→高)，用于特殊"阴刻"灰度图。
    """
    h = gray.astype(np.float32) / 255.0
    if invert:
        h = 1.0 - h
    if bg_mask is not None:
        h[bg_mask] = 0.0
    return h.astype(np.float32)


# ---------------------------------------------------------------------------
# 6. 边缘保护式曲面磨光 (核心算法，复刻 Relifto3D)
# ---------------------------------------------------------------------------
def _max_neighbor_diff(h: np.ndarray) -> np.ndarray:
    """形态学梯度近似：膨胀 - 腐蚀，反映 8 邻域最大高度差 (边缘强度)。"""
    kernel = np.ones((3, 3), np.uint8)
    return cv2.dilate(h, kernel) - cv2.erode(h, kernel)


def edge_protected_smooth(height_norm: np.ndarray,
                          smooth_strength: int = 30,
                          edge_threshold: int = 8,
                          bg_mask: np.ndarray | None = None) -> np.ndarray:
    """边缘保护式高度场平滑。

    原理:
      - 低高度差区域 (平缓曲面): 高斯平滑磨光，去除像素台阶与层纹。
      - 高高度差区域 (边缘/轮廓): 保留原值，保证图案线条锐利。
      - 通过 "1 / (1 + (diff/threshold)^2)" 作为过渡权重，实现平滑过渡。

    参数:
        height_norm:    float32 [0,1] 归一化高度场
        smooth_strength: 0~100，越大核越大、迭代越多、混合越强
        edge_threshold:  1~30 (灰度值尺度，内部换算到 [0,1])
    """
    strength = max(0, min(100, int(smooth_strength)))
    if strength <= 0:
        out = height_norm.copy()
        if bg_mask is not None:
            out[bg_mask] = 0.0
        return out

    h = height_norm.astype(np.float32).copy()
    if bg_mask is not None:
        h[bg_mask] = 0.0

    s_norm = float(strength) / 100.0
    threshold = max(1.0, float(edge_threshold)) / 255.0  # → [0,1]

    # 核大小: 3 (弱) → 5 (中) → 7 (强)
    ksize = 3 if s_norm < 0.34 else (5 if s_norm < 0.67 else 7)
    sigma = 0.3 * ((ksize - 1) * 0.5 - 1) + 0.8
    # 迭代次数: 1 → 2 → 3
    iters = 1 if s_norm < 0.34 else (2 if s_norm < 0.67 else 3)
    blend = s_norm  # 每次迭代混合系数 = s_norm (0~1)

    work = h.copy()
    for _ in range(iters):
        diff = _max_neighbor_diff(work)
        ratio = diff / threshold
        edge_weight = 1.0 / (1.0 + ratio * ratio)
        smoothed = cv2.GaussianBlur(work, (ksize, ksize), sigma)
        w = edge_weight * blend
        work = work * (1.0 - w) + smoothed * w
        if bg_mask is not None:
            work[bg_mask] = 0.0

    work = np.clip(work, 0.0, 1.0)
    return work.astype(np.float32)


# ---------------------------------------------------------------------------
# 7. 细节增强 (CLAHE 局部对比度 + Unsharp Masking)
# ---------------------------------------------------------------------------
def enhance_detail(height_norm: np.ndarray,
                   strength: int = 0,
                   bg_mask: np.ndarray | None = None) -> np.ndarray:
    """细节增强：局部对比度拉伸 + 微锐化，呈现"深浮雕"层次感。

    - CLAHE: 局部窗口内独立拉伸灰度范围，让浅层凹凸获得明显高度差。
    - Unsharp Masking: 叠加高频细节，锐化凹凸转折边缘。
    """
    strength = max(0, min(100, int(strength)))
    if strength <= 0:
        out = height_norm.copy()
        if bg_mask is not None:
            out[bg_mask] = 0.0
        return out

    h = height_norm.astype(np.float32).copy()
    if bg_mask is not None:
        h[bg_mask] = 0.0

    s_norm = float(strength) / 100.0

    # CLAHE：clipLimit 随强度从 1.0 → 4.0；tileGridSize 从 12 → 4（越局部）
    h_u8 = np.clip(h * 255.0, 0, 255).astype(np.uint8)
    cl = 1.0 + s_norm * 3.0
    ts = max(4, int(round(12 - s_norm * 8)))
    clahe = cv2.createCLAHE(clipLimit=cl, tileGridSize=(ts, ts))
    enhanced = clahe.apply(h_u8).astype(np.float32) / 255.0

    # Unsharp Masking: h' = h + k * (h - blur(h))
    blur = cv2.GaussianBlur(enhanced, (3, 3), 0.8)
    k = 0.6 * s_norm
    enhanced = enhanced + k * (enhanced - blur)

    # 前景归一化回 [0,1]（保留背景 0）
    if bg_mask is not None:
        fg = enhanced[~bg_mask]
    else:
        fg = enhanced.ravel()
    if fg.size > 0:
        lo = float(fg.min())
        hi = float(fg.max())
        if hi - lo > 1e-6:
            enhanced = (enhanced - lo) / (hi - lo)

    enhanced = np.clip(enhanced, 0.0, 1.0).astype(np.float32)
    if bg_mask is not None:
        enhanced[bg_mask] = 0.0
    return enhanced


# ---------------------------------------------------------------------------
# 8. 分层高度重映射 (高层/底层独立拉高或压低)
# ---------------------------------------------------------------------------
def remap_height(height_norm: np.ndarray,
                 mid_pos: float = 0.5,
                 high_adjust_mm: float = 0.0,
                 low_adjust_mm: float = 0.0,
                 depth_mm: float = 2.0,
                 bg_mask: np.ndarray | None = None,
                 band: float = 0.1) -> np.ndarray:
    """分层高度重映射：最高处 / 最低处独立调整，中层带平滑过渡。

    约定 h ∈ [0,1], mid_pos=m 为中层分界：
    - high_adjust: 最高处(h=1)影响100% → 中层影响10% → 中层下方band处影响0%
    - low_adjust:  最低处(h=0)影响100% → 中层影响10% → 中层上方band处影响0%
    - adjust 单位 mm，正=拉高，负=压低；内部按 depth_mm 换算到归一化单位。
    """
    h = np.asarray(height_norm, dtype=np.float32)
    out = h.copy()
    m = float(mid_pos)
    a = float(high_adjust_mm) / max(float(depth_mm), 1e-6)   # → 归一化调节量
    b = float(low_adjust_mm) / max(float(depth_mm), 1e-6)
    bd = max(1e-3, float(band))

    if a != 0.0:
        denom = max(1.0 - m, 1e-6)
        k = 1.0 + a / denom
        target = m + (h - m) * k
        w = np.zeros_like(h)
        high = h >= m
        w[high] = 0.1 + 0.9 * (h[high] - m) / denom
        fade = (h >= m - bd) & (h < m)
        w[fade] = 0.1 * (h[fade] - (m - bd)) / bd
        out = out + (target - h) * w

    if b != 0.0:
        denom = max(m, 1e-6)
        k = 1.0 - b / denom
        target = m + (h - m) * k
        w = np.zeros_like(h)
        low = h <= m
        w[low] = 0.1 + 0.9 * (m - h[low]) / denom
        fade = (h > m) & (h <= m + bd)
        w[fade] = 0.1 * (m + bd - h[fade]) / bd
        out = out + (target - h) * w

    out = np.maximum(out, 0.0).astype(np.float32)
    if bg_mask is not None:
        out[bg_mask] = 0.0
    return out


# ---------------------------------------------------------------------------
# 9. 归一化高度场 [0,1] → HeightField (mm 单位，可直接加入文档)
# ---------------------------------------------------------------------------
def height_to_heightfield(height_mm: np.ndarray,
                          pixel_size_mm: float,
                          origin_xy: tuple = (0.0, 0.0),
                          base_height_mm: float = 0.0) -> HeightField:
    """把 (H, W) 的高度图 (mm) 包装成 cangai_relief 的 HeightField。

    注意：HeightField 约定 "行 0 = minY"，这里 height_mm[row=0] 即 y=origin.y，
    无需翻转行序 (与 Relifto3D 直接导入→生成的方式一致)。
    """
    data = np.asarray(height_mm, dtype=np.float32)
    if base_height_mm:
        data = data + float(base_height_mm)
    # 高度不能为负
    data = np.maximum(data, 0.0)
    return HeightField(data, (float(origin_xy[0]), float(origin_xy[1])),
                       float(pixel_size_mm))


# ---------------------------------------------------------------------------
# 10. 一键流水线 (顶层 API)
# ---------------------------------------------------------------------------
@dataclass
class ImageToModelResult:
    gray_original: np.ndarray        # (H, W) uint8 原图灰度
    gray_processed: np.ndarray       # (H', W') uint8 超分+拉伸后灰度
    bg_mask: np.ndarray              # (H', W') bool 背景掩码
    height_normalized: np.ndarray    # (H', W') float32 [0,1] 磨光+增强+重映射后
    height_field: HeightField        # mm 单位 HeightField (最终产物)
    timings: dict


def run_pipeline(image_path: str,
                 params: ImageToModelParams | None = None,
                 progress_cb=None) -> ImageToModelResult:
    """完整流水线：图片路径 → HeightField。

    progress_cb(pct: int, msg: str) 可选，用于 UI 进度显示。
    """
    gray = load_image_gray(image_path)
    return _process_gray_to_hf(gray, params, progress_cb, loaded_from_path=True)


# ---------------------------------------------------------------------------
# 11. 从"文档中已导入的图片 ImageObject"直接启动流水线
# ---------------------------------------------------------------------------
def decode_image_bytes(data: bytes) -> np.ndarray:
    """把 ImageObject.imageData() 的原始字节 (PNG/JPG/…) 解码为 uint8 单通道灰度矩阵。

    不依赖 cv2.imread（不走磁盘），直接用 cv2.imdecode 从内存字节解码，
    处理 alpha 通道逻辑与 load_image_gray 一致（透明→黑色背景合并）。
    """
    if not data:
        raise ValueError("图片字节为空")
    buf = np.frombuffer(data, dtype=np.uint8)
    raw = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise ValueError("无法解码图片数据（格式不支持或损坏）")

    if raw.ndim == 3 and raw.shape[2] == 4:
        bgr = raw[:, :, :3].copy()
        alpha = raw[:, :, 3]
        bgr[alpha == 0] = 0
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    elif raw.ndim == 3:
        gray = cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY)
    elif raw.ndim == 2:
        gray = raw
    else:
        raise ValueError(f"不支持的通道数: {raw.ndim}")
    return gray


def run_pipeline_from_gray(gray: np.ndarray,
                           params: ImageToModelParams | None = None,
                           progress_cb=None) -> ImageToModelResult:
    """完整流水线：已有 uint8 灰度图 → HeightField。

    用于"图片已导入到软件里 (ImageObject)"的场景：从 ImageObject 取字节解码
    后直接调用，免去写临时文件；进度、参数、产物与 run_pipeline 完全一致。
    """
    g = np.asarray(gray)
    if g.ndim != 2:
        raise ValueError(f"gray 必须是二维 (H,W)，实际 shape={g.shape}")
    # 保证 uint8：避免上游传 float 造成流程异常
    if g.dtype != np.uint8:
        g = np.clip(g, 0, 255).astype(np.uint8)
    return _process_gray_to_hf(g, params, progress_cb, loaded_from_path=False)


# ---------------------------------------------------------------------------
# 共享内部分支：灰度 → 背景识别 → 放大 → 拉伸 → 归一化 → 磨光 → 增强 → 重映射 → HF
# ---------------------------------------------------------------------------
def _process_gray_to_hf(gray: np.ndarray,
                       params: ImageToModelParams | None,
                       progress_cb,
                       loaded_from_path: bool) -> ImageToModelResult:
    p = params if params is not None else ImageToModelParams()
    t = {}

    def step(pct, msg):
        if progress_cb is not None:
            try:
                progress_cb(int(pct), str(msg))
            except Exception:
                pass

    # 1. 原图灰度 (对于 from_gray 路径就是输入，跳过"导入"阶段)
    original_gray = gray.copy()
    if loaded_from_path:
        step(5, "导入灰度图 ...")
    else:
        step(10, "读取图片数据 ...")

    step(20, "识别黑色背景 ...")
    bg_mask = compute_background_mask(gray, p.blackThreshold)

    step(30, f"放大图像 x{p.scale} ...")
    t0 = time.time()
    if p.scale > 1:
        gray2 = upscale_gray(gray, p.scale)
        bg_mask2 = upscale_mask(bg_mask, gray2.shape)
    else:
        gray2 = gray
        bg_mask2 = bg_mask
    t["upscale"] = time.time() - t0

    step(45, "灰度直方图自动拉伸 ...")
    t0 = time.time()
    stretched = auto_stretch(gray2, p.clipPercent, bg_mask2)
    t["stretch"] = time.time() - t0

    step(55, "生成浮点高度矩阵 ...")
    h_norm = gray_to_height_normalized(stretched, bg_mask2, invert=p.invert)

    step(70, "边缘保护式曲面磨光 ...")
    t0 = time.time()
    h_smooth = edge_protected_smooth(h_norm, p.smoothStrength,
                                     p.edgeThreshold, bg_mask2)
    t["smooth"] = time.time() - t0

    step(80, "细节增强 ...")
    t0 = time.time()
    h_detail = enhance_detail(h_smooth, p.detailEnhance, bg_mask2)
    t["enhance"] = time.time() - t0

    step(88, "分层高度重映射 ...")
    t0 = time.time()
    h_remap = remap_height(h_detail, mid_pos=p.midPos,
                           high_adjust_mm=p.highAdjust,
                           low_adjust_mm=p.lowAdjust,
                           depth_mm=p.depthMm, bg_mask=bg_mask2)
    t["remap"] = time.time() - t0

    step(94, "生成 HeightField (mm) ...")
    h_mm = h_remap * float(p.depthMm)
    ny, nx = h_mm.shape
    sp = float(p.pixelSizeMm)
    if p.autoCenter:
        ox = -0.5 * (nx - 1) * sp
        oy = -0.5 * (ny - 1) * sp
    else:
        ox, oy = 0.0, 0.0
    hf = height_to_heightfield(h_mm, sp, (ox, oy), p.baseHeight)

    step(100, "完成")
    return ImageToModelResult(
        gray_original=original_gray,
        gray_processed=stretched,
        bg_mask=bg_mask2,
        height_normalized=h_remap,
        height_field=hf,
        timings=t,
    )


# ---------------------------------------------------------------------------
# 12. 两阶段拆分：阶段 A 图像处理（用于覆盖原图），阶段 B 高度场生成（用于建模型）
# ---------------------------------------------------------------------------

@dataclass
class ImageProcessParams:
    """阶段 A：仅图像处理参数（输出是 uint8 灰度图，可覆盖文档中的原 ImageObject）。"""
    scale: int = 1                       # 超分倍率 1~4
    blackThreshold: int = 8              # 背景阈值 0~50
    clipPercent: float = 0.5             # 直方图两端裁剪 %
    smoothStrength: int = 30             # 磨光强度 0~100
    edgeThreshold: int = 8               # 边缘阈值 1~50
    detailEnhance: int = 0               # 细节增强 0~100
    invert: bool = False                 # 反色 (对最终处理图生效)
    output01Height: bool = False         # 若 True，输出 [0,255] 对应 [0,1] 高度映射
                                         # (否则输出人眼观感的灰度视觉)

    # (以下仅对"视觉灰度输出"有效：output01Height=False)
    # 磨光 / 细节增强后的 [0,1] 高度场视觉映射回 uint8 灰度：
    #   默认用 gamma=1 的线性映射；若想让浅色区域层次更明显可改 gamma。
    gammaForVisual: float = 1.0


@dataclass
class HeightFieldParams:
    """阶段 B：高度场生成参数（从已处理的 uint8 灰度图 → HeightField）。"""
    depthMm: float = 2.0                 # 浮雕深度 mm
    pixelSizeMm: float = 0.1             # 像素间距 mm/px
    baseHeight: float = 0.0              # 底座 mm
    blackThreshold: int = 8              # 背景阈值：在最终 255 灰度中 ≤ 此值视为背景清零
    autoCenter: bool = False             # 是否把 HeightField 居中到 (0,0)
    gridSampling: int = 1                # 网格采样：1=每像素一个栅格，2=每2像素一个栅格(降采样)


def process_image_only(gray: np.ndarray,
                       p: ImageProcessParams,
                       progress_cb=None) -> np.ndarray:
    """阶段 A：只做图像处理，返回 uint8 灰度矩阵（用于覆盖原图）。

    流程：超分 → 背景识别 → 直方图拉伸 → (可选: 归一化高度+磨光+细节+重映射)
         → 映射回 uint8 灰度。
    说明：如果 p.output01Height=True，输出是"高度值 0→255"的映射（越白越高），
    可以作为后续 HeightFieldParams 直接建模的输入（无需再识别背景拉伸），
    但仍推荐 blackThreshold=0 避免再次清零。
    """
    def step(pct, msg):
        if progress_cb is not None:
            try:
                progress_cb(int(pct), str(msg))
            except Exception:
                pass

    g = np.asarray(gray)
    if g.ndim != 2:
        raise ValueError("gray 必须是二维")
    if g.dtype != np.uint8:
        g = np.clip(g, 0, 255).astype(np.uint8)

    # 1) 超分
    step(20, "超分放大")
    s = int(max(1, p.scale))
    if s > 1:
        g2 = upscale_gray(g, s)
    else:
        g2 = g.copy()

    # 2) 背景识别 + 拉伸
    step(40, "背景识别 + 直方图拉伸")
    bg = compute_background_mask(g2, p.blackThreshold)
    stretched = auto_stretch(g2, p.clipPercent, bg)

    # 3) 走完整高度流水线（磨光+细节）但不用深度/重映射
    step(70, "磨光 + 细节")
    # 背景清零后归一化高度
    hn = gray_to_height_normalized(stretched, bg, invert=False)
    hs = edge_protected_smooth(hn, p.smoothStrength, p.edgeThreshold, bg)
    hd = enhance_detail(hs, p.detailEnhance, bg)

    # 映射回 uint8 灰度
    step(90, "映射回灰度")
    if p.output01Height:
        # 0.0~1.0 → 0~255 线性
        out_norm = hd
    else:
        # 视觉用 gamma 修正（观感更自然），再反色
        gam = max(0.1, float(p.gammaForVisual))
        if abs(gam - 1.0) > 1e-3:
            out_norm = np.power(hd, 1.0 / gam).astype(np.float32)
        else:
            out_norm = hd
    if p.invert:
        out_norm = 1.0 - out_norm
    out = np.clip(out_norm * 255.0, 0, 255).astype(np.uint8)
    # 保留背景纯黑（一致语义）
    out[bg] = 0
    step(100, "图像处理完成")
    return out


def heightfield_from_processed(processed_gray: np.ndarray,
                               p: HeightFieldParams,
                               progress_cb=None) -> HeightField:
    """阶段 B：已处理的灰度图 → HeightField。

    假定 processed_gray 的"越白越高"映射已经正确（阶段 A output01Height=True 最佳），
    这里仅做：(可选) 网格降采样 → 背景识别简单清零 → 归一化高度 → 乘深度 → HeightField。
    """
    def step(pct, msg):
        if progress_cb is not None:
            try:
                progress_cb(int(pct), str(msg))
            except Exception:
                pass

    import numpy as np
    g = np.asarray(processed_gray)
    if g.ndim != 2:
        raise ValueError("processed_gray 必须是二维")
    if g.dtype != np.uint8:
        g = np.clip(g, 0, 255).astype(np.uint8)

    step(20, "网格采样")
    gs = int(max(1, p.gridSampling))
    if gs > 1:
        # 等间隔采样（降采样）: [0, gs, 2gs, ...]
        ny, nx = g.shape
        step_rows = list(range(0, ny, gs))
        step_cols = list(range(0, nx, gs))
        g = g[np.ix_(step_rows, step_cols)]

    # 总像素护栏：外部传入的图（如巨大源图未经 upscale 钳制）仍可能超上限，
    # 超限时继续整格抽稀并把栅距等比放大，保持物理尺寸不变。
    extra_gs = 1
    ny, nx = g.shape
    mp = _max_field_pixels()
    if ny * nx > mp:
        extra_gs = max(1, int(math.ceil(math.sqrt(ny * nx / float(mp)))))
        g = g[::extra_gs, ::extra_gs]
        while g.shape[0] * g.shape[1] > mp and extra_gs < 65536:
            extra_gs += 1
            g = g[::extra_gs, ::extra_gs]

    step(40, "背景识别")
    thr = int(max(0, p.blackThreshold))
    bg = (g <= thr) if thr > 0 else np.zeros(g.shape, dtype=bool)

    step(60, "高度归一化")
    hnorm = g.astype(np.float32) / 255.0
    # 归一化到 [0,1]：处理阶段做了 gamma+磨光，应该已包含正确的 0/1 分布，不再拉伸
    # 但为保险起见，让非背景像素 min→0 max→1（保证深度能打满）
    fg = hnorm[~bg]
    if fg.size > 0:
        lo = float(fg.min())
        hi = float(fg.max())
        if hi - lo > 1e-3:
            hnorm = (hnorm - lo) / (hi - lo)
        else:
            hnorm = hnorm - lo   # 前景平坦：背景同样清零、压回 [0,1]
        hnorm[bg] = 0.0
        hnorm = np.clip(hnorm, 0.0, 1.0)

    step(80, "生成 HeightField")
    h_mm = hnorm * float(p.depthMm)
    ny, nx = h_mm.shape
    sp = float(p.pixelSizeMm) * extra_gs   # 抽稀了 extra_gs 倍，栅距等比放大
    # 若阶段 A 做了超分放大（图像更多像素），同时阶段 B 用 gridSampling>1 降采样，
    # 则 spacing=pixelSizeMm*scale/gs 才对。这里约定传入的 pixelSizeMm 是"最终栅格间距"
    # （由外部根据 scale/gs 以及 ImageObject 原始像素间距计算）。
    if p.autoCenter:
        ox = -0.5 * (nx - 1) * sp
        oy = -0.5 * (ny - 1) * sp
    else:
        ox, oy = 0.0, 0.0
    hf = height_to_heightfield(h_mm, sp, (ox, oy), p.baseHeight)
    step(100, "HeightField 生成完成")
    return hf

