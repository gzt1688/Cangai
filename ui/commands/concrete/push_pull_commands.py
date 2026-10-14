"""推拉命令：面推拉（挤出/挖孔/整面缩放）+ 线推拉（倒角）。build123d 内核。

交互：选中 3D 实体 → 点击其面 = 面操作（推拉模式：挤出/挖孔，圆柱面改
半径；缩放模式：平面整面缩放、侧壁放样跟随——圆柱顶面缩放即变圆锥台；
圆锥倒角面沿母线调整倒角量），点击其特征边 = 线推拉（倒角：拖出=圆角，
拖入=斜角）。右侧参数面板随选择类型自动切换（边=倒角页，面=面编辑页）。
拖动中实时预览，松开应用成型（一次撤销）。拾取类型由 3D 视口左上角
「点/线/面/实体」过滤器决定（face → 面操作，edge → 线推拉）。
"""
from __future__ import annotations
import math
import time
from typing import List, Optional

import threading
from PySide6.QtCore import Qt, QPointF, QObject, Signal as _Signal
from PySide6.QtGui import QPen, QColor, QBrush, QPolygonF, QFont

from core.vector.geometry_types import Point3D, Color3D, ObjectType
from core.vector.brep_shape import BrepShape
from core.vector.brep_object import BRepObject
from ui.commands.command_instance import CommandInstance

_PICK_PX = 8.0        # 子元素拾取半径（屏幕 px，与视口 PICK_RADIUS 同量级）
_HANDLE_LEN = 20.0    # 手柄箭头基长（世界单位，短手柄更易看清锚点）
_GIZMO_RIGHT_PX = 42.0   # 手柄锚点屏幕右偏移（偏右不居中）
_GRAB_PX = 11.0       # 抓取圆圈命中半径（屏幕 px）：鼠标必须点在圆圈内才可拖动
_REBUILD_MS = 24      # 拖动预览重建最小间隔（ms），保证拖动丝滑

# 选中面的类型 → 面板提示（缩放/推拉语义随面几何变化）
_FACE_HINTS = {
    "plane": "平面：推拉=挤出/挖孔；缩放=整面缩放（圆柱顶→圆锥台）",
    "cylinder": "圆柱面（外壁/孔壁）：推拉/缩放=改直径（+增大 −减小）",
    "cone": "圆锥(倒角)面：缩放 拉出=倒角变小，推入=倒角变大",
    "torus": "圆角环面：仅推拉可用（缩放暂不支持）",
    "region": "曲线分割区域（内圆面/外圆环）：推拉=该区域挤出/挖孔；"
              "缩放暂不支持",
    "other": "该面类型：仅支持推拉",
}


def _import_bd():
    """确保 build123d 已就绪并返回模块（_stagedImportBd 无返回值，
    导入后 import 即命中缓存，立即返回模块对象）。"""
    from core.vector.brep_shape import _stagedImportBd
    _stagedImportBd()
    import build123d
    return build123d


def _isCylinderFace(face) -> bool:
    """判断 build123d Face 是否为圆柱面（圆柱侧面）。"""
    try:
        from OCP.BRep import BRep_Tool
        from OCP.Geom import Geom_CylindricalSurface
        return isinstance(BRep_Tool.Surface_s(face.wrapped),
                          Geom_CylindricalSurface)
    except Exception:
        return False


def _faceSurfaceKind(face) -> str:
    """面几何类型：plane/cylinder/cone/torus/other（决定缩放行为与提示）。"""
    try:
        from OCP.BRep import BRep_Tool
        from OCP.Geom import (Geom_Plane, Geom_CylindricalSurface,
                              Geom_ConicalSurface, Geom_ToroidalSurface)
        s = BRep_Tool.Surface_s(face.wrapped)
        if isinstance(s, Geom_Plane):
            return "plane"
        if isinstance(s, Geom_CylindricalSurface):
            return "cylinder"
        if isinstance(s, Geom_ConicalSurface):
            return "cone"
        if isinstance(s, Geom_ToroidalSurface):
            return "torus"
    except Exception:
        pass
    return "other"


def _probeOutwardDir(part, anchor, d):
    """确保方向指向实体外侧：沿方向取探测点，若在实体内部则取反。
    凸棱的法向平均已朝外，只有凹角（T 形接合等）会朝内。"""
    try:
        from OCP.BRepClass3d import BRepClass3d_SolidClassifier
        from OCP.gp import gp_Pnt
        from OCP.TopAbs import TopAbs_State
        solid = None
        for s in part.solids():
            solid = s.wrapped
            break
        if solid is None:
            return d
        ln = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
        if ln < 1e-12:
            return d
        ux, uy, uz = d[0] / ln, d[1] / ln, d[2] / ln
        pt = gp_Pnt(anchor[0] + ux * 0.5, anchor[1] + uy * 0.5,
                    anchor[2] + uz * 0.5)
        cl = BRepClass3d_SolidClassifier(solid)
        cl.Perform(pt, 1e-6)
        if cl.State() == TopAbs_State.TopAbs_IN:
            return (-d[0], -d[1], -d[2])
    except Exception:
        pass
    return d


def _scaleFaceOp(part, face, delta: float):
    """缩放面（纯几何，无 UI 依赖，可供 headless 测试）：
    平面 = 整面缩放、邻接侧壁放样跟随（圆柱顶面 → 圆锥台）；
    圆锥面（已有倒角）= 沿母线平移调整倒角量；圆柱面 = 改半径。
    delta>0 = 面放大，delta<0 = 面缩小。返回新 Part，失败抛异常。"""
    kind = _faceSurfaceKind(face)
    if kind == "cylinder":
        return _resizeCylinderOp(part, face, delta)
    if kind == "cone":
        return _adjustConeFaceOp(part, face, delta)
    if kind == "plane":
        return _scalePlanarFaceOp(part, face, delta)
    if kind == "torus":
        raise ValueError("圆角环面暂不支持缩放（请用推拉，或撤销后重做圆角）")
    raise ValueError("该面类型暂不支持缩放")


def _transformShape(bd, shape, trsf):
    """深拷贝变换（含缩放）：TopLoc_Location 只允许刚体运动，
    缩放必须经 BRepBuilderAPI_Transform 把几何烘焙进形状。
    Shape() 返回泛型 TopoDS_Shape，需 TopoDS.Wire_s 下转型后才能
    被 bd.Wire 正确包装。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.TopoDS import TopoDS
    return bd.Wire(TopoDS.Wire_s(
        BRepBuilderAPI_Transform(shape.wrapped, trsf, True).Shape()))


def _lateralDepth(part, face, n, c) -> float:
    """相邻侧壁在面法向反侧的延伸深度（缩放过渡的基面距离）：
    取邻接面顶点的最大深度；查不到邻接时回退整个实体的最大深度。"""
    from OCP.BRep import BRep_Tool
    try:
        face_edges = [e.wrapped for e in face.edges()]
    except Exception:
        face_edges = []
    verts = []
    for f2 in part.faces():
        try:
            if f2.wrapped.IsSame(face.wrapped):
                continue
            hit = False
            for e2 in f2.edges():
                if any(e2.wrapped.IsSame(fe) for fe in face_edges):
                    hit = True
                    break
            if hit:
                verts.extend(f2.vertices())
        except Exception:
            continue
    if not verts:
        verts = part.vertices()
    best = 0.0
    for v in verts:
        p = BRep_Tool.Pnt_s(v.wrapped)
        t = ((c[0] - p.X()) * n[0] + (c[1] - p.Y()) * n[1]
             + (c[2] - p.Z()) * n[2])
        if t > best:
            best = t
    return best


def _faceFromWires(bd, outer, inners=()):
    """外环(+内孔环) → 平面 Face（build123d 0.11 的 loft 截面要求是
    Face，内部自动取 outer/inner wires，孔随放样过渡）。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    mk = BRepBuilderAPI_MakeFace(outer.wrapped)
    for w in inners:
        try:
            mk.Add(w.wrapped)
        except Exception:
            pass
    return bd.Face(mk.Face())


