"""重叠拾取菜单：鼠标处命中多条曲线时弹出候选列表（仿 Rhino 的 selection cycling）。

只把"曲线类"对象送进候选菜单：点/实体/图片/标注不参与，避免浮雕模式下每次
点选叠加在模型上的曲线都弹菜单。2D 画布与 3D 视口共用本模块。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QMenu

from core.vector.geometry_types import ObjectType, polylineLength
from i18n import tr

# 参与重叠候选的曲线类对象
CURVE_TYPES = frozenset({
    ObjectType.LINE, ObjectType.CURVE, ObjectType.NURBS_CURVE,
    ObjectType.POLYCURVE, ObjectType.CIRCLE, ObjectType.ARC,
    ObjectType.ELLIPSE, ObjectType.RECTANGLE, ObjectType.REVCLOUD,
})

_TYPE_LABELS = {
    ObjectType.LINE: "直线",
    ObjectType.CURVE: "曲线",
    ObjectType.NURBS_CURVE: "NURBS 曲线",
    ObjectType.POLYCURVE: "多段线",
    ObjectType.CIRCLE: "圆",
    ObjectType.ARC: "圆弧",
    ObjectType.ELLIPSE: "椭圆",
    ObjectType.RECTANGLE: "矩形",
    ObjectType.REVCLOUD: "修订云线",
}


def isCurveLike(obj) -> bool:
    """对象是否属于"重叠曲线候选"范围。"""
    try:
        return obj is not None and obj.type() in CURVE_TYPES
    except Exception:
        return False


# 天然闭合的类型（名称已是最终形态，不再加"闭合"前缀）
_CLOSED_TYPES = frozenset({ObjectType.CIRCLE, ObjectType.ELLIPSE,
                           ObjectType.RECTANGLE, ObjectType.REVCLOUD})


def _isClosed(obj) -> bool:
    try:
        if obj.type() in _CLOSED_TYPES:
            return True
        fn = getattr(obj, "isClosed", None)
        return bool(fn()) if callable(fn) else False
    except Exception:
        return False


def _baseTypeLabel(obj) -> str:
    """按几何实际形态取名：多顶点的 LINE 是本程序里的"多段线"，不是直线。"""
    t = obj.type()
    if t == ObjectType.LINE:
        try:
            n = len(obj.points())
        except Exception:
            n = 2
        return tr("多段线") if n > 2 else tr("直线")
    return tr(_TYPE_LABELS.get(t, "曲线"))


def curveTypeLabel(obj) -> str:
    base = _baseTypeLabel(obj)
    # 闭合的曲线要显式标"闭合"，否则用户选中闭合曲线却看到"直线"会困惑
    if _isClosed(obj) and obj.type() not in _CLOSED_TYPES:
        return tr("闭合") + base
    return base


def curveLength(obj) -> float | None:
    """曲线长度（mm）；无法计算返回 None。仅用于候选菜单的区分信息。"""
    try:
        if hasattr(obj, "getSampledPoints"):
            pts = obj.getSampledPoints()
            if len(pts) >= 2:
                return polylineLength(pts)
        if obj.type() == ObjectType.LINE:
            pts = obj.points()
            if len(pts) >= 2:
                total = polylineLength(pts)
                if obj.isClosed():
                    total += ((pts[0].x - pts[-1].x) ** 2 +
                              (pts[0].y - pts[-1].y) ** 2 +
                              (pts[0].z - pts[-1].z) ** 2) ** 0.5
                return total
        if hasattr(obj, "toPolycurve"):
            total = 0.0
            for seg in obj.toPolycurve().segments():
                total += polylineLength(seg.get("points", []))
            return total
    except Exception:
        return None
    return None


def _label(obj) -> str:
    text = curveTypeLabel(obj)
    length = curveLength(obj)
    if length is not None and length > 1e-9:
        text += f" · {length:.1f} mm"
    return text


def buildCandidatesMenu(parent, objects) -> QMenu:
    """构造候选菜单：每项左侧带序号，项间加分割线。"""
    labels = [_label(o) for o in objects]
    # 几何完全重合的副本（类型/长度都一样）补短 id 以便区分
    for i, text in enumerate(labels):
        if labels.count(text) > 1:
            labels[i] = f"{text}  #{objects[i].id()[:4]}"

    menu = QMenu(parent)
    menu.setToolTipsVisible(True)
    # 弹出窗口默认是矩形原生背景：全局 QSS 的 8px 圆角只在窗口内绘制，圆角
    # 之外的四个角会露出白色尖角。开透明背景 + 无边框才真正只剩圆角面板。
    menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    menu.setWindowFlags(menu.windowFlags()
                        | Qt.WindowType.FramelessWindowHint
                        | Qt.WindowType.NoDropShadowWindowHint)
    # 文字靠左：全局 QSS 的 item 左右各 20px 内边距太宽，这里收窄左侧
    menu.setStyleSheet(
        "QMenu { padding: 4px 4px; }"
        "QMenu::item { padding: 4px 14px 4px 6px; margin: 1px 2px; }"
        "QMenu::separator { margin: 3px 2px; }")
    for i in range(len(objects)):
        act = menu.addAction(f"{i + 1}. {labels[i]}")
        act.setData(i)
        if i < len(objects) - 1:
            menu.addSeparator()
    return menu


def pickFromMenu(parent, globalPos: QPoint, objects) -> tuple:
    """在 globalPos 弹出候选菜单。

    返回 ``(选中对象或 None, 是否弹出过菜单)``：用户按 Esc 或点菜单外时
    为 ``(None, True)``，调用方据此"取消且不改选择"。
    """
    menu = buildCandidatesMenu(parent, objects)
    chosen = menu.exec(globalPos)
    if chosen is None:
        return None, True
    idx = chosen.data()
    if not isinstance(idx, int) or not (0 <= idx < len(objects)):
        return None, True
    return objects[idx], True
