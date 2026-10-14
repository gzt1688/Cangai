"""尺寸标注对象（DIMENSION）：独立的 2D 工程标注，归入“标注”固定层。

两种样式：
- kind="xy"  ：对一块世界 AABB 区域直接标注 X 宽 / Y 高。默认 X 宽尺寸线在
  被测区域下方、Y 高尺寸线在左侧，两个子标注可自由穿过被测区域翻越到对侧
  （X 宽可到上方、Y 高可到右侧）；读数文字始终自动摆到远离被测区域的方位
  （标注在下→文字在下、在上→文字在上、在左→文字在左、在右→文字在右）。
  区域通常来自被选中对象的联合包围盒。
- kind="linear"：标注两个点之间的对齐尺寸（任意方向，尺寸线平行于连线并
  沿法向偏移，自动带延伸线/箭头/距离读数）。

标注文字以 QPainter 矢量字形渲染（由 2D 图元负责绘制），对象本身只保存
“世界坐标 + 样式”纯数据：被测区域（矩形或两点）、字高（世界单位）、文字
与图形的净空。读数文本 = 数字 + 单位后缀（后缀来自设置 → 通用 → 标注）。
translate/rotate/scale 均作用于被测几何，文字样式默认不变；绑定中的 XY
标注禁止自身几何变换（被测矩形完全由绑定源物体决定，随物体固定相对跟随，
避免与物体一起被缩放时越缩越偏）。变换面板“增大/缩小尺寸”只放大读数
文字与随字号绘制的图形，已摆放的尺寸线保持原位（rescaleText）。

数据供 2D 图元/属性面板直接消费：layout() 一次性给出全部绘制几何
（线条/箭头/文本），getBoundingBox() 覆盖全部图形与文字估算范围。
"""
from __future__ import annotations
import math

from core import app_settings
from .base_object import BaseObject
from .geometry_types import Point3D, BoundingBox3D, ObjectType

# 字号相关比例（相对文字世界高度 fs 的系数）
_T_EXT = 0.30        # 延伸线超出尺寸线的长度
_T_LINE_GAP = 0.18   # 读数文字与尺寸线的净空
_T_ARROW_LEN = 0.55  # 箭头长度
_T_ARROW_HW = 0.18   # 箭头半宽
_T_CHAR_W = 0.62     # 数字字形平均宽高比（估算 bbox 用）
_TEXT_TO_OBJ_GAP = 0.9   # 读数文字与对象轮廓净空系数（相对 fs，创建时 gap=fs*此系数）


def formatLength(v: float) -> str:
    """标注数字格式：≥100 无小数、≥10 一位小数、其余两位。"""
    a = abs(v)
    if a >= 999.5:
        return f"{v:.0f}"
    if a >= 99.95:
        return f"{v:.1f}"
    return f"{v:.2f}"


def _unitSuffix() -> str:
    """读数字单位后缀（来自设置 → 通用 → 标注；为空则不追加）。"""
    return getattr(app_settings, "dim_unit", "").strip()