def _scalePlanarFaceOp(part, face, delta: float):
    """平面整面缩放（SketchUp 语义）：面外环绕面心缩放 s=1+delta/R，
    侧壁替换为 基截面→缩放环 的直纹放样——圆柱顶面缩放即整个圆柱变
    圆锥台（孔也随放样过渡）。基截面取**实体在基面处的真实截面**
    （布尔求交），因此二次缩放时底面保持原状、锥台从原始底轮廓继续
    收放；侧壁带倒角时也无台阶。结果 = (原体 − 基面以上) ∪ 放样体。"""
    from OCP.gp import (gp_Trsf, gp_Pnt, gp_Vec, gp_Ax3, gp_Dir,
                        gp_Pln)
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    bd = _import_bd()
    if abs(delta) < 1e-9:
        return part
    s3 = face.bounding_box().size
    r_ref = 0.5 * max(s3.X, s3.Y, s3.Z)
    if r_ref < 1e-9:
        raise ValueError("面太小，无法缩放")
    s = 1.0 + delta / r_ref
    if s < 0.05:
        raise ValueError("缩放比例过小")
    c = face.center()
    nn = face.normal_at(c)
    n = _probeOutwardDir(part, (c.X, c.Y, c.Z), (nn.X, nn.Y, nn.Z))
    depth = _lateralDepth(part, face, n, (c.X, c.Y, c.Z))
    if depth <= 1e-6:
        raise ValueError("无法确定面缩放过渡深度")
    # 基面工具：一张覆盖整个实体的大矩形面（截面求交 + 上方切除共用）
    bb = part.bounding_box()
    diag = math.sqrt(bb.size.X ** 2 + bb.size.Y ** 2 + bb.size.Z ** 2)
    m = diag + 1.0
    bx = c.X - n[0] * depth
    by = c.Y - n[1] * depth
    bz = c.Z - n[2] * depth
    xdir = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    pln = gp_Pln(gp_Ax3(gp_Pnt(bx, by, bz), gp_Dir(*n), gp_Dir(*xdir)))
    rect = bd.Face(BRepBuilderAPI_MakeFace(pln, -m, m, -m, m).Face())
    # 基截面 = 实体在基面处的真实截面（多个截域取离面中心最近）；
    # 求交失败时回退为 外/内环沿 -n 平移 depth
    base_face = None
    try:
        secs = [f for f in (part & rect).faces()]
        if secs:
            base_face = min(
                secs, key=lambda f: (f.center().X - bx) ** 2
                + (f.center().Y - by) ** 2 + (f.center().Z - bz) ** 2)
    except Exception:
        base_face = None
    if base_face is None:
        tr = gp_Trsf()
        tr.SetTranslation(gp_Vec(-n[0] * depth, -n[1] * depth,
                                 -n[2] * depth))
        base_outer = _transformShape(bd, face.outer_wire(), tr)
        base_inners = [_transformShape(bd, w, tr)
                       for w in (face.inner_wires() or [])]
        base_face = _faceFromWires(bd, base_outer, base_inners)
    # 顶截面 = 外/内环绕面心缩放 s（仍在原平面）
    ts = gp_Trsf()
    ts.SetScale(gp_Pnt(c.X, c.Y, c.Z), s)
    top_outer = _transformShape(bd, face.outer_wire(), ts)
    top_inners = [_transformShape(bd, w, ts)
                  for w in (face.inner_wires() or [])]
    lofted = bd.loft([base_face,
                      _faceFromWires(bd, top_outer, top_inners)],
                     ruled=True)
    above = bd.extrude(rect, amount=diag + depth + 1.0, dir=n)
    return (part - above) + lofted


def _adjustConeFaceOp(part, face, delta: float):
    """圆锥面（如圆柱顶已有倒角）缩放 = 沿母线平移锥面：边界小圈半径
    r_s → r_s+delta（大圈贴邻圆柱，半径不变、沿轴移动）。用含轴剖面
    四边形旋转出增/减料环做布尔：delta>0=加料（倒角变小），
    delta<0=去料（倒角变大）。"""
    from OCP.BRep import BRep_Tool
    from OCP.Geom import Geom_ConicalSurface
    from OCP.gp import gp_Pnt
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon
    bd = _import_bd()
    if abs(delta) < 1e-9:
        return part
    surf = BRep_Tool.Surface_s(face.wrapped)
    if not isinstance(surf, Geom_ConicalSurface):
        raise ValueError("非圆锥面")
    ax3 = surf.Position()
    o = ax3.Location()
    a = ax3.Direction()
    ox, oy, oz = o.X(), o.Y(), o.Z()
    ax, ay, az = a.X(), a.Y(), a.Z()
    al = math.sqrt(ax * ax + ay * ay + az * az) or 1.0
    ax, ay, az = ax / al, ay / al, az / al
    # 面顶点 → (轴向 z, 径向 r)，取边界小圈/大圈
    zs, rs = [], []
    for v in face.vertices():
        p = BRep_Tool.Pnt_s(v.wrapped)
        px, py, pz = p.X() - ox, p.Y() - oy, p.Z() - oz
        z = px * ax + py * ay + pz * az
        qx, qy, qz = px - ax * z, py - ay * z, pz - az * z
        zs.append(z)
        rs.append(math.sqrt(qx * qx + qy * qy + qz * qz))
    if not rs:
        raise ValueError("圆锥面无顶点")
    i_s = min(range(len(rs)), key=lambda i: rs[i])
    i_b = max(range(len(rs)), key=lambda i: rs[i])
    r_s, z_s = rs[i_s], zs[i_s]
    r_b, z_b = rs[i_b], zs[i_b]
    if r_b - r_s < 1e-6:
        raise ValueError("圆锥面范围过小")
    dz = z_s - z_b
    if abs(dz) < 1e-9:
        raise ValueError("圆锥面方向异常")
    tana = (r_b - r_s) / dz           # 有符号：母线 dr/dz
    r2 = r_s + delta
    if r2 <= 1e-6 or r2 >= r_b - 1e-9:
        raise ValueError("倒角调整过量")
    z2b = z_s - (r_b - r2) / tana
    if abs(z2b - z_b) < 1e-9:
        return part
    # 剖面方向 D：面中心径向投影（保证剖面穿过面），退化时任取 ⊥ 轴
    c = face.center()
    cx, cy, cz = c.X - ox, c.Y - oy, c.Z - oz
    zc = cx * ax + cy * ay + cz * az
    dx, dy, dzz = cx - ax * zc, cy - ay * zc, cz - az * zc
    dl = math.sqrt(dx * dx + dy * dy + dzz * dzz)
    if dl < 1e-9:
        ex, ey, ez = (1.0, 0.0, 0.0) if abs(ax) < 0.9 else (0.0, 1.0, 0.0)
        dot = ex * ax + ey * ay + ez * az
        dx, dy, dzz = ex - ax * dot, ey - ay * dot, ez - az * dot
        dl = math.sqrt(dx * dx + dy * dy + dzz * dzz) or 1.0
    dx, dy, dzz = dx / dl, dy / dl, dzz / dl

    def P(r: float, z: float) -> gp_Pnt:
        return gp_Pnt(ox + ax * z + dx * r, oy + ay * z + dy * r,
                      oz + az * z + dzz * r)

    mk = BRepBuilderAPI_MakePolygon()
    for r, z in ((r_s, z_s), (r2, z_s), (r_b, z2b), (r_b, z_b)):
        mk.Add(P(r, z))
    mk.Close()
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    prof = bd.Face(BRepBuilderAPI_MakeFace(mk.Wire()).Face())
    ring = bd.revolve(prof, axis=bd.Axis((ox, oy, oz), (ax, ay, az)))
    return (part + ring) if delta > 0.0 else (part - ring)


def _planeOf(face):
    """平面面 → (原点, 单位法向)；非平面返回 None。"""
    from OCP.BRep import BRep_Tool
    from OCP.Geom import Geom_Plane
    try:
        s = BRep_Tool.Surface_s(face.wrapped)
        if not isinstance(s, Geom_Plane):
            return None
        pln = s.Pln()
        loc = pln.Location()
        n = pln.Axis().Direction()
        nx, ny, nz = n.X(), n.Y(), n.Z()
        ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        return ((loc.X(), loc.Y(), loc.Z()), (nx / ln, ny / ln, nz / ln))
    except Exception:
        return None


def _pointInPolygon(px: float, py: float, pts) -> bool:
    """射线法点在多边形内判定（pts 为 (u,v) 序列）。"""
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > py) != (yj > py):
            xint = (xj - xi) * (py - yi) / (yj - yi) + xi
            if px < xint:
                inside = not inside
        j = i
    return inside


