"""图片对象：画布参考图（描图底图），中心/宽高/旋转描述放置几何。

数据完整性：原始图片字节（任意 Qt 支持格式）base64 嵌入 serialize，
随文档 .cai 文件整体保存/载入；base64 结果按数据缓存共享，
撤销快照多次 serialize 不会反复复制大字符串。
"""
from __future__ import annotations
import base64
import math

from PySide6.QtCore import QBuffer
from PySide6.QtGui import QImage, QPixmap

from .base_object import BaseObject
from .geometry_types import Point3D, BoundingBox3D, ObjectType


class ImageObject(BaseObject):
    def __init__(self) -> None:
        super().__init__(ObjectType.IMAGE)
        self._center = Point3D(0.0, 0.0, 0.0)   # 放置中心（世界坐标）
        self._width = 100.0                     # 世界宽
        self._height = 100.0                    # 世界高
        self._rotation = 0.0                    # 绕中心旋转角（弧度，逆时针）
        self._data: bytes = b""                 # 原始图片字节
        self._format: str = ""                  # 图片格式（PNG/JPEG…，仅记录）
        self._pixelW = 0
        self._pixelH = 0
        self._pixmap: QPixmap | None = None     # 显示缓存
        self._b64: str | None = None            # base64 缓存（快照共享引用）

    # ---------------- 数据 ----------------

    @classmethod
    def fromFile(cls, path: str) -> "ImageObject":
        """从图片文件构造（原始字节原样保存，保持全部数据）。"""
        obj = cls()
        with open(path, "rb") as f:
            obj.setImageData(f.read())
        return obj

    def setImageData(self, data: bytes) -> None:
        img = QImage.fromData(data)
        if img.isNull():
            raise ValueError("无法识别的图片数据")
        self._data = bytes(data)
        self._pixelW, self._pixelH = img.width(), img.height()
        self._pixmap = None
        self._b64 = None

    def imageData(self) -> bytes:
        return self._data

    def pixelSize(self) -> tuple:
        return (self._pixelW, self._pixelH)

    def pixmap(self) -> QPixmap:
        if self._pixmap is None and self._data:
            pm = QPixmap()
            pm.loadFromData(self._data)
            self._pixmap = pm
        return self._pixmap if self._pixmap is not None else QPixmap()

    def _b64cached(self) -> str:
        if self._b64 is None:
            self._b64 = base64.b64encode(self._data).decode("ascii")
        return self._b64

    # ---------------- 几何 ----------------

    def setGeometry(self, center: Point3D, width: float, height: float,
                    rotation: float = 0.0) -> None:
        self._center = center
        self._width = max(width, 1e-6)
        self._height = max(height, 1e-6)
        self._rotation = rotation

    def center(self) -> Point3D:
        return self._center

    def width(self) -> float:
        return self._width

    def height(self) -> float:
        return self._height

    def rotation(self) -> float:
        return self._rotation

    def points(self) -> list:
        """旋转后四角（世界坐标，供框选/采样兼容）。"""
        c, r = self._center, self._rotation
        cos, sin = math.cos(r), math.sin(r)
        hw, hh = self._width * 0.5, self._height * 0.5
        out = []
        for lx, ly in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
            out.append(Point3D(c.x + lx * cos - ly * sin,
                               c.y + lx * sin + ly * cos, c.z))
        return out

    # ---------------- 变换 ----------------

    def translate(self, dx: float, dy: float, dz: float) -> None:
        self._center = Point3D(self._center.x + dx, self._center.y + dy,
                               self._center.z + dz)

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        """绕原点旋转（rotateAround 先平移到基准点再调此处）。"""
        c, r = self._center, self._rotation
        cos, sin = math.cos(angle), math.sin(angle)
        self._center = Point3D(c.x * cos - c.y * sin,
                               c.x * sin + c.y * cos, c.z)
        self._rotation = r + angle

    def scale(self, sx: float, sy: float, sz: float) -> None:
        """绕原点缩放：等比缩放宽高，同时把中心点（绕原点）缩放到新位置。

        这样 ``BaseObject.scaleAround(k,k,k, P1)`` 的组合才正确：
        先 translate(−P1) 移到相对 P1 的局部坐标 → scale(k,k,k) 对中心和宽高都缩放 →
        再 translate(+P1) 搬回去，基准点 P1 在缩放后保持不动。"""
        self._width  *= abs(sx)
        self._height *= abs(sy)
        c = self._center
        self._center = Point3D(c.x * sx, c.y * sy, c.z * sz)

    def getBoundingBox(self) -> BoundingBox3D:
        bb = BoundingBox3D()
        for p in self.points():
            bb.expand(p)
        return bb

    def getPosition(self) -> Point3D:
        return self._center

    def clone(self) -> "ImageObject":
        obj = ImageObject()
        if self._data:
            obj.setImageData(self._data)
        obj.setGeometry(Point3D(self._center.x, self._center.y, self._center.z),
                        self._width, self._height, self._rotation)
        # 只还原基类属性（id/颜色/图层/群组/锁定…），避免 ImageObject.deserialize
        # 再把 center/width/height/rotation 覆盖回默认值 0/100/100/0。
        BaseObject.deserialize(obj, self.serializeBase())
        return obj

    def serializeBase(self) -> dict:
        return super().serialize()

    def summaryDict(self) -> dict:
        """属性面板轻量摘要：不含 base64 图片数据（避免刷屏）。"""
        d = super().serialize()
        d.update({
            "center": [round(self._center.x, 3), round(self._center.y, 3),
                       round(self._center.z, 3)],
            "width": round(self._width, 3),
            "height": round(self._height, 3),
            "rotationDeg": round(math.degrees(self._rotation), 2),
            "pixels": [self._pixelW, self._pixelH],
            "dataKB": round(len(self._data) / 1024, 1),
        })
        return d

    # ---------------- 序列化 ----------------

    def serialize(self) -> dict:
        d = super().serialize()
        d.update({
            "center": [self._center.x, self._center.y, self._center.z],
            "width": self._width,
            "height": self._height,
            "rotation": self._rotation,
            "format": self._format,
            "image": self._b64cached(),
        })
        return d

    def deserialize(self, data: dict) -> None:
        super().deserialize(data)
        c = data.get("center", [0.0, 0.0, 0.0])
        self._center = Point3D(float(c[0]), float(c[1]), float(c[2]))
        self._width = max(float(data.get("width", 100.0)), 1e-6)
        self._height = max(float(data.get("height", 100.0)), 1e-6)
        self._rotation = float(data.get("rotation", 0.0))
        self._format = data.get("format", "")
        b64 = data.get("image", "")
        if b64:
            self.setImageData(base64.b64decode(b64))

    # ---------------- 撤销快照（图片字节原样引用，免 base64 编码） ----------------

    def snapshot(self) -> dict:
        d = super().serialize()
        d.update({
            "center": [self._center.x, self._center.y, self._center.z],
            "width": self._width,
            "height": self._height,
            "rotation": self._rotation,
            "format": self._format,
            "pixels": [self._pixelW, self._pixelH],
            # bytes 不可变：快照直接共享引用（零拷贝），撤销栈持有不会失效
            "image_raw": self._data,
        })
        return d

    def restoreSnapshot(self, snap: dict) -> None:
        super().deserialize(snap)
        c = snap.get("center", [0.0, 0.0, 0.0])
        self._center = Point3D(float(c[0]), float(c[1]), float(c[2]))
        self._width = max(float(snap.get("width", 100.0)), 1e-6)
        self._height = max(float(snap.get("height", 100.0)), 1e-6)
        self._rotation = float(snap.get("rotation", 0.0))
        self._format = snap.get("format", "")
        raw = snap.get("image_raw")
        # 快照里的字节是最初 QImage 解码成功的数据，直接采用（免重新解码）
        px = snap.get("pixels") or [0, 0]
        self._data = raw if raw else b""
        self._pixelW, self._pixelH = int(px[0]), int(px[1])
        self._pixmap = None
        self._b64 = None

    def matchesSnapshot(self, snap: dict) -> bool:
        if "image_raw" not in snap:
            return self.serialize() == snap
        c = snap.get("center", [0.0, 0.0, 0.0])
        px = snap.get("pixels") or [0, 0]
        return (self._data == snap.get("image_raw")
                and self._center.x == c[0] and self._center.y == c[1]
                and self._center.z == c[2]
                and self._width == snap.get("width")
                and self._height == snap.get("height")
                and self._rotation == snap.get("rotation")
                and self._pixelW == px[0] and self._pixelH == px[1])

    def snapshotSize(self, snap: dict) -> int:
        return len(self._data) + 4096