class DimensionObject(BaseObject):
    def __init__(self, kind: str = "linear") -> None:
        super().__init__(ObjectType.DIMENSION)
        self._kind = kind if kind in ("xy", "linear", "z") else "linear"
        # xy：被测世界 AABB（已标准化 x0<=x1, y0<=y1）
        self._rect = (0.0, 0.0, 0.0, 0.0)
        self._hasRect = False
        # linear：被测两点
        self._p1 = Point3D()
        self._p2 = Point3D()
        self._hasPoints = False
        # z：被测对象包围盒 (x0,y0,x1,y1,z0,z1)，标注 Z 高度（3D 模式专用）
        self._zBox = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        self._hasZBox = False
        self._zOff = 0.0           # 尺寸线沿 X 离对象右侧的偏移（可拖动调位置）
        self._fontH = 1.0      # 文字世界高度
        self._gap = 0.9        # 标注与被测轮廓净空（世界单位）
        # xy 两个子标注的自由摆放量（世界单位，可正可负，无钳制）：
        #   _xyOffX：Y 高标注（竖）沿 X 的偏移量。默认 0 即已落在区域左侧；
        #            拖拽读数向左（ox 增大）更远离区域，向右拖到 ox 为足够
        #            负值时子标注穿过被测区域翻越到右侧；
        #   _xyOffY：X 宽标注（横）沿 Y 的偏移量。默认 0 即已落在区域下方；
        #            拖拽读数向下（oy 增大）更远离区域，向上拖到 oy 为足够
        #            负值时子标注穿过被测区域翻越到上方。
        # 两个值均不设下限：子标注可自由穿过被测区域放到任意一侧，绘制时
        # 按尺寸线相对被测区域的位置自动把读数文字摆到远离区域的那一侧。
        self._xyOffX = 0.0
        self._xyOffY = 0.0
        # 物体绑定：XY 标注默认与其创建时选中的源对象绑定，源对象几何变化
        # （尺寸/移动/旋转…）时由文档层跟随刷新被测区域；用户可解除绑定，
        # 解除后脱离关系且不可恢复（数据源即目标 id 列表，清空即永久解除）。
        self._boundIds: list = []

    # ---------------- 配置 ----------------

    def kind(self) -> str:
        return self._kind

    def setXYRect(self, x0: float, y0: float, x1: float, y1: float,
                  fontH: float, gap: float) -> None:
        """以一块世界 AABB（通常为多选对象联合包围盒）建立 XY 标注。"""
        self._kind = "xy"
        self._fontH = max(float(fontH), 1e-3)
        self._gap = max(float(gap), 0.0)
        self._rect = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        self._hasRect = True
        self._hasPoints = False

    def setLinear(self, p1: Point3D, p2: Point3D, fontH: float, gap: float) -> None:
        """以两点建立对齐（线性）标注。"""
        self._kind = "linear"
        self._fontH = max(float(fontH), 1e-3)
        self._gap = max(float(gap), 0.0)
        self._p1 = Point3D(p1.x, p1.y, 0.0)
        self._p2 = Point3D(p2.x, p2.y, 0.0)
        self._hasPoints = True
        self._hasRect = False

    def setZRange(self, x0: float, y0: float, x1: float, y1: float,
                  z0: float, z1: float, fontH: float, gap: float) -> None:
        """以被测对象包围盒建立 Z 标注（3D 模式专用，标注 Z 高度）。"""
        self._kind = "z"
        self._fontH = max(float(fontH), 1e-3)
        self._gap = max(float(gap), 0.0)
        self._zBox = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1),
                      min(z0, z1), max(z0, z1))
        self._hasZBox = True

    def fontHeight(self) -> float:
        return self._fontH

    def gap(self) -> float:
        return self._gap

    def rescaleText(self, factor: float) -> None:
        """只放大/缩小读数文字与箭头等随字号绘制的图形（“增大/缩小尺寸”按钮用）。

        XY 标注的两条尺寸线保持世界位置不动——即标注相对被测物体的
        摆放位置（含用户手动拖到的任意一侧）不随字号变化，不会越调越跑。
        linear/Z 标注整体等比缩放（无独立摆放量）。
        """
        f = max(float(factor), 1e-6)
        fsOld = self._fontH
        self._fontH = max(self._fontH * f, 1e-3)
        if self._kind == "xy":
            # base = gap + 0.5*fs 中 fs 增大让尺寸线有外移趋势，反向调整
            # 两个偏移量抵消之，使 yL/xL（尺寸线位置）保持不变。
            d = 0.5 * (self._fontH - fsOld)
            self._xyOffX -= d
            self._xyOffY -= d
        else:
            self._gap = max(self._gap * f, 0.0)

    # ---------------- XY 子标注单独外拉 ----------------

    def xyOffsets(self) -> tuple:
        """XY 两个子标注的摆放量：(Y 高标注沿 X 的偏移量, X 宽标注沿 Y 的偏移量)。

        可正可负、无钳制：默认 0 已使标注落于被测区域左侧/下方，拖向负值
        方向可穿过区域翻越到右侧/上方。linear 标注无此概念，恒为 (0, 0)。
        """
        if self._kind != "xy":
            return (0.0, 0.0)
        return (self._xyOffX, self._xyOffY)

    def setXYOffset(self, ox: float, oy: float) -> None:
        """自由摆放 XY 子标注（不再限制在物体外）：
        X 宽标注（横）可沿 Y 上下拖动、Y 高标注（竖）可沿 X 左右拖动。

        两个偏移均不设下限——拖动子标注可自由穿过被测区域翻越到对侧
        （X 宽翻到区域上方、Y 高翻到区域右侧）；绘制端会按尺寸线相对
        被测区域的位置自动把读数文字摆到远离区域的那一侧。
        """
        if self._kind != "xy":
            return
        self._xyOffX = float(ox)
        self._xyOffY = float(oy)

    def zOffset(self) -> float:
        """Z 标注尺寸线沿 X 离对象右侧的额外偏移（可拖动调位置）。"""
        return self._zOff if self._kind == "z" else 0.0

    def setZOffset(self, off: float) -> None:
        """设置 Z 标注尺寸线沿 X 的额外偏移。"""
        if self._kind != "z":
            return
        self._zOff = float(off)

    # ---------------- 物体绑定（仅 XY 标注） ----------------

    def boundTargetIds(self) -> list:
        """当前绑定的源对象 id 列表（空 = 已解除绑定/从未绑定）。
        XY/Z 标注创建时自动记录被测对象；linear 标注恒为空。"""
        if self._kind not in ("xy", "z"):
            return []
        return list(self._boundIds)

    def bindTo(self, ids) -> None:
        """记录绑定的源对象 id 列表（仅 XY/Z；去重、按给定顺序）。"""
        if self._kind not in ("xy", "z"):
            return
        seen = []
        for i in ids:
            if i and i not in seen:
                seen.append(i)
        self._boundIds = seen

    def unbind(self) -> None:
        """解除与物体的绑定（永久脱离：目标列表被清空，无法重新绑定）。"""
        self._boundIds = []

    def followTargetBounds(self, targets) -> bool:
        """绑定跟随：按现存绑定源对象的联合包围盒重设被测 XY 区域。

        保留字号/净空；默认位于区域下方/左侧的子标注保持与相应被测边的
        距离不变（可正可负）。子标注若已被用户翻越到上方/右侧，则随区域
        的尺寸变化同步平移，保持其与被测边之间的净空——物体变大时标注
        不会被“吞”进物体内部。返回是否实际发生变化。所有源对象均无
        有效包围盒时不改动（标注原地保留）。
        """
        if self._kind not in ("xy", "z"):
            return False
        bb = BoundingBox3D()
        for t in targets:
            try:
                b = t.getBoundingBox()
            except Exception:
                continue
            if b is not None and b.isValid():
                bb.expand(b.min)
                bb.expand(b.max)
        if not bb.isValid():
            return False
        if self._kind == "z":
            nz = (bb.min.x, bb.min.y, bb.max.x, bb.max.y, bb.min.z, bb.max.z)
            if all(abs(a - b) < 1e-9 for a, b in zip(self._zBox, nz)):
                return False
            self._zBox = nz
            return True
        x0, y0, x1, y1 = self._rect
        nx0, ny0, nx1, ny1 = bb.min.x, bb.min.y, bb.max.x, bb.max.y
        eps = 1e-9
        if (abs(x0 - nx0) < eps and abs(y0 - ny0) < eps
                and abs(x1 - nx1) < eps and abs(y1 - ny1) < eps):
            return False
        dx = (nx1 - nx0) - (x1 - x0)     # 被测区域宽度变化量
        dy = (ny1 - ny0) - (y1 - y0)     # 高度变化量
        fs = self._fs()
        base = self._gap + fs * 0.5
        # 翻越到上方/右侧的子标注：随区域的尺寸增量平移，保持外侧净空
        yL = y0 - (base + self._xyOffY)
        if yL > y1 + eps:
            self._xyOffY -= dy
        xL = x0 - (base + self._xyOffX)
        if xL > x1 + eps:
            self._xyOffX -= dx
        self._rect = (nx0, ny0, nx1, ny1)
        return True

    def rect(self):
        return self._rect if self._hasRect else None

    def endpoints(self):
        if self._kind == "linear" and self._hasPoints:
            return (self._p1, self._p2)
        return None

    def measureValue(self) -> float | None:
        """当前读数值（XY→宽/高并列展示在内部）；linear→两点距离；z→Z 高度。"""
        if self._kind == "xy" and self._hasRect:
            x0, y0, x1, y1 = self._rect
            return max(abs(x1 - x0), abs(y1 - y0))
        if self._kind == "z" and self._hasZBox:
            return abs(self._zBox[5] - self._zBox[4])
        if self._kind == "linear" and self._hasPoints:
            return math.hypot(self._p2.x - self._p1.x, self._p2.y - self._p1.y)
        return None

    def hasGeometry(self) -> bool:
        if self._kind == "xy":
            return self._hasRect
        if self._kind == "z":
            return self._hasZBox
        return self._hasPoints

    # ---------------- 几何生成 ----------------

    def _fs(self) -> float:
        return max(self._fontH, 1e-6)

    def _style(self) -> dict:
        fs = self._fs()
        return {
            "fs": fs,
            "ext": fs * _T_EXT,
            "lineGap": fs * _T_LINE_GAP,
            "arrowLen": fs * _T_ARROW_LEN,
            "arrowHW": fs * _T_ARROW_HW,
        }

    def layout(self) -> dict:
        """计算全部标注绘制几何（纯世界坐标数据）。

        返回: {lines: [(x1,y1,x2,y2)...], fills: [[(x,y)...]箭头三角...],
                texts: [{cx,cy,text,angle}]}  无几何时 lines 为空。
        angle 为文本“世界旋转角”（度，逆时针为正）。
        """
        out = {"lines": [], "fills": [], "texts": []}
        if not self.hasGeometry():
            return out
        st = self._style()
        gap = self._gap
        if self._kind == "xy":
            self._layoutXY(out, st, gap)
        elif self._kind == "z":
            self._layoutZ(out, st, gap)
        else:
            fs = st["fs"]
            # 尺寸线整体离被测轮廓的距离 = 文字净空 + 一行文字 + 文字与线净空
            D = gap + fs + st["lineGap"]
            self._layoutLinear(out, st, D)
        return out

    def _fmt(self, v: float) -> str:
        """读数文本：数字 + 单位后缀（设置→通用→标注；后缀为空则纯数字）。"""
        return formatLength(v) + _unitSuffix()

    def _addText(self, out, cx: float, cy: float, text: str,
                 worldAngle: float) -> None:
        out["texts"].append({"cx": cx, "cy": cy, "text": text,
                             "angle": worldAngle})

    def _layoutXY(self, out: dict, st: dict, gap: float) -> None:
        x0, y0, x1, y1 = self._rect
        w = x1 - x0
        h = y1 - y0
        fs = st["fs"]
        ah, awh = st["arrowLen"], st["arrowHW"]
        # 尺寸线离被测区域轮廓的默认距离：轮廓净空 + 半行字（0.5fs）。
        # X/Y 子标注经 setXYOffset 可自由移动（不设下限，允许穿过被测区域
        # 翻越到对侧）；读数文字始终排在尺寸线“远离被测区域”的一侧——
        # 标注在下/左时文字在下/左，穿过区域翻到上/右后文字自动跟到上/右。
        base = gap + fs * 0.5

        # ---- X 宽：可沿 Y 上下自由拖动，翻到区域上方时文字自动在上 ----
        if w > 1e-6:
            yL = y0 - (base + self._xyOffY)   # 尺寸线 Y
            below = yL <= (y0 + y1) * 0.5     # 尺寸线处于区域下半/下方：按下方布置
            edgeY = y0 if below else y1       # 延伸线接离标注最近的那条被测边
            out["lines"].append((x0, edgeY, x0, yL))
            out["lines"].append((x1, edgeY, x1, yL))
            out["lines"].append((x0, yL, x1, yL))
            # 箭头：尖端在两端延伸线处，开口朝内
            out["fills"].append([(x0, yL), (x0 + ah, yL - awh), (x0 + ah, yL + awh)])
            out["fills"].append([(x1, yL), (x1 - ah, yL - awh), (x1 - ah, yL + awh)])
            cx = (x0 + x1) * 0.5
            if below:
                cy = yL - (st["lineGap"] + fs * 0.5)   # 文字行中心（尺寸线下方）
            else:
                cy = yL + (st["lineGap"] + fs * 0.5)   # 文字行中心（尺寸线上方）
            self._addText(out, cx, cy, self._fmt(w), 0.0)

        # ---- Y 高：可沿 X 左右自由拖动，翻到区域右侧时文字自动在右 ----
        if h > 1e-6:
            xL = x0 - (base + self._xyOffX)   # 尺寸线 X
            left = xL <= (x0 + x1) * 0.5      # 尺寸线处于区域左半/左侧：按左侧布置
            edgeX = x0 if left else x1        # 延伸线接离标注最近的那条被测边
            out["lines"].append((edgeX, y0, xL, y0))
            out["lines"].append((edgeX, y1, xL, y1))
            out["lines"].append((xL, y0, xL, y1))
            out["fills"].append([(xL, y0), (xL - awh, y0 + ah), (xL + awh, y0 + ah)])
            out["fills"].append([(xL, y1), (xL - awh, y1 - ah), (xL + awh, y1 - ah)])
            if left:
                cx = xL - (st["lineGap"] + fs * 0.5)   # 文字列中心（尺寸线左侧）
            else:
                cx = xL + (st["lineGap"] + fs * 0.5)   # 文字列中心（尺寸线右侧）
            cy = (y0 + y1) * 0.5
            # 竖排：读数沿 Y 轴方向，逆时针 90° 使顶部朝上（顺着高度方向读）
            self._addText(out, cx, cy, self._fmt(h), 90.0)

    def _layoutZ(self, out: dict, st: dict, gap: float) -> None:
        """Z 标注：3D 竖直尺寸线标注被测对象 Z 高度。

        线条; fills 用 3D 点 (x,y,z)；texts 带 cz。
        尺寸线在对象右侧 (x1+gap+zOff, yc)，从 z0 到 z1 竖直。
        """
        x0, y0, x1, y1, z0, z1 = self._zBox
        zh = z1 - z0
        if zh < 1e-6:
            return
        fs = st["fs"]
        ah, awh = st["arrowLen"], st["arrowHW"]
        yc = (y0 + y1) * 0.5
        xL = x1 + gap + self._zOff          # 尺寸线 X 位置
        # 延伸线（水平，从对象右边到尺寸线）
        out["lines"].append((x1, yc, z0, xL, yc, z0))
        out["lines"].append((x1, yc, z1, xL, yc, z1))
        # 尺寸线（竖直）
        out["lines"].append((xL, yc, z0, xL, yc, z1))
        # 箭头（水平开口朝内，在尺寸线两端）
        out["fills"].append([(xL, yc, z0), (xL - ah, yc, z0 - awh), (xL - ah, yc, z0 + awh)])
        out["fills"].append([(xL, yc, z1), (xL - ah, yc, z1 - awh), (xL - ah, yc, z1 + awh)])
        # 读数文字（尺寸线右侧，竖排顺着 Z 方向读）
        cx = xL + (st["lineGap"] + fs * 0.5)
        cz = (z0 + z1) * 0.5
        out["texts"].append({"cx": cx, "cy": yc, "cz": cz,
                             "text": self._fmt(zh), "angle": 90.0})

    def _layoutLinear(self, out: dict, st: dict, D: float) -> None:
        p1, p2 = self._p1, self._p2
        dx = p2.x - p1.x
        dy = p2.y - p1.y
        L = math.hypot(dx, dy)
        if L < 1e-9:
            return
        ux, uy = dx / L, dy / L
        nx, ny = -uy, ux                 # 法向（相对连线朝左）
        fs = st["fs"]
        ah, awh = st["arrowLen"], st["arrowHW"]

        ax, ay = p1.x + nx * D, p1.y + ny * D
        bx, by = p2.x + nx * D, p2.y + ny * D
        # 延伸线 + 尺寸线
        out["lines"].append((p1.x, p1.y, ax, ay))
        out["lines"].append((p2.x, p2.y, bx, by))
        out["lines"].append((ax, ay, bx, by))
        # 箭头开口朝内
        out["fills"].append([(ax, ay),
                             (ax + ux * ah - nx * awh, ay + uy * ah - ny * awh),
                             (ax + ux * ah + nx * awh, ay + uy * ah + ny * awh)])
        out["fills"].append([(bx, by),
                             (bx - ux * ah - nx * awh, by - uy * ah - ny * awh),
                             (bx - ux * ah + nx * awh, by - uy * ah + ny * awh)])
        # 读数居中于尺寸线外侧
        cx = (ax + bx) * 0.5 + nx * (st["lineGap"] + fs * 0.5)
        cy = (ay + by) * 0.5 + ny * (st["lineGap"] + fs * 0.5)
        ang = math.degrees(math.atan2(dy, dx))
        if ang > 90.0:
            ang -= 180.0
        elif ang < -90.0:
            ang += 180.0
        self._addText(out, cx, cy, self._fmt(L), ang)

    def _textExtent(self, t: dict):
        """文本粗略世界范围（估算字号与字符数）。返回以中心旋转后的
        (minx, miny, maxx, maxy)；angle 取文本世界角。"""
        fs = self._fs()
        w = max(len(t["text"]) * fs * _T_CHAR_W, fs)
        h = fs
        a = math.radians(t["angle"])
        ca, sa = math.cos(a), math.sin(a)
        hw, hh = w * 0.5, h * 0.5
        pts = []
        for lx, ly in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
            rx = t["cx"] + lx * ca - ly * sa
            ry = t["cy"] + lx * sa + ly * ca
            pts.append((rx, ry))
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    def textBounds(self) -> list:
        """各读数文字的粗略世界包围盒（含旋转），供命中/框选使用。"""
        lay = self.layout()
        return [self._textExtent(t) for t in lay["texts"]]

    # ---------------- 对象标准接口 ----------------

    def points(self) -> list:
        """布局整体外框四角（供框选/3D 投影兼容）。"""
        bb = self.getBoundingBox()
        if not bb.isValid():
            return []
        return [Point3D(bb.min.x, bb.min.y, bb.min.z),
                Point3D(bb.max.x, bb.min.y, bb.min.z),
                Point3D(bb.max.x, bb.max.y, bb.max.z),
                Point3D(bb.min.x, bb.max.y, bb.max.z)]

    def getBoundingBox(self) -> BoundingBox3D:
        bb = BoundingBox3D()
        lay = self.layout()
        for seg in lay["lines"]:
            if len(seg) == 6:
                x1, y1, z1, x2, y2, z2 = seg
                bb.expand(Point3D(x1, y1, z1))
                bb.expand(Point3D(x2, y2, z2))
            else:
                x1, y1, x2, y2 = seg
                bb.expand(Point3D(x1, y1, 0.0))
                bb.expand(Point3D(x2, y2, 0.0))
        for tri in lay["fills"]:
            for p in tri:
                if len(p) == 3:
                    bb.expand(Point3D(p[0], p[1], p[2]))
                else:
                    bb.expand(Point3D(p[0], p[1], 0.0))
        for t in lay["texts"]:
            cz = t.get("cz", 0.0)
            x0, y0, x1, y1 = self._textExtent(t)
            bb.expand(Point3D(x0, y0, cz))
            bb.expand(Point3D(x1, y1, cz))
        return bb

    def getPosition(self) -> Point3D:
        bb = self.getBoundingBox()
        if bb.isValid():
            return bb.center()
        if self._kind == "linear" and self._hasPoints:
            return Point3D((self._p1.x + self._p2.x) * 0.5,
                           (self._p1.y + self._p2.y) * 0.5, 0.0)
        return Point3D()

    # ---------------- 变换 ----------------

    def translate(self, dx: float, dy: float, dz: float) -> None:
        if self._kind == "xy" and self._hasRect:
            if self._boundIds:
                return   # 绑定标注：被测矩形完全由绑定源物体决定，禁止单独平移
            x0, y0, x1, y1 = self._rect
            self._rect = (x0 + dx, y0 + dy, x1 + dx, y1 + dy)
        elif self._kind == "z" and self._hasZBox:
            if self._boundIds:
                return   # 绑定 Z 标注：被测包围盒由绑定源物体决定，禁止单独平移
            x0, y0, x1, y1, z0, z1 = self._zBox
            self._zBox = (x0 + dx, y0 + dy, x1 + dx, y1 + dy,
                          z0 + dz, z1 + dz)
        elif self._hasPoints:
            self._p1 = Point3D(self._p1.x + dx, self._p1.y + dy, self._p1.z + dz)
            self._p2 = Point3D(self._p2.x + dx, self._p2.y + dy, self._p2.z + dz)

    def _rotPt(self, p: Point3D, c: float, s: float) -> Point3D:
        return Point3D(p.x * c - p.y * s, p.x * s + p.y * c, p.z)

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        """绕原点（绕 Z）旋转被测几何；标注文字方向由绘制按新几何重算，
        字号与净空不变。XY 区域旋转后重新标准化为 AABB。绑定中的 XY/Z 标注
        禁止自身旋转——几何始终跟随绑定源物体。"""
        if self._kind == "xy" and self._boundIds:
            return
        if self._kind == "z" and self._boundIds:
            return
        c, s = math.cos(angle), math.sin(angle)
        if self._kind == "xy" and self._hasRect:
            x0, y0, x1, y1 = self._rect
            pts = [self._rotPt(Point3D(x, y, 0.0), c, s)
                   for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
            xs = [p.x for p in pts]
            ys = [p.y for p in pts]
            self._rect = (min(xs), min(ys), max(xs), max(ys))
        elif self._kind == "z" and self._hasZBox:
            # Z 标注：绕 Z 轴旋转 XY 足迹（包围盒矩形），Z 范围不变
            x0, y0, x1, y1, z0, z1 = self._zBox
            pts = [self._rotPt(Point3D(x, y, 0.0), c, s)
                   for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
            xs = [p.x for p in pts]
            ys = [p.y for p in pts]
            self._zBox = (min(xs), min(ys), max(xs), max(ys), z0, z1)
        elif self._hasPoints:
            self._p1 = self._rotPt(self._p1, c, s)
            self._p2 = self._rotPt(self._p2, c, s)

    def scale(self, sx: float, sy: float, sz: float) -> None:
        """缩放被测几何。绑定中的 XY/Z 标注禁止自身缩放——几何始终跟随绑定
        源物体（避免与物体一起被缩放时被测区域脱离物体的包围盒、越缩越偏）。"""
        if self._kind == "xy" and self._boundIds:
            return
        if self._kind == "z" and self._boundIds:
            return
        if self._kind == "xy" and self._hasRect:
            x0, y0, x1, y1 = self._rect
            xs = sorted((x0 * sx, x1 * sx))
            ys = sorted((y0 * sy, y1 * sy))
            self._rect = (xs[0], ys[0], xs[1], ys[1])
        elif self._kind == "z" and self._hasZBox:
            x0, y0, x1, y1, z0, z1 = self._zBox
            xs = sorted((x0 * sx, x1 * sx))
            ys = sorted((y0 * sy, y1 * sy))
            zs = sorted((z0 * sz, z1 * sz))
            self._zBox = (xs[0], ys[0], xs[1], ys[1], zs[0], zs[1])
        elif self._hasPoints:
            self._p1 = Point3D(self._p1.x * sx, self._p1.y * sy, self._p1.z * sz)
            self._p2 = Point3D(self._p2.x * sx, self._p2.y * sy, self._p2.z * sz)

    def clone(self) -> "DimensionObject":
        obj = DimensionObject(self._kind)
        obj.deserialize(self.serialize())
        return obj

    # ---------------- 序列化 ----------------

    def serialize(self) -> dict:
        d = super().serialize()
        d["kind"] = self._kind
        d["fontH"] = self._fontH
        d["gap"] = self._gap
        if self._kind == "xy" and self._hasRect:
            d["rect"] = list(self._rect)
            d["xOff"] = self._xyOffX
            d["yOff"] = self._xyOffY
            d["bind"] = list(self._boundIds)
        elif self._kind == "z" and self._hasZBox:
            d["zBox"] = list(self._zBox)
            d["zOff"] = self._zOff
            d["bind"] = list(self._boundIds)
        elif self._kind == "linear" and self._hasPoints:
            d["p1"] = [self._p1.x, self._p1.y, self._p1.z]
            d["p2"] = [self._p2.x, self._p2.y, self._p2.z]
        return d

    def deserialize(self, data: dict) -> None:
        super().deserialize(data)
        kind = data.get("kind", self._kind)
        self._kind = kind if kind in ("xy", "linear", "z") else "linear"
        self._fontH = max(float(data.get("fontH", self._fontH)), 1e-3)
        self._gap = max(float(data.get("gap", self._gap)), 0.0)
        if self._kind == "xy":
            r = data.get("rect")
            if r:
                self._rect = (float(r[0]), float(r[1]), float(r[2]), float(r[3]))
                self._hasRect = True
                self.setXYOffset(float(data.get("xOff", 0.0)),
                                 float(data.get("yOff", 0.0)))
                b = data.get("bind")
                self._boundIds = [str(x) for x in b] if isinstance(b, list) else []
        elif self._kind == "z":
            zb = data.get("zBox")
            if zb and len(zb) >= 6:
                self._zBox = tuple(float(v) for v in zb[:6])
                self._hasZBox = True
                self._zOff = float(data.get("zOff", 0.0))
                b = data.get("bind")
                self._boundIds = [str(x) for x in b] if isinstance(b, list) else []
        else:
            p1 = data.get("p1")
            p2 = data.get("p2")
            if p1 and p2:
                self._p1 = Point3D(float(p1[0]), float(p1[1]),
                                   float(p1[2]) if len(p1) > 2 else 0.0)
                self._p2 = Point3D(float(p2[0]), float(p2[1]),
                                   float(p2[2]) if len(p2) > 2 else 0.0)
                self._hasPoints = True

    def summaryDict(self) -> dict:
        d = super().serialize()
        d["kind"] = self._kind
        if self._kind == "xy":
            x0, y0, x1, y1 = self._rect
            d["X"] = self._fmt(x1 - x0)
            d["Y"] = self._fmt(y1 - y0)
            d["字高"] = f"{self._fontH:.3f}"
        elif self._kind == "z":
            z0, z1 = self._zBox[4], self._zBox[5]
            d["Z"] = self._fmt(z1 - z0)
            d["字高"] = f"{self._fontH:.3f}"
        else:
            d["距离"] = self._fmt(self.measureValue() or 0.0)
        return d