def _curveDiscFace(bd, curve: dict, p0, n):
    """闭合曲线 → 共面圆盘面（区域布尔的工具体）。"""
    from OCP.gp import (gp_Circ, gp_Ax2, gp_Pnt, gp_Dir, gp_Pln)
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge,
                                    BRepBuilderAPI_MakeWire,
                                    BRepBuilderAPI_MakeFace,
                                    BRepBuilderAPI_MakePolygon)
    if curve["kind"] == "circle":
        cx, cy, cz = curve["center"]
        ax2 = gp_Ax2(gp_Pnt(cx, cy, cz), gp_Dir(*n))
        edge = BRepBuilderAPI_MakeEdge(gp_Circ(ax2, curve["radius"])).Edge()
        wire = BRepBuilderAPI_MakeWire(edge).Wire()
    else:
        mkp = BRepBuilderAPI_MakePolygon()
        for (x, y, z) in curve["pts"]:
            mkp.Add(gp_Pnt(x, y, z))
        mkp.Close()
        wire = mkp.Wire()
    u = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    du = u[0] * n[0] + u[1] * n[1] + u[2] * n[2]
    u = (u[0] - n[0] * du, u[1] - n[1] * du, u[2] - n[2] * du)
    from OCP.gp import gp_Ax3
    pln = gp_Pln(gp_Ax3(gp_Pnt(*p0), gp_Dir(*n), gp_Dir(*u)))
    return bd.Face(BRepBuilderAPI_MakeFace(pln, wire).Face())


def _wireLoopsOf(region_face) -> list:
    """区域面各环 → 折线环列表（世界 3D 点），供视口高亮绘制。"""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_CurveType as _CT
    loops = []
    try:
        for wire in region_face.wires():
            pts = []
            for e in wire.edges():
                ad = BRepAdaptor_Curve(e.wrapped)
                u0, u1 = ad.FirstParameter(), ad.LastParameter()
                if ad.GetType() == _CT.GeomAbs_Line:
                    steps = 1
                else:
                    steps = max(12, min(96, int(
                        abs(u1 - u0) / (2 * math.pi) * 64)))
                for i in range(steps + 1):
                    p = ad.Value(u0 + (u1 - u0) * i / steps)
                    pts.append((p.X(), p.Y(), p.Z()))
            if len(pts) >= 3:
                loops.append(pts)
    except Exception:
        pass
    return loops


def _faceContainingClick(bd, faces_list, click, n):
    """多片区域中找包含点击的那片：薄棱柱（±ε 夹住平面）实体分类。"""
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_State
    eps = 0.05
    for f in faces_list:
        try:
            moved = f.moved(bd.Location((-n[0] * eps, -n[1] * eps,
                                         -n[2] * eps)))
            prism = bd.extrude(moved, amount=2 * eps, dir=n)
            solids = prism.solids()
            if not solids:
                continue
            cl = BRepClass3d_SolidClassifier(solids[0].wrapped)
            cl.Perform(gp_Pnt(*click), 1e-7)
            if cl.State() in (TopAbs_State.TopAbs_IN,
                              TopAbs_State.TopAbs_ON):
                return f
        except Exception:
            continue
    return None


def _planeBasisOf(face):
    """平面面 → (p0, n, u, v) 面内坐标系；非平面返回 None。"""
    pl = _planeOf(face)
    if pl is None:
        return None
    p0, n = pl
    u = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    du = u[0] * n[0] + u[1] * n[1] + u[2] * n[2]
    u = (u[0] - n[0] * du, u[1] - n[1] * du, u[2] - n[2] * du)
    ul = math.sqrt(u[0] ** 2 + u[1] ** 2 + u[2] ** 2) or 1.0
    u = (u[0] / ul, u[1] / ul, u[2] / ul)
    v = (n[1] * u[2] - n[2] * u[1], n[2] * u[0] - n[0] * u[2],
         n[0] * u[1] - n[1] * u[0])
    return (p0, n, u, v)


def _classifyCurves(curves, click, basis) -> tuple:
    """点击点对各曲线的包含判定（悬停/点击共用，结果作缓存键）。"""
    p0, n, u, v = basis
    t = ((click[0] - p0[0]) * n[0] + (click[1] - p0[1]) * n[1]
         + (click[2] - p0[2]) * n[2])
    cq = (click[0] - n[0] * t, click[1] - n[1] * t, click[2] - n[2] * t)

    def uv(p):
        return ((p[0] - p0[0]) * u[0] + (p[1] - p0[1]) * u[1]
                + (p[2] - p0[2]) * u[2],
                (p[0] - p0[0]) * v[0] + (p[1] - p0[1]) * v[1]
                + (p[2] - p0[2]) * v[2])

    out = []
    for curve in curves:
        if curve["kind"] == "circle":
            cx, cy, cz = curve["center"]
            d = math.sqrt((cq[0] - cx) ** 2 + (cq[1] - cy) ** 2
                          + (cq[2] - cz) ** 2)
            out.append(d < curve["radius"])
        else:
            poly = [uv(p) for p in curve["pts"]]
            cu, cv = uv(cq)
            out.append(_pointInPolygon(cu, cv, poly))
    return tuple(out)


def _coplanarSplitRegion(bd, part, face, curves, click,
                         basis=None, insides=None):
    """面上被共面闭合曲线分割出的、包含 click 的区域（内圆盘/外圆环）。

    curves 元素：{"kind":"circle","center":(x,y,z),"radius":r} 或
    {"kind":"poly","pts":[(x,y,z),…]}。包含 click 的曲线取交（inner），
    不包含的作差（outer），可叠加多条。insides 可传入预计算的分类
    （_classifyCurves，悬停缓存路径），basis 传面坐标系避免重复构建。
    返回 (region_face, loops, label) 或 None（无有效分割）。"""
    basis = basis if basis is not None else _planeBasisOf(face)
    if basis is None:
        return None
    p0, n, u, v = basis
    try:
        face_area = float(face.area)
    except Exception:
        return None
    if face_area < 1e-12:
        return None
    tol = max(1e-4, 0.001 * math.sqrt(face_area))
    # 点击投影到面平面
    t = ((click[0] - p0[0]) * n[0] + (click[1] - p0[1]) * n[1]
         + (click[2] - p0[2]) * n[2])
    cq = (click[0] - n[0] * t, click[1] - n[1] * t, click[2] - n[2] * t)

    if insides is None:
        insides = _classifyCurves(curves, click, basis)

    region = face
    applied = False
    for curve, inside in zip(curves, insides):
        # 共面校验：曲线上各点到面平面距离 ≤ tol
        if curve["kind"] == "circle":
            cx, cy, cz = curve["center"]
            if abs((cx - p0[0]) * n[0] + (cy - p0[1]) * n[1]
                   + (cz - p0[2]) * n[2]) > tol:
                continue
        else:
            if any(abs((p[0] - p0[0]) * n[0] + (p[1] - p0[1]) * n[1]
                       + (p[2] - p0[2]) * n[2]) > tol
                   for p in curve["pts"]):
                continue
        try:
            disc = _curveDiscFace(bd, curve, p0, n)
            r2 = (region & disc) if inside else (region - disc)
            rf = [f for f in r2.faces()]
        except Exception:
            continue
        if not rf:
            continue
        pick = _faceContainingClick(bd, rf, cq, n) if len(rf) > 1 else rf[0]
        region = pick if pick is not None else rf[0]
        applied = True
    if not applied:
        return None
    try:
        ratio = float(region.area) / face_area
    except Exception:
        return None
    if ratio > 0.999 or ratio < 1e-3:
        return None          # 曲线没有真正分割面（完全在面外/相切）
    loops = _wireLoopsOf(region)
    label = "内侧区域" if ratio < 0.5 else "外侧区域"
    return (region, loops, label)


