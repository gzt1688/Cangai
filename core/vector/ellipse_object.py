"""椭圆对象模型。"""
from __future__ import annotations
import math
from .base_object import BaseObject
from .geometry_types import Point3D, BoundingBox3D, ObjectType
from ..snap.snap_types import SnapType


class EllipseObject(BaseObject):
    def __init__(self, cx: float = 0, cy: float = 0, cz: float = 0,
                 majorAxis: float = 2.0, minorAxis: float = 1.0) -> None:
        super().__init__(ObjectType.ELLIPSE)
        self._cx = float(cx); self._cy = float(cy); self._cz = float(cz)
        self._majorAxis = float(majorAxis)
        self._minorAxis = float(minorAxis)
        self._rotation = 0.0

    def center(self) -> Point3D:
        return Point3D(self._cx, self._cy, self._cz)

    def setCenter(self, p: Point3D) -> None:
        self._cx, self._cy, self._cz = p.x, p.y, p.z

    def majorAxis(self) -> float:
        return self._majorAxis

    def minorAxis(self) -> float:
        return self._minorAxis

    def setAxes(self, major: float, minor: float) -> None:
        self._majorAxis = major
        self._minorAxis = minor

    def rotation(self) -> float:
        return self._rotation

    def setRotation(self, r: float) -> None:
        self._rotation = r

    def serialize(self) -> dict:
        d = super().serialize()
        d["center"] = [self._cx, self._cy, self._cz]
        d["majorAxis"] = self._majorAxis
        d["minorAxis"] = self._minorAxis
        d["rotation"] = self._rotation
        return d

    def deserialize(self, data: dict) -> None:
        super().deserialize(data)
        c = data.get("center", [0, 0, 0])
        self._cx, self._cy, self._cz = float(c[0]), float(c[1]), float(c[2])
        self._majorAxis = float(data.get("majorAxis", 2.0))
        self._minorAxis = float(data.get("minorAxis", 1.0))
        self._rotation = float(data.get("rotation", 0.0))

    def translate(self, dx: float, dy: float, dz: float) -> None:
        self._cx += dx; self._cy += dy; self._cz += dz

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        """绕原点旋转（rotateAround 先平移到基准点再调此处）：转中心位置 +
        累加自转角（与 ImageObject.rotate 同范式）。"""
        if (ax, ay, az) == (0, 0, 1):
            c, s = math.cos(angle), math.sin(angle)
            self._cx, self._cy = (self._cx * c - self._cy * s,
                                  self._cx * s + self._cy * c)
            self._rotation += angle

    def scale(self, sx: float, sy: float, sz: float) -> None:
        """沿世界轴缩放（sx/sy 可为负 = 镜像）。

        斜放椭圆沿世界轴缩放后一般不再是原倾角（镜像也一样：θ 要变成 -θ），
        所以不能只给轴长乘系数。用二次型精确解：椭圆 = {u: uᵀ A u = 1}，
        A = R(θ)·diag(1/a²,1/b²)·R(θ)ᵀ；缩放 S 后 A' = S⁻ᵀ·A·S⁻¹，对 A' 做
        2x2 特征分解 → 特征值给新半轴、特征向量给新倾角。
        （旧写法 `majorAxis *= abs(sx)` 完全不动 `_rotation`，导致“镜像一个
        斜放椭圆”结果与自身重合，看着就是 XY 镜像没反应。）
        """
        self._cx *= sx; self._cy *= sy; self._cz *= sz
        a, b, th = self._normalizedAxes()
        eps = 1e-12
        if b <= eps or abs(sx) <= eps or abs(sy) <= eps:
            # 退化椭圆（一轴为 0）或被压扁：只按绝对比例缩轴长，镜像时手动反倾角
            self._majorAxis *= abs(sx)
            self._minorAxis *= abs(sy)
            if (sx < 0) != (sy < 0):
                self._rotation = -self._rotation
            return
        c, s = math.cos(th), math.sin(th)
        ia, ib = 1.0 / (a * a), 1.0 / (b * b)
        m11 = (c * c * ia + s * s * ib) / (sx * sx)
        m12 = c * s * (ia - ib) / (sx * sy)
        m22 = (s * s * ia + c * c * ib) / (sy * sy)
        tr = m11 + m22
        disc = math.sqrt(max(0.0, tr * tr * 0.25 - (m11 * m22 - m12 * m12)))
        l1, l2 = tr * 0.5 - disc, tr * 0.5 + disc        # 小特征值 ↔ 长半轴
        if l1 <= eps:
            return
        if abs(m12) > eps:
            vx, vy = m12, l1 - m11                        # 长轴方向特征向量
        elif m11 <= m22:
            vx, vy = 1.0, 0.0
        else:
            vx, vy = 0.0, 1.0
        self._majorAxis = 1.0 / math.sqrt(l1)
        self._minorAxis = 1.0 / math.sqrt(l2)
        self._rotation = math.atan2(vy, vx)

    def _normalizedAxes(self) -> tuple:
        """当前椭圆的 (长半轴, 短半轴, 长轴方向角)：修正轴长写反/负值。"""
        a = abs(self._majorAxis)
        b = abs(self._minorAxis)
        th = self._rotation
        if a < b:
            a, b, th = b, a, th + math.pi / 2.0
        return a, b, th

    def getBoundingBox(self) -> BoundingBox3D:
        """真实轴对齐包围盒（**计入倾角**）：半轴按 θ 投影到 X/Y。

        不能直接用 ±major/±minor——那是“未旋转”时的范围，斜放时与图形实际
        外框差一大截，导致 XY 标注、包围盒、镜像基准、变换面板尺寸都按原始
        （未旋转）位置算。
        """
        a, b, th = self._normalizedAxes()
        c, s = math.cos(th), math.sin(th)
        hw = math.sqrt((a * c) ** 2 + (b * s) ** 2)
        hh = math.sqrt((a * s) ** 2 + (b * c) ** 2)
        bb = BoundingBox3D()
        bb.expand(Point3D(self._cx - hw, self._cy - hh, self._cz))
        bb.expand(Point3D(self._cx + hw, self._cy + hh, self._cz))
        return bb

    def getPosition(self) -> Point3D:
        return self.center()

    def clone(self) -> "EllipseObject":
        o = EllipseObject(self._cx, self._cy, self._cz, self._majorAxis, self._minorAxis)
        o._rotation = self._rotation
        o._color = self._color; o._lineWidth = self._lineWidth
        return o

    def controlPointCount(self) -> int:
        # 椭圆周 8 等分控制点（与 toPolycurve 采样一致）：选中即显示全部控制点；
        # 中心无控制点，点击中心仍是"选中对象"而非编辑，不会误破坏椭圆
        return 8

    def getControlPoint(self, i: int) -> Point3D:
        t = 2.0 * math.pi * i / 8
        c = math.cos(self._rotation)
        s = math.sin(self._rotation)
        ex = self._majorAxis * math.cos(t)
        ey = self._minorAxis * math.sin(t)
        return Point3D(self._cx + ex * c - ey * s,
                       self._cy + ex * s + ey * c, self._cz)

    def setControlPoint(self, i: int, p: Point3D) -> None:
        # 兜底（编辑前会先转自由曲线）：按拖动距离与当前控制点半径的比例缩放两轴，
        # 保持椭圆形状比例
        cur = self.getControlPoint(i)
        r_cur = math.hypot(cur.x - self._cx, cur.y - self._cy)
        r_new = math.hypot(p.x - self._cx, p.y - self._cy)
        if r_cur > 1e-12:
            f = max(0.0, r_new / r_cur)
            self._majorAxis *= f
            self._minorAxis *= f

    def toPolycurve(self):
        """转为闭合采样曲线：椭圆参数公式精确采样（首尾重合），控制点仍为 8 等分点。

        不经 catmullRom 逼近——catmullRom 是开放曲线端点处理（首段 p0、末段
        p3 用重复端点），对首尾重合的闭合椭圆会在接缝处切线方向错误、采样点
        形成尖角（偏移/浮雕后首尾点凸起）。精确采样保证每点严格落在椭圆周上。
        """
        from .poly_curve_object import PolyCurveObject
        poly = PolyCurveObject()
        c = math.cos(self._rotation)
        s = math.sin(self._rotation)
        # 外观采样：椭圆周 n_sample 等分精确点（闭合，首尾重合）
        n_sample = 128
        pts = []
        for i in range(n_sample + 1):
            t = 2.0 * math.pi * i / n_sample
            ex = self._majorAxis * math.cos(t)
            ey = self._minorAxis * math.sin(t)
            pts.append([self._cx + ex * c - ey * s,
                        self._cy + ex * s + ey * c, self._cz])
        # 编辑控制点：8 椭圆周等分点（首尾重合，与 controlPointCount 一致）
        cps = []
        for i in range(8 + 1):
            t = 2.0 * math.pi * i / 8
            ex = self._majorAxis * math.cos(t)
            ey = self._minorAxis * math.sin(t)
            cps.append([self._cx + ex * c - ey * s,
                        self._cy + ex * s + ey * c, self._cz])
        poly.addSegment({"points": pts, "inputPoints": cps})
        return poly

    def getSupportedSnapTypes(self):
        return [SnapType.CENTER, SnapType.QUARTER, SnapType.NEAREST,
                SnapType.INTERSECTION]

    def pathLength(self) -> float:
        # Ramanujan 周长近似，误差约 0.04%
        a = max(abs(self._majorAxis), abs(self._minorAxis))
        b = min(abs(self._majorAxis), abs(self._minorAxis))
        if b <= 0.0:
            return 0.0
        h = ((a - b) / (a + b)) ** 2
        return math.pi * (a + b) * (1.0 + 3.0 * h / (10.0 + math.sqrt(4.0 - 3.0 * h)))

    def enclosedArea(self) -> float | None:
        return math.pi * abs(self._majorAxis) * abs(self._minorAxis)