"""HDRI 环境贴图加载（IBL 用）。

把一张等距柱状(equirectangular)全景环境图（.hdr/.exr/.png/.jpg）读成 float32
RGB 数组，供 GPU 上传为带 mipmap 的环境纹理，按反射向量采样做真实环境反射。
纯算法、无 UI 依赖。

读取后端按可用性依次尝试：cv2（Radiance .hdr 与常见 LDR 必支持；.exr 视编译而定）
→ openexr（官方 Imath 绑定，pip install openexr）→ imageio → Pillow。若环境里
没有任何 .exr 解码器，.exr 会读取失败——此时改用 .hdr 版本即可（cv2 原生支持）。
"""
from __future__ import annotations

import os

import numpy as np

# 上传前的环境图尺寸（2:1 等距柱状、均为 2 的幂，便于生成完整 mipmap 链）。
_ENV_W, _ENV_H = 1024, 512
# 视为 sRGB 编码的 LDR 扩展名（需 /255 后线性化）；其余按线性 HDR 处理。
_LDR_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")


def _to3ch(img) -> np.ndarray:
    """任意通道数 → (H,W,3) float32（灰度复制、多于3通道截断）。"""
    a = np.asarray(img, dtype=np.float32)
    if a.ndim == 2:
        a = a[:, :, None]
    return a[:, :, :3]


def _read_float_rgb(path: str) -> np.ndarray | None:
    """读环境图为 (H,W,3) float32 **RGB**（未归一/未缩放）。无可用后端返回 None。"""
    ext = os.path.splitext(path)[1].lower()
    # 1) cv2：.hdr / LDR 一定支持；.exr 取决于该 build 是否带 OpenEXR 编解码
    try:
        import cv2
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is not None:
            return np.ascontiguousarray(_to3ch(img)[:, :, ::-1])   # BGR → RGB
    except Exception:
        pass
    # 2) 专用 OpenEXR 后端（仅 .exr，输出线性 RGB）
    if ext == ".exr":
        try:
            import openexr
            with openexr.InputFile(path) as f:
                return np.ascontiguousarray(_to3ch(np.asarray(f.asarray(min_resolution=512), np.float32)))
        except Exception:
            pass
        try:
            import imageio.v3 as iio
            arr = iio.imread(path)
            if arr is not None:
                return np.ascontiguousarray(_to3ch(arr))
        except Exception:
            pass
    # 3) Pillow（部分构建带 exr 插件；LDR 也能读）
    try:
        from PIL import Image
        with Image.open(path) as im:
            return np.ascontiguousarray(_to3ch(np.asarray(im, dtype=np.float32)))
    except Exception:
        return None


def load_env(path: str, intensity: float = 1.0) -> np.ndarray | None:
    """加载环境图 → (H,W,3) float32 RGB（已按 intensity 缩放、去 NaN/inf）。

    失败/无文件/无解码后端返回 None（调用方回落到程序化环境）。HDR(.hdr/.exr) 保留
    高动态线性值（太阳等可 >1），供 ACES 色调映射产生真实亮部滚降；LDR(.png/.jpg)
    归一到 0..1 后按 sRGB→线性近似。统一缩放到 1024×512 等距柱状。
    """
    if not path or not os.path.isfile(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    img = _read_float_rgb(path)
    if img is None:
        return None
    if ext in _LDR_EXT:
        if float(img.max()) > 1.5:                 # cv2/PIL 读 LDR 常给 0..255
            img = img / 255.0
        img = np.power(np.clip(img, 0.0, None), 2.2)
    img = np.nan_to_num(img, nan=0.0, posinf=1e4, neginf=0.0)
    try:
        import cv2
        img = cv2.resize(np.ascontiguousarray(img), (_ENV_W, _ENV_H),
                         interpolation=cv2.INTER_AREA)
    except Exception:
        return None
    img = img * max(0.0, float(intensity))
    return np.ascontiguousarray(img, dtype=np.float32)