def _resizeCylinderOp(part, face, d: float):
    """圆柱侧面（外壁/孔壁）推拉/缩放 = 改直径：按面法向与径向的
    点积判定外壁（法向背离轴）还是孔壁（法向指向轴，如凹槽侧壁），
    用新旧半径之间的材料环做布尔增减——只改动该圆柱特征本身，
    不整模替换（孔壁拖动绝不能把整个模型丢掉）。d 为半径增量
    （沿"远离轴"方向拖动为正：外壁=实体变粗，孔壁=孔变大）。"""
    from OCP.BRep import BRep_Tool
    from OCP.Geom import Geom_CylindricalSurface
    from OCP.gp import gp_Pnt, gp_Trsf, gp_Ax3, gp_Dir
    from OCP.TopLoc import TopLoc_Location
    from build123d import Location
    bd = _import_bd()
    h = BRep_Tool.Surface_s(face.wrapped)
    if not isinstance(h, Geom_CylindricalSurface):
        raise ValueError("非圆柱面")
    ax3 = h.Position()
    # 面定向可能导致 Position 的 Z 反向（左手系，变换含反射使体积变负）；
    # 圆柱绕轴旋转对称，用 位置+轴方向 重新右手化即可
    ax3 = gp_Ax3(ax3.Location(), ax3.Direction())
    r = h.Radius()
    # 面顶点沿轴投影 -> 高度范围（比 bbox 角点法在旋转时更精确）
    loc_trsf = gp_Trsf()
    loc_trsf.SetTransformation(ax3)
    zs = []
    for v in face.vertices():
        p = BRep_Tool.Pnt_s(v.wrapped)
        p.Transform(loc_trsf)
        zs.append(p.Z())
    if not zs:
        raise ValueError("圆柱面无顶点")
    z_lo, z_hi = min(zs), max(zs)
    ht = z_hi - z_lo
    if ht <= 1e-9:
        raise ValueError("圆柱面高度为零")
    # 内外壁判定：面法向（朝材料外）与"轴→面中心"径向的点积
    c = face.center()
    nn = face.normal_at(c)
    ox, oy, oz = (ax3.Location().X(), ax3.Location().Y(),
                  ax3.Location().Z())
    ax, ay, az = (ax3.Direction().X(), ax3.Direction().Y(),
                  ax3.Direction().Z())
    cx, cy, cz = c.X - ox, c.Y - oy, c.Z - oz
    t = cx * ax + cy * ay + cz * az
    rx, ry, rz = cx - ax * t, cy - ay * t, cz - az * t
    rl = math.sqrt(rx * rx + ry * ry + rz * rz) or 1.0
    inner = (nn.X * rx + nn.Y * ry + nn.Z * rz) / rl < 0.0
    nr = r + d
    if inner:
        if nr < 0.0:
            nr = 0.0            # 孔收到 0 = 整体回填
    elif nr <= 1e-6:
        raise ValueError("直径过小")
    if abs(nr - r) <= 1e-9:
        return part
    # 材料环（内径收到 0 时为实心柱）
    r_out, r_in = max(r, nr), min(r, nr)
    ring = bd.Cylinder(r_out, ht, align=(bd.Align.CENTER, bd.Align.CENTER,
                                         bd.Align.MIN))
    if r_in > 1e-6:
        ring = ring - bd.Cylinder(
            r_in, ht, align=(bd.Align.CENTER, bd.Align.CENTER,
                             bd.Align.MIN))
    # 底中心世界位置 = 轴原点沿局部轴抬升 z_lo（绝不能沿世界 Z 平移，
    # 否则旋转/离轴圆柱重建后位置偏移）
    origin = gp_Pnt(ax3.Location().XYZ()
                    + ax3.Direction().XYZ() * z_lo)
    trsf = gp_Trsf()
    # SetTransformation(From, To) 生成 To->From 的变换；要得到
    # 局部系->世界，必须 From=带偏移轴系、To=标准系
    trsf.SetTransformation(gp_Ax3(origin, ax3.Direction(),
                                  ax3.XDirection()),
                           gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)))
    ring = Location(TopLoc_Location(trsf)) * ring
    # 外壁：扩=加料、缩=去料；孔壁：扩（挖更多）=去料、缩（回填）=加料
    grow = nr > r
    add = (not inner) == grow
    return (part + ring) if add else (part - ring)


class _PreviewDoneSignal(QObject):
    done = _Signal(object)


def _segDist(mx: float, my: float,
             ax: float, ay: float, bx: float, by: float) -> float:
    """点到线段屏幕距离。"""
    abx, aby = bx - ax, by - ay
    l2 = abx * abx + aby * aby
    if l2 < 1e-18:
        return math.hypot(mx - ax, my - ay)
    t = ((mx - ax) * abx + (my - ay) * aby) / l2
    t = max(0.0, min(1.0, t))
    return math.hypot(mx - (ax + t * abx), my - (ay + t * aby))


class PushPullCommand(CommandInstance):
    """面推拉 = 挤出特征（正）/凹陷（负）；线推拉 = 倒角。"""

    def __init__(self) -> None:
        super().__init__()
        self._target: Optional[BRepObject] = None
        self._faceIdx: Optional[int] = None   # 选中拓扑面索引
        self._edgeIdx: Optional[int] = None   # 选中边索引
        self._hover: Optional[tuple] = None   # (obj, kind, idx) 悬停
        self._gizmo: Optional[dict] = None    # 手柄 {anchor, axis, dist}
        self._dragging = False
        self._gizmoHover = False   # 鼠标悬停抓取圆圈（变色提示）
        self._previewObj: Optional[BRepObject] = None
        self._lastRebuild = 0.0
        self._lastKey = None                  # (kind, idx, dist) 去抖
        # 后台预览重建：合并式（只算最新距离），主线程拖动不阻塞
        self._bgLock = threading.Lock()
        self._bgPending = None
        self._bgBusy = False
        self._bgSig = _PreviewDoneSignal()
        self._bgSig.done.connect(self._onBgPreviewDone)
        self._panel = None
        self._ppMode = 0            # 0=圆角 1=倒直角（斜角）
        self._faceMode = 0          # 面操作：0=推拉 1=缩放
        self._faceDir = 0           # 推拉方向：0=添加(挤出) 1=切除(挖孔)
        self._faceKind = ""         # 选中面几何类型 plane/cylinder/cone/…
        self._region = None         # 曲线分割区域 (face, loops, label, n)
        self._hoverRegion = None    # 悬停面的分割区域（预选高亮）
        self._regionCache = {}      # (obj/面/曲线签名/分类) → 区域
        self._faceInfoCache = {}    # (obj, fi) → (part, face, basis, curves, sig)

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        super().start()
        self._target = None
        self._faceIdx = None
        self._edgeIdx = None
        self._hover = None
        self._gizmo = None
        self._dragging = False
        self._previewObj = None
        self._region = None
        self._hoverRegion = None
        self._regionCache = {}
        self._faceInfoCache = {}
        # 推拉默认拾取面（面推拉=挤出/凹陷）；若过滤是 body/vertex
        # 则自动切到 face，保证点实体即有反应（切"线"过滤可倒角）
        try:
            vp = self._viewport()
            if not (vp.selFilters() & {"face", "edge"}):
                vp.setSelectionMode("face")
        except Exception:
            pass
        self._hideCursorMarker()
        self._notify("点击面=推拉(挤出/挖孔)或缩放，点击边=倒角；"
                     "右侧面板随选择切换，Esc 结束")

    def prompt(self) -> str:
        if self._gizmo is not None:
            if self._dragging:
                return f"拖动手柄推拉（距离 {self._gizmo['dist']:g}），松开应用"
            if self._edgeIdx is not None:
                return "已选边（倒角）：拖手柄或用右侧面板，Esc 结束"
            if self._region is not None:
                return (f"已选{self._region[2]}（曲线分割）："
                        "拖手柄推拉，Esc 结束")
            if self._faceMode == 1:
                return "已选面（缩放）：拖手柄或用右侧面板，Esc 结束"
            return "已选面（推拉）：拖手柄或用右侧面板，Esc 结束"
        return "点击面=推拉/缩放，点击边=倒角（面板随选择切换）"

    def name(self) -> str:
        return "PushPull"

    def needsCursorMarker(self) -> bool:
        """推拉不显示光标预览点（鼠标旁不要白点）。"""
        return False

    def showSnapHints(self) -> bool:
        """推拉不显示吸附气泡：鼠标旁不需要小圆点干扰。"""
        return False

    def previewSolid(self):
        return self._previewObj

    def onEnter(self) -> bool:
        return self.onRightClick(None)

    # ---------------- 视口解析 ----------------

    def _viewport(self):
        """活动 3D 视口（_modeView）优先；否则回退 2D 画布（_view）。

        CommandManager 注入的 _view 恒为 2D 画布（GraphicsView，无
        _camera/_project），3D 模式必须用 _modeView（Viewport3D）。
        """
        mv = getattr(self, "_modeView", None)
        if mv is not None and mv is not self._view:
            cm = getattr(mv, "canvasMode", None)
            if callable(cm):
                try:
                    if cm() == "3d":
                        return mv
                except Exception:
                    pass
        return self._view

    # ---------------- 鼠标 ----------------

    def onMouseMove(self, wp, mods) -> None:
        if self._view is None:
            return
        sp = self._screenPoint(wp)
        if sp is None:
            return
        mx, my = sp
        if self._dragging:
            self._updateDrag(mx, my)
            return
        hit = self._pick(mx, my)
        # _pickSubElement 返回三角形索引，必须转成面索引，否则
        # _drawSubSelection 用 faceRanges[idx]（面索引）会越界/画错面
        if hit is not None and hit[1] == "face":
            fi = self._faceIndexFromTri(hit[0], hit[2])
            if fi is not None:
                hit = (hit[0], "face", fi)
        # 悬停预选：面上的曲线分割区域（内圆面/外圆环）提前高亮，
        # 告诉用户点击将选中哪块（缓存命中时仅做点分类，开销极小）
        if hit is not None and hit[1] == "face":
            hr = self._regionFor(hit[0], hit[2], (wp.x, wp.y, wp.z))
        else:
            hr = None
        if hit != self._hover or hr is not self._hoverRegion:
            self._hover = hit
            self._hoverRegion = hr
            self._updateHighlight()
        # 圆圈悬停变色：鼠标在抓取圆圈内才高亮（拖拽中保持）
        self._gizmoHover = bool(
            self._gizmo is not None
            and (self._dragging or self._hitGizmo(mx, my)))
        # 拖动/选中态下也保持悬停提示（点到已选面时 hover 不再变化）

    def onMousePress(self, wp, mods) -> None:
        if self._view is None:
            return
        self._pressWorld = (wp.x, wp.y, wp.z)
        sp = self._screenPoint(wp)
        if sp is None:
            return
        mx, my = sp
        # 1) 手柄命中 → 开始拖动
        if self._gizmo is not None and self._hitGizmo(mx, my):
            self._dragging = True
            self._notify("拖动手柄推拉，松开鼠标应用")
            self._viewport().update()
            return
        # 2) 点实体子元素 → 锁定该实体 + 出手柄（无需预选）
        hit = self._pick(mx, my)
        if hit is None:
            self._clearPick()
            return
        obj, kind, idx = hit
        self._target = obj
        self._region = None
        if kind == "face":
            fi = self._faceIndexFromTri(obj, idx)
            if fi is None:
                return
            self._faceIdx, self._edgeIdx = fi, None
            # 曲线分割检测：点击点落在贴面闭合曲线分割出的区域
            # （内圆面/外圆环）内 → 区域推拉（与悬停共用缓存，通常命中）
            self._region = self._regionFor(obj, fi, self._pressWorld)
        elif kind == "edge":
            self._edgeIdx, self._faceIdx = idx, None
        else:
            return
        self._buildGizmo()
        self._syncPanel(force=True)
        self._refreshPreview(force=True)
        self._updateHighlight()
        self._viewport().update()

    def onMouseRelease(self, mods) -> None:
        if self._dragging:
            self._dragging = False
            self._gizmoHover = False
            self._apply()
            self._faceIdx = None
            self._edgeIdx = None
            self._region = None
            self._gizmo = None
            self._previewObj = None
            self._syncPanel()
            self._updateHighlight()
            if self._manager is not None:
                self._manager.invalidatePreview()
            self._viewport().update()

    def cancel(self) -> None:
        """取消：作废在途后台预览（gizmo 置 None 使 _onBgPreviewDone 守卫
        生效，否则过期回调会把真实文档实体隐藏）+ 复原可见性 + 清高亮。"""
        self._gizmo = None
        self._bgPending = None
        self._showTarget(True)
        self._previewObj = None
        self._clearViewportHighlight()
        self._target = None
        super().cancel()

    def finish(self) -> None:
        """结束命令：与 cancel 同一套收尾（原第一份 finish 只清高亮、被本
        定义覆盖成死代码，两份合一）。"""
        self._gizmo = None
        self._bgPending = None
        self._showTarget(True)
        self._previewObj = None
        self._clearViewportHighlight()
        self._target = None
        super().finish()

    def _clearViewportHighlight(self) -> None:
        """清理 3D 视口残留的子元素高亮（悬停白/选中黄）。"""
        try:
            vp = self._viewport()
            if hasattr(vp, "_subSel"):
                vp._subSel = []
                vp._subHover = None
                vp.update()
        except Exception:
            pass

    def onRightClick(self, wp) -> bool:
        if self._target is not None and self._gizmo is not None:
            self._apply()
            self._faceIdx = None
            self._edgeIdx = None
            self._region = None
            self._gizmo = None
            self._previewObj = None
            self._syncPanel()
            self._updateHighlight()
            if self._manager is not None:
                self._manager.invalidatePreview()
            self._viewport().update()
            return True
        self.finish()
        return True

    # ---------------- 拾取 ----------------

    def _screenPoint(self, wp):
        """世界点 → 屏幕 (mx, my)。_project 返回 (x, y, depth)，只取前两元。"""
        vp = self._viewport()
        cam = vp._camera()
        w, h = vp.width(), vp.height()
        pr = vp._project((wp.x, wp.y, wp.z), cam, w, h)
        if pr is None:
            return None
        return (pr[0], pr[1])

    def _pick(self, mx: float, my: float):
        """按当前过滤器拾取 face/edge 子元素。"""
        kinds = set(self._viewport().selFilters()) & {"face", "edge"}
        if not kinds:
            return None
        try:
            return self._viewport()._pickSubElement(mx, my, kinds=kinds)
        except Exception:
            return None

    def _faceIndexFromTri(self, obj, tri_idx: int) -> Optional[int]:
        """三角形索引 → 拓扑面索引（mesh faceRanges 与 BRep 面同序）。"""
        try:
            mesh = obj.mesh()
            for fi, r in enumerate(mesh["faceRanges"]):
                s, c = int(r[0]), int(r[1])
                if s <= tri_idx < s + c:
                    return fi
        except Exception:
            pass
        return None

    def _shape(self) -> Optional[BrepShape]:
        if self._target is None:
            return None
        shape = self._target.shape()
        return shape if isinstance(shape, BrepShape) else None

    _REGION_NONE = object()      # "无分割"哨兵（区别于未缓存）

    def _faceInfoFor(self, obj, fi: int):
        """(obj, 面索引) → (part, face, basis)，按 shape 对象身份缓存：
        实体被编辑（setShape 换新 part）后自动失效重建。"""
        shape = obj.shape()
        if not isinstance(shape, BrepShape):
            return None
        key = (obj.id(), fi)
        hit = self._faceInfoCache.get(key)
        if hit is not None and hit[0] is shape:
            return hit[1]
        try:
            part = shape.part()
            faces = part.faces()
            if fi >= len(faces):
                return None
            face = faces[fi]
            if _faceSurfaceKind(face) != "plane":
                info = None
            else:
                basis = _planeBasisOf(face)
                info = (part, face, basis) if basis is not None else None
        except Exception:
            info = None
        if len(self._faceInfoCache) > 8:
            self._faceInfoCache.clear()
        self._faceInfoCache[key] = (shape, info)
        return info

    def _scanCurves(self, obj):
        """文档中共面候选闭合曲线（圆/闭合折线）：每次现扫（廉价），
        移动/修改曲线后签名即变，区域缓存键随之更新。"""
        curves = []
        sig = []
        if self._doc is None:
            return curves, tuple(sig)
        for o in self._doc.visibleObjects():
            if o is obj or not o.isVisible() or o.isLocked():
                continue
            t = o.type()
            if t == ObjectType.CIRCLE:
                c = o.center()
                r = float(o.radius())
                curves.append({"kind": "circle",
                               "center": (c.x, c.y, c.z), "radius": r})
                sig.append(("c", round(c.x, 6), round(c.y, 6),
                            round(c.z, 6), round(r, 6)))
            elif t == ObjectType.LINE:
                try:
                    if not o.isClosed():
                        continue
                    pts = [(p.x, p.y, p.z) for p in o.points()]
                    if len(pts) >= 3:
                        curves.append({"kind": "poly", "pts": pts})
                        sig.append(("p", tuple(
                            (round(p[0], 6), round(p[1], 6),
                             round(p[2], 6)) for p in pts)))
                except Exception:
                    pass
        return curves, tuple(sig)

    def _regionFor(self, obj, fi: int, world):
        """包含 world 点的曲线分割区域 (face, loops, label, 外法向)，
        带缓存（键=对象/面/曲线签名/包含分类）：悬停每次移动只做廉价的
        点分类 + 文档曲线扫描，布尔求交每类只算一次；无分割返回 None。"""
        try:
            info = self._faceInfoFor(obj, fi)
            if info is None:
                return None
            part, face, basis = info
            curves, sig = self._scanCurves(obj)
            if not curves:
                return None
            insides = _classifyCurves(curves, world, basis)
            key = (obj.id(), fi, sig, insides)
            hit = self._regionCache.get(key, PushPullCommand._REGION_NONE)
            if hit is not PushPullCommand._REGION_NONE:
                return hit
            bd = _import_bd()
            r = _coplanarSplitRegion(bd, part, face, curves, world,
                                     basis=basis, insides=insides)
            entry = None
            if r is not None:
                region, loops, label = r
                c = face.center()
                n = _probeOutwardDir(part, (c.X, c.Y, c.Z), basis[1])
                entry = (region, loops, label, n)
            if len(self._regionCache) > 32:
                self._regionCache.clear()
            self._regionCache[key] = entry
            return entry
        except Exception:
            return None

    # ---------------- 手柄 ----------------

    def _buildGizmo(self) -> None:
        shape = self._shape()
        if shape is None:
            self._gizmo = None
            return
        try:
            part = shape.part()
            if self._faceIdx is not None:
                faces = part.faces()
                if self._faceIdx >= len(faces):
                    self._gizmo = None
                    return
                face = faces[self._faceIdx]
                self._faceKind = _faceSurfaceKind(face)
                if self._region is not None:
                    # 分割区域：锚点=区域形心，方向=面外法向（几何语义，
                    # +拉出=加料/−推入=去料，与相机朝向无关）
                    self._faceKind = "region"
                    c = self._region[0].center()
                    ax, ay, az = c.X, c.Y, c.Z
                    dx, dy, dz = self._region[3]
                else:
                    ax, ay, az, dx, dy, dz = self._faceAnchorAxis(face)
            elif self._edgeIdx is not None:
                self._faceKind = ""
                edges = shape.edges()
                if self._edgeIdx >= len(edges):
                    self._gizmo = None
                    return
                edge = edges[self._edgeIdx]
                # 锚点 = 鼠标位置在边曲线上的最近点（沿曲线投影：
                # 闭合圆边 p0==p1，直线参数化会退化成固定起点）
                _p0 = edge @ 0.0
                _p1 = edge @ 1.0
                pw = getattr(self, "_pressWorld", None)
                if pw is not None:
                    try:
                        from OCP.BRepExtrema import (
                            BRepExtrema_DistShapeShape)
                        from OCP.BRepBuilderAPI import (
                            BRepBuilderAPI_MakeVertex)
                        from OCP.gp import gp_Pnt
                        _v = BRepBuilderAPI_MakeVertex(
                            gp_Pnt(pw[0], pw[1], pw[2])).Vertex()
                        _dss = BRepExtrema_DistShapeShape(
                            _v, edge.wrapped)
                        if _dss.IsDone() and _dss.NbSolution() > 0:
                            _pt = _dss.PointOnShape2(1)
                            ax, ay, az = _pt.X(), _pt.Y(), _pt.Z()
                        else:
                            raise RuntimeError("no proj")
                    except Exception:
                        # 回退：直线投影（非闭合边）
                        import numpy as np
                        _a = np.array((_p0.X, _p0.Y, _p0.Z))
                        _b = np.array((_p1.X, _p1.Y, _p1.Z))
                        _e = _b - _a
                        _l2 = float(np.dot(_e, _e))
                        if _l2 > 1e-12:
                            _t = float(np.dot(
                                np.array(pw) - _a, _e)) / _l2
                            _t = max(0.0, min(1.0, _t))
                            _p = _a + _e * _t
                        else:
                            _p = _a
                        ax, ay, az = float(_p[0]), float(_p[1]),
                        float(_p[2])
                else:
                    anchor = edge.center()
                    ax, ay, az = anchor.X, anchor.Y, anchor.Z
                # 方向：垂直边、朝实体外（邻接面法向平均 + 体内探测兜底）
                # 法向在锚点处取：圆边法向随位置变化，固定中点会差 90 度
                axis = self._edgeChamferDir(part, edge, at=(ax, ay, az))
                dx = getattr(axis, "X", getattr(axis, "x", 0.0))
                dy = getattr(axis, "Y", getattr(axis, "y", 0.0))
                dz = getattr(axis, "Z", getattr(axis, "z", 0.0))
                dx, dy, dz = self._outwardDir(part, (ax, ay, az),
                                              (dx, dy, dz))
            else:
                self._gizmo = None
                return
            ln = math.sqrt(dx * dx + dy * dy + dz * dz)
            if ln < 1e-12:
                dx, dy, dz = 0.0, 0.0, 1.0
                ln = 1.0
            dx, dy, dz = dx / ln, dy / ln, dz / ln
            # 手柄永远朝向相机：方向背离视线则翻转，避免手柄伸到
            # 模型背面被遮挡（拖向屏幕=朝屏幕挤出，语义一致）。
            # 圆柱径向已取屏幕右方向（自动可见），不再翻转，否则会
            # 把朝右的箭头翻到左边。
            if (self._faceIdx is not None
                    and not _isCylinderFace(faces[self._faceIdx])):
                try:
                    eye = self._viewport()._camera()[0]
                    vx = eye[0] - ax
                    vy = eye[1] - ay
                    vz = eye[2] - az
                    if vx * dx + vy * dy + vz * dz < 0.0:
                        dx, dy, dz = -dx, -dy, -dz
                except Exception:
                    pass
            self._gizmo = {
                "anchor": (ax, ay, az),
                "axis": (dx, dy, dz),
                "dist": 0.0,
            }
        except Exception:
            self._gizmo = None

    def _faceAnchorAxis(self, face):
        """手柄锚点+方向：圆柱面=面向相机的外侧母线点（径向朝相机，
        避免 face.center() 落在轴线上导致手柄在实体内部）；平面=面中心
        +法线。返回 (ax, ay, az, dx, dy, dz)。"""
        try:
            if _isCylinderFace(face):
                from OCP.BRep import BRep_Tool
                from OCP.gp import gp_Ax3
                import numpy as _np
                h = BRep_Tool.Surface_s(face.wrapped)
                ax3 = gp_Ax3(h.Position().Location(),
                             h.Position().Direction())
                r = h.Radius()
                eye = _np.array(self._viewport()._camera()[0])
                o = _np.array((ax3.Location().X(), ax3.Location().Y(),
                               ax3.Location().Z()))
                d = _np.array((ax3.Direction().X(), ax3.Direction().Y(),
                               ax3.Direction().Z()))
                d /= (_np.linalg.norm(d) or 1e-12)
                # face.center() 对圆柱面返回曲面上 u/v 中心的点（非轴点），
                # 沿轴投影回轴线作基准（高度=面中心高度）；径向取
                # "屏幕右方向去轴向分量"——锚点贴面（朝右母线点）、
                # 箭头垂直于表面且指向画布右侧
                c = _np.array((face.center().X, face.center().Y,
                               face.center().Z))
                base = o + d * float(_np.dot(c - o, d))
                _right = _np.array(self._viewport()._camera()[1])
                v = _right - d * float(_np.dot(_right, d))
                vn = _np.linalg.norm(v)
                if vn < 1e-12:
                    ref = (_np.array((1.0, 0.0, 0.0))
                           if abs(d[0]) < 0.9
                           else _np.array((0.0, 1.0, 0.0)))
                    v = _np.cross(d, ref)
                    vn = _np.linalg.norm(v) or 1.0
                    v = v / vn
                else:
                    v = v / vn
                anchor = base + v * r
                return (float(anchor[0]), float(anchor[1]),
                        float(anchor[2]), float(v[0]), float(v[1]),
                        float(v[2]))
        except Exception:
            pass
        c = face.center()
        nn = face.normal_at(c)
        return (c.X, c.Y, c.Z, nn.X, nn.Y, nn.Z)

    def _edgeChamferDir(self, part, edge, at=None):
        """倒角方向：边邻接面在锚点处（默认边中点）的法向平均（朝外，
        角平分方向）。必须用锚点处的法向——圆边法向随位置变化，
        固定中点会对侧取到相反方向。"""
        try:
            acc = [0.0, 0.0, 0.0]
            cnt = 0
            if at is not None:
                # 用 tuple：自定义 Point3D 传 normal_at 会让圆柱侧面
                # 法向取反（实测 (-1,0,0) vs tuple (1,0,0)）
                mp = (at[0], at[1], at[2])
            else:
                mp = edge @ 0.5
            for face in part.faces():
                for fe in face.edges():
                    if fe.wrapped.IsSame(edge.wrapped):
                        n = face.normal_at(mp)
                        acc[0] += n.X
                        acc[1] += n.Y
                        acc[2] += n.Z
                        cnt += 1
                        break
            if cnt == 0:
                return Point3D(0.0, 0.0, 1.0)
            return Point3D(acc[0] / cnt, acc[1] / cnt, acc[2] / cnt)
        except Exception:
            return Point3D(0.0, 0.0, 1.0)

    def _outwardDir(self, part, anchor, d):
        """确保方向指向实体外侧（实现见模块级 _probeOutwardDir）。"""
        return _probeOutwardDir(part, anchor, d)

    def _gizmoScreen(self):
        """手柄锚点/端点/轴方向 的屏幕投影。"""
        g = self._gizmo
        cam = self._viewport()._camera()
        w, h = self._viewport().width(), self._viewport().height()
        ax, ay, az = g["anchor"]
        dx, dy, dz = g["axis"]
        s0 = self._viewport()._project((ax, ay, az), cam, w, h)
        tip = (ax + dx * _HANDLE_LEN, ay + dy * _HANDLE_LEN,
               az + dz * _HANDLE_LEN)
        s1 = self._viewport()._project(tip, cam, w, h)
        if s0 is None or s1 is None:
            return None
        return (s0[0], s0[1], s1[0], s1[1])

    def _hitGizmo(self, mx: float, my: float) -> bool:
        """只有鼠标落在锚点抓取圆圈内才可拖动（端点箭头仅指示方向）。"""
        gs = self._gizmoScreen()
        if gs is None:
            return False
        s0x, s0y, s1x, s1y = gs
        return math.hypot(mx - s0x, my - s0y) <= _GRAB_PX

    def _updateDrag(self, mx: float, my: float) -> None:
        g = self._gizmo
        gs = self._gizmoScreen()
        if gs is None:
            return
        s0x, s0y, s1x, s1y = gs
        vx, vy = s1x - s0x, s1y - s0y
        vlen = math.hypot(vx, vy)
        if vlen < 1e-9:
            return
        ux, uy = vx / vlen, vy / vlen
        proj = (mx - s0x) * ux + (my - s0y) * uy
        # 屏幕像素 → 世界距离（按手柄深度处 px/unit 比例）
        scale = vlen / _HANDLE_LEN
        g["dist"] = proj / scale if scale > 1e-9 else 0.0
        self._syncPanel()
        self._refreshPreview()

    # ---------------- 后台预览重建 ----------------

    def _scheduleBg(self, shape, d: float) -> None:
        """合并式后台重建：只保留最新距离，主线程立即返回。"""
        fi, ei = self._faceIdx, self._edgeIdx
        region = self._region
        with self._bgLock:
            self._bgPending = (shape, d, fi, ei, region)
        if not self._bgBusy:
            self._bgBusy = True
            threading.Thread(target=self._bgLoop, daemon=True).start()

    def _bgLoop(self) -> None:
        while True:
            with self._bgLock:
                job = self._bgPending
                self._bgPending = None
            if job is None:
                self._bgBusy = False
                return
            shape, d, fi, ei, region = job
            try:
                if fi is not None:
                    new_part = self._applyFaceOp(shape, fi, d, region)
                elif ei is not None:
                    new_part = self._chamferEdge(shape, ei, d)
                else:
                    continue
                bs = BrepShape.fromPart(new_part)
                try:
                    self._bgSig.done.emit((d, bs, None))
                except RuntimeError:
                    return  # 命令已销毁（窗口关闭）
            except Exception as e:
                try:
                    self._bgSig.done.emit((d, None, str(e)))
                except RuntimeError:
                    return

    def _onBgPreviewDone(self, payload) -> None:
        """主线程应用后台重建结果（过期距离丢弃，避免闪烁）。"""
        d, bs, err = payload
        if self._gizmo is None:
            return
        if abs(self._gizmo["dist"] - d) > 0.05:
            return
        if bs is None:
            self._notify(f"预览失败：{err}")
            self._showTarget(True)
            return
        prev = BRepObject()
        prev.setShape(bs)
        # 不透明预览：半透明时挖凹槽的口袋内部前后层三角形互相透视
        # 叠加，看起来"凹槽面全是三角面"。原实体在预览期间本就隐藏，
        # 不透明预览即最终效果，更干净
        prev.setColor(Color3D(
            0x15 / 255.0, 0xB6 / 255.0, 0xC9 / 255.0, 1.0))
        self._previewObj = prev
        self._showTarget(False)
        if self._manager is not None:
            self._manager.invalidatePreview()
        try:
            self._viewport().update()
        except Exception:
            pass

    # ---------------- 参数面板（随选择切换） --------------

    def paramPanel(self):
        """右侧标准参数面板：边=圆角/斜角+半径；面=推拉(挤出/挖孔)/缩放。"""
        if self._panel is None:
            from ..param_panels import PushPullParamPanel
            self._panel = PushPullParamPanel()
            p = self._panel
            p.filletModeChanged.connect(self._onPanelMode)
            p.radiusChanged.connect(self._onPanelRadius)
            p.faceModeChanged.connect(self._onPanelFaceMode)
            p.faceDirChanged.connect(self._onPanelFaceDir)
            p.faceValueChanged.connect(self._onPanelFaceValue)
            p.confirmed.connect(self._apply)
            p.setRadius(5.0)
        return self._panel

    def _onPanelMode(self, mode: int) -> None:
        """面板切 圆角(0)/倒直角(1)：改 dist 符号。"""
        self._ppMode = mode
        if self._gizmo is None:
            return
        d = self._gizmo["dist"]
        self._gizmo["dist"] = abs(d) if mode == 0 else -abs(d)
        self._refreshPreview(force=True)

    def _onPanelRadius(self, r: float) -> None:
        """面板输入半径：按当前类型设置符号（圆角正/倒直角负）。"""
        if self._gizmo is None:
            return
        m = self._ppMode
        self._gizmo["dist"] = abs(r) if m == 0 else -abs(r)
        self._refreshPreview(force=True)

    def _onPanelFaceMode(self, mode: int) -> None:
        """面操作模式切换：推拉(0)/缩放(1)，数值语义沿用当前 dist。"""
        self._faceMode = mode
        if self._gizmo is not None and self._faceIdx is not None:
            self._refreshPreview(force=True)

    def _onPanelFaceDir(self, did: int) -> None:
        """推拉方向切换（数值重发由面板的 faceValueChanged 槽接力）。"""
        self._faceDir = did

    def _onPanelFaceValue(self, v: float) -> None:
        """面板输入面操作数值：推拉=符号已含方向，缩放=带符号增量。"""
        if self._gizmo is None or self._faceIdx is None:
            return
        self._gizmo["dist"] = v
        self._refreshPreview(force=True)

    def _syncPanel(self, force: bool = False) -> None:
        """选中边/面时同步右侧参数面板（切页 + 回写数值与提示）。"""
        panel = getattr(self, "_panel", None)
        if panel is None or self._gizmo is None:
            return
        try:
            d = self._gizmo["dist"]
            if self._edgeIdx is not None:
                self._ppMode = 0 if d >= 0.0 else 1
                panel.setContext("edge")
                panel.setEdgeMode(self._ppMode)
                panel.setRadius(abs(d))
            elif self._faceIdx is not None:
                panel.setContext("face", _FACE_HINTS.get(
                    self._faceKind, _FACE_HINTS["other"]))
                if self._faceMode == 0:
                    panel.setFaceMode(0)
                    panel.setFaceDir(1 if d < 0.0 else 0)
                    panel.setFaceValue(abs(d))
                else:
                    panel.setFaceMode(1)
                    panel.setFaceValue(d)
        except Exception:
            pass

    # ---------------- 预览 ----------------

    def _showTarget(self, v: bool) -> None:
        """预览期间隐藏/恢复原实体：预览体=“在原模型上”修改的效果。"""
        if self._target is not None:
            try:
                self._target.setVisible(v)
            except Exception:
                pass

    def _refreshPreview(self, force: bool = False) -> None:
        self._updateHighlight()
        shape = self._shape()
        if shape is None or self._gizmo is None:
            self._previewObj = None
            self._showTarget(True)
            if self._manager is not None:
                self._manager.invalidatePreview()
            return
        d = self._gizmo["dist"]
        if abs(d) < 1e-9:
            # 距离为 0（刚选中）：无操作效果，直接显示原体，不发起内核
            # 重建往返——否则"原体先隐藏、预览迟到"会造成整模闪一下
            self._previewObj = None
            self._lastKey = None
            self._showTarget(True)
            if self._manager is not None:
                self._manager.invalidatePreview()
            return
        key = (self._faceIdx, self._edgeIdx, round(d, 2))
        if not force:
            now = time.monotonic() * 1000.0
            if key == self._lastKey:
                return
            if now - self._lastRebuild < _REBUILD_MS:
                return
            self._lastRebuild = now
        self._lastKey = key
        # 重建放后台线程：圆角/直角切换、复杂模型拖动都丝滑。
        # 原实体推迟到预览就绪（_onBgPreviewDone）才隐藏：调度即隐藏
        # 会在内核计算期间让模型整体消失片刻
        self._scheduleBg(shape, d)
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _applyFaceOp(self, shape: BrepShape, fi: int, dist: float,
                     region=None):
        """面操作统一入口：曲线分割区域优先（区域挤出/挖切）；缩放模式
        走 _scaleFaceOp；推拉模式 = 圆柱面改半径，其他面沿手柄轴挤出
        （正）/切料（负）。"""
        bd = _import_bd()
        part = shape.part()
        if region is not None:
            if self._faceMode == 1:
                raise ValueError("分割区域暂不支持缩放，请切回推拉模式")
            rf, n = region[0], region[3]
            if dist >= 0:
                return part + bd.extrude(rf, amount=dist, dir=n)
            return part - bd.extrude(rf, amount=-dist,
                                     dir=(-n[0], -n[1], -n[2]))
        faces = part.faces()
        if fi >= len(faces):
            raise ValueError("所选面已失效")
        face = faces[fi]
        if self._faceMode == 1:
            return _scaleFaceOp(part, face, dist)
        if _isCylinderFace(face):
            return _resizeCylinderOp(part, face, dist)
        # 挤出方向 = 手柄轴（已翻转朝向相机），拖向屏幕=朝屏幕挤出
        g = self._gizmo
        if g is not None:
            n = (g["axis"][0], g["axis"][1], g["axis"][2])
        else:
            nn = face.normal_at(face.center())
            n = (nn.X, nn.Y, nn.Z)
        feat = bd.extrude(face, amount=dist, dir=n)
        if dist >= 0:
            return part + feat
        return part - feat

    def _chamferEdge(self, shape: BrepShape, ei: int, dist: float):
        """线推拉：外拉（正）=倒圆角 Fillet；内推（负）=倒直角 Chamfer。"""
        bd = _import_bd()
        edges = shape.edges()
        if ei >= len(edges):
            raise ValueError("所选边已失效")
        d = abs(dist)
        if d < 1e-9:
            return shape.part()
        if dist > 0:
            return bd.fillet([edges[ei]], d)
        return bd.chamfer([edges[ei]], d)

    # ---------------- 执行 ----------------

    def _apply(self) -> None:
        shape = self._shape()
        if shape is None or self._gizmo is None:
            self._showTarget(True)
            return
        self._showTarget(True)
        d = self._gizmo["dist"]
        if abs(d) < 1e-9:
            self._notify("推拉距离为 0，未应用")
            return
        try:
            if self._faceIdx is not None:
                new_part = self._applyFaceOp(shape, self._faceIdx, d,
                                             self._region)
            elif self._edgeIdx is not None:
                new_part = self._chamferEdge(shape, self._edgeIdx, d)
            else:
                return
        except Exception as e:
            self._notify(f"推拉失败：{e}")
            return
        doc = self._doc
        doc.beginEdit()
        try:
            self._target.setShape(BrepShape.fromPart(new_part))
            try:
                self._target.mesh()
                self._target.topology()
            except Exception:
                pass
        except Exception as e:
            self._notify(f"推拉失败：{e}")
            return
        finally:
            try:
                doc.endEdit()
            except Exception:
                pass
        if self._faceIdx is not None:
            if self._region is not None:
                self._notify(f"{self._region[2]}推拉完成（距离 {d:g}）")
            elif self._faceMode == 1:
                self._notify(f"面缩放完成（增量 {d:g}）")
            else:
                self._notify(f"面推拉完成（距离 {d:g}）")
        elif d > 0:
            self._notify(f"边圆角完成（距离 {d:g}）")
        else:
            self._notify(f"边倒直角完成（距离 {-d:g}）")

    # ---------------- 高亮 ----------------

    def _updateHighlight(self) -> None:
        if self._view is None:
            return
        vp = self._viewport()
        entries = []
        hover = None
        if self._target is not None:
            if self._faceIdx is not None:
                if self._region is not None:
                    entries.append((self._target, "region",
                                    self._region[1]))
                else:
                    entries.append((self._target, "face", self._faceIdx))
            if self._edgeIdx is not None:
                entries.append((self._target, "edge", self._edgeIdx))
        # 悬停高亮（白色提示，与选中黄色区分）；与已选同元素时不重复；
        # 悬停面带曲线分割时直接显示分割区域（预选提示）
        if self._hover is not None:
            same = (self._hover[0] is self._target and
                    self._hover[1] == ("face" if self._faceIdx is not None else
                                       "edge" if self._edgeIdx is not None else None) and
                    self._hover[2] in (self._faceIdx, self._edgeIdx))
            if not same:
                if (self._hover[1] == "face"
                        and self._hoverRegion is not None):
                    hover = (self._hover[0], "region",
                             self._hoverRegion[1])
                else:
                    hover = self._hover
        vp._subSel = entries
        vp._subHover = hover
        vp.update()

    def _clearPick(self) -> None:
        self._showTarget(True)
        self._syncPanel()
        self._faceIdx = None
        self._edgeIdx = None
        self._region = None
        self._gizmo = None
        self._previewObj = None
        self._updateHighlight()
        if self._manager is not None:
            self._manager.invalidatePreview()

    # ---------------- 叠加绘制（手柄） ----------------

    def drawOverlay(self, painter, view) -> None:
        """3D 视口叠加层：手柄箭头 + 距离标签（由视口命令预览阶段调用）。"""
        g = self._gizmo
        if g is None or view is None:
            return
        cam = view._camera()
        w, h = view.width(), view.height()
        ax, ay, az = g["anchor"]
        dx, dy, dz = g["axis"]
        d = g["dist"]
        # 手柄端点随拖动伸缩（至少基长，负方向缩回锚点）
        tip_len = max(_HANDLE_LEN, abs(d) * 1.0)
        tip = (ax + dx * tip_len, ay + dy * tip_len, az + dz * tip_len)
        s0 = view._project((ax, ay, az), cam, w, h)
        s1 = view._project(tip, cam, w, h)
        if s0 is None or s1 is None:
            return
        x0, y0 = s0[0], s0[1]
        x1, y1 = s1[0], s1[1]
        # 轴方向单位屏幕向量
        vx, vy = x1 - x0, y1 - y0
        vl = math.hypot(vx, vy)
        if vl < 1e-9:
            return
        ux, uy = vx / vl, vy / vl
        # 箭头尖（屏幕固定长度，较短的视觉箭头）
        alen = 9.0
        axx, ayy = x1 - ux * alen, y1 - uy * alen
        arrow = QPolygonF([
            QPointF(x1, y1),
            QPointF(axx - uy * alen * 0.5, ayy + ux * alen * 0.5),
            QPointF(axx + uy * alen * 0.5, ayy - ux * alen * 0.5),
        ])
        pen = QPen(QColor("#ff9800"), 2.2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QColor("#ff9800"))
        painter.drawLine(QPointF(x0, y0), QPointF(axx, ayy))
        painter.drawPolygon(arrow)
        # 抓取圆圈（拖拽命中区）：橙环+白心，悬停/拖拽时变亮黄高亮
        gr = _GRAB_PX * 0.85
        ring = QColor("#ffd54f") if self._gizmoHover else QColor("#ff9800")
        rpen = QPen(ring, 2.6 if self._gizmoHover else 2.2)
        rpen.setCosmetic(True)
        painter.setPen(rpen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(x0, y0), gr, gr)
        painter.setBrush(QColor("#ffd54f") if self._gizmoHover
                         else QColor("#ffffff"))
        painter.drawEllipse(QPointF(x0, y0), gr * 0.5, gr * 0.5)
        # 距离标签
        if abs(d) > 1e-9:
            painter.setFont(QFont("Arial", 10))
            painter.setPen(QPen(QColor("#ff9800"), 1.0))
            painter.drawText(QPointF(x1 + ux * 8, y1 + uy * 8 - 6),
                             f"{d:+.2f}")

    # ---------------- 工具 ----------------

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            try:
                self._manager.notifyResult(str(text))
            except Exception:
                pass


def registerAll(manager) -> None:
    manager.register("PushPull", PushPullCommand)
