"""B-Rep 实体对象：持有 PolySolid（自研轻量）或 BrepShape（build123d 内核）。

实体 = build123d Part（BRepShape）或闭合多边形面集（PolySolid），
三角化/拓扑/变换由内核提供，对外 mesh/topology/faces/shape/setShape/
translate/rotate/scale/serialize 接口一致，渲染与拾取层无需感知内核差异。
"""
from __future__ import annotations
from typing import List, Optional, Set

from .base_object import BaseObject
from .geometry_types import Point3D, BoundingBox3D, ObjectType
from .poly_solid import PolySolid
from .brep_shape import BrepShape


def isOCCAvailable() -> bool:
    """兼容入口：轻量内核永远可用（不再依赖 OpenCASCADE）。"""
    return True


def _tessellate(ps, deflection: float = 0.05, angular: float = 0.05):
    """PolySolid → 渲染网格（轻量三角化；参数仅兼容旧调用）。"""
    if ps is None:
        return None
    m = ps.triangulate()
    if m is None or len(m["tris"]) == 0:
        return None
    return m


def _edgePolylinesOfShape(ps, angular: float = 0.05):
    """PolySolid → 特征边折线列表（直线段，每边 2 点）。"""
    if ps is None:
        return []
    return ps.edgePolylines()


# 实体捕捉特征数上限：超复杂实体截断，保证鼠标移动时捕捉即时
_SNAP_FEAT_MAX = 600


def _quarterInArc(k: int, a0: float, a1: float):
    """第 k 个象限角（k·π/2）折算进 [a0, a1] 参数范围，不在则 None。"""
    import math as _m
    q = a0 + ((k * _m.pi / 2.0 - a0) % (2.0 * _m.pi))
    return q if q <= a1 + 1e-6 else None


def _extractSnapFeatures(ps) -> list:
    """BrepShape → 解析捕捉特征列表（供 SnapManager 吸附实体）：

    圆边（圆柱顶/底圆、圆环边等）→ 圆心/象限/端点/中点/最近点；
    直线边 → 端点/中点/最近点；直线端点去重 → 顶点。
    圆的端点/象限点在圆自身坐标系内预生成（不依赖世界 XY 假设），
    距离度量由 SnapManager 按捕捉平面处理。仅 build123d 内核实体
    支持解析提取；PolySolid 无圆边概念，返回空。"""
    import math as _m
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_CurveType as _CT
    feats: list = []
    verts = {}
    for e in ps.edges():
        if len(feats) >= _SNAP_FEAT_MAX:
            break
        try:
            ad = BRepAdaptor_Curve(e.wrapped)
            ct = ad.GetType()
            u0, u1 = ad.FirstParameter(), ad.LastParameter()
            if ct == _CT.GeomAbs_Circle:
                circ = ad.Circle()
                pos2 = circ.Position()          # gp_Ax2：主轴 + X 方向
                axd = pos2.Direction()
                exd = pos2.XDirection()
                loc = circ.Location()
                ax = (axd.X(), axd.Y(), axd.Z())
                ex = (exd.X(), exd.Y(), exd.Z())
                ey = (ax[1] * ex[2] - ax[2] * ex[1],
                      ax[2] * ex[0] - ax[0] * ex[2],
                      ax[0] * ex[1] - ax[1] * ex[0])
                r = circ.Radius()
                if r <= 1e-9:
                    continue
                o = (loc.X(), loc.Y(), loc.Z())

                def pt(u, o=o, r=r, ex=ex, ey=ey):
                    cu, su = _m.cos(u), _m.sin(u)
                    return Point3D(o[0] + r * (cu * ex[0] + su * ey[0]),
                                   o[1] + r * (cu * ex[1] + su * ey[1]),
                                   o[2] + r * (cu * ex[2] + su * ey[2]))

                full = abs((u1 - u0) - 2.0 * _m.pi) < 1e-6
                f = {"kind": "circle", "center": o, "radius": r,
                     "axis": ax, "ex": ex, "ey": ey, "full": full,
                     "a0": u0, "a1": u1, "quarters": [], "endPts": [],
                     "midPt": None}
                for k in range(4):
                    q = _quarterInArc(k, u0, u1)
                    if q is not None:
                        f["quarters"].append(pt(q))
                if not full:
                    f["endPts"] = [pt(u0), pt(u1)]
                    f["midPt"] = pt(0.5 * (u0 + u1))
                feats.append(f)
            elif ct == _CT.GeomAbs_Line:
                p0, p1 = ad.Value(u0), ad.Value(u1)
                a = Point3D(p0.X(), p0.Y(), p0.Z())
                b = Point3D(p1.X(), p1.Y(), p1.Z())
                if a.distanceTo(b) < 1e-9:
                    continue
                feats.append({"kind": "line", "a": a, "b": b})
                for p in (a, b):
                    verts[(round(p.x, 6), round(p.y, 6),
                           round(p.z, 6))] = p
        except Exception:
            continue
    feats.extend({"kind": "vertex", "p": p} for p in verts.values())
    return feats


class BRepObject(BaseObject):
    """轻量 B-Rep 实体（ObjectType.BREP）：持有 PolySolid。

    三角化/拓扑按需缓存（_dirty 时重建）。子元素选中（面/边/顶点）用
    拓扑索引（与 topology() 同序）。
    """

    _MESH_TRI_MAX = 40000   # 渲染网格三角形上限（超出仅提示，不降密）

    def __init__(self) -> None:
        super().__init__(ObjectType.BREP)
        self._ps: Optional[object] = None   # PolySolid | BrepShape
        self._meshCache: Optional[dict] = None
        self._topoCache: Optional[dict] = None
        # 子元素选中（P2）：存拓扑索引
        self._selectedFaces: Set[int] = set()
        self._selectedEdges: Set[int] = set()
        self._selectedVertices: Set[int] = set()
        # 源圆信息（圆轮廓挤出时记录）：STL 导出按解析圆高分段重采样消除棱角
        self._sourceCircle: Optional[dict] = None
        # 网格快照恢复的 STL 模型（deserialize 走 {"mesh"} 分支时置 True）
        self._fromMesh: bool = False
        # 捕捉特征缓存（圆/直线/顶点）：setShape/变换时失效
        self._snapFeatCache: Optional[list] = None

    # ---------------- 几何管理 ----------------

    def shape(self):
        """返回内部 PolySolid（兼容旧 shape() 语义）。"""
        return self._ps

    def setShape(self, s) -> None:
        """设置实体：接受 PolySolid / BrepShape / 另一个 BRepObject / 面点列。"""
        if isinstance(s, BRepObject):
            s = s._ps
        if isinstance(s, list):
            s = PolySolid.fromFacePoints(s)
        if s is not None and not isinstance(s, (PolySolid, BrepShape)):
            raise TypeError("setShape 需要 PolySolid/BrepShape/BRepObject/面点列")
        self._ps = s
        self._dirty = True
        self._meshCache = None
        self._topoCache = None
        self._snapFeatCache = None

    def setPolySolid(self, ps) -> None:
        self.setShape(ps)

    def isClosed(self) -> bool:
        return self._ps is not None and self._ps.isClosed()

    # ---------------- 网格化（渲染 / 拾取共用缓存） ----------------

    def mesh(self, angular: float = 0.05) -> Optional[dict]:
        """渲染网格 {verts, normals, tris, faceRanges}；按角步长缓存。"""
        if self._ps is None:
            return None
        key = round(angular, 4)
        cache = self._meshCache
        if isinstance(cache, dict) and key in cache:
            return cache[key]
        m = self._ps.triangulate()
        if m is not None and len(m["tris"]) == 0:
            m = None
        if not isinstance(self._meshCache, dict):
            self._meshCache = {}
        if len(self._meshCache) >= 3:
            self._meshCache.pop(next(iter(self._meshCache)))
        self._meshCache[key] = m
        self._dirty = False
        return m

    def setShapeCoarse(self, ps) -> None:
        """设置实体并立即构建粗显示网格（拖动预览专用：避免重建重复三角化）。"""
        self.setShape(ps)
        try:
            self._meshCache = {0.05: self._ps.triangulate() if self._ps else None}
        except Exception:
            self._meshCache = {}

    def triangles(self):
        """(T,3,3) 世界坐标三角形（兼容旧接口）。"""
        import numpy as np
        mesh = self.mesh()
        if mesh is None:
            return np.zeros((0, 3, 3), dtype=np.float64)
        return mesh["verts"][mesh["tris"]]

    def faces(self) -> List[List[Point3D]]:
        """全部面（多边形点列）。"""
        if self._ps is None:
            return []
        return self._ps.facePoints()

    def topPolyline(self) -> List[Point3D]:
        """顶面轮廓（faces()[1]，挤出体顶面；无则空列表）。"""
        f = self.faces()
        if isinstance(self._ps, BrepShape):
            # build123d 内核：faces() 为三角片，顶面轮廓不作拓扑语义
            # （3D 渲染走 z-buffer 通道 + 特征边，不依赖本方法）
            return []
        return f[1] if len(f) > 1 else []

    def sourceCircle(self) -> Optional[dict]:
        return self._sourceCircle

    def setSourceCircle(self, info: Optional[dict]) -> None:
        self._sourceCircle = (dict(info) if info is not None else None)

    def setFacePoints(self, faces: List[List[Point3D]]) -> None:
        self.setShape(faces)

    # ---------------- 拓扑枚举（P2 子元素拾取） ----------------

    def topology(self, angular: float = 0.05) -> dict:
        """拓扑缓存：faces（面点列）、faceRanges、edgePolylines、vertices。"""
        if self._ps is None:
            return {"faces": [], "faceRanges": [], "edgePolylines": [],
                    "vertices": []}
        key = round(angular, 4)
        cache = self._topoCache
        if isinstance(cache, dict) and key in cache:
            return cache[key]
        mesh = self.mesh(key)
        ranges = list(mesh["faceRanges"]) if mesh is not None else []
        data = {
            "faces": self._ps.facePoints(),
            "faceRanges": ranges,
            "edgePolylines": self._ps.featureEdgePolylines(),
            "vertices": list(self._ps.verts()),
        }
        if not isinstance(self._topoCache, dict):
            self._topoCache = {}
        if len(self._topoCache) >= 3:
            self._topoCache.pop(next(iter(self._topoCache)))
        self._topoCache[key] = data
        return data

    def snapFeatures(self) -> list:
        """捕捉特征（圆/直线/顶点），供 SnapManager 吸附实体子元素：
        在圆柱顶面附近画圆可吸到顶圆真实圆心（位置带真实 Z）。
        按形状缓存，setShape/translate/rotate/scale/快照恢复时失效。"""
        if self._snapFeatCache is not None:
            return self._snapFeatCache
        out: list = []
        if isinstance(self._ps, BrepShape):
            try:
                out = _extractSnapFeatures(self._ps)
            except Exception:
                out = []
        self._snapFeatCache = out
        return out

    # ---------------- 变换 ----------------

    def translate(self, dx: float, dy: float, dz: float) -> None:
        if self._ps is not None:
            self._ps.translate(dx, dy, dz)
            self._dirty = True
            self._meshCache = None
            self._topoCache = None
            self._snapFeatCache = None

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        if self._ps is not None:
            self._ps.rotate(angle, ax, ay, az)
            self._dirty = True
            self._meshCache = None
            self._topoCache = None
            self._snapFeatCache = None

    def scale(self, sx: float, sy: float, sz: float) -> None:
        if self._ps is not None:
            self._ps.scale(sx, sy, sz)
            self._dirty = True
            self._meshCache = None
            self._topoCache = None
            self._snapFeatCache = None

    def getBoundingBox(self) -> BoundingBox3D:
        if self._ps is None:
            return BoundingBox3D()
        return self._ps.boundingBox()

    def getPosition(self) -> Point3D:
        bb = self.getBoundingBox()
        if not bb.isValid():
            return Point3D()
        return bb.center()

    def clone(self) -> "BRepObject":
        o = BRepObject()
        if self._ps is not None:
            o._ps = self._ps.clone()
        o._color = self._color
        o._lineWidth = self._lineWidth
        o._sourceCircle = (dict(self._sourceCircle)
                           if self._sourceCircle else None)
        o._fromMesh = self._fromMesh
        # 派生缓存随 shape 一并共享（BrepShape.clone 已共享内核缓存）：
        # 快照克隆丢缓存会导致每次撤销快照全量重三角化（文档实体越多，
        # 点击任何命令都卡一次）。缓存按 angular 键整体重绑，浅拷贝安全。
        if isinstance(self._meshCache, dict):
            o._meshCache = dict(self._meshCache)
        if isinstance(self._topoCache, dict):
            o._topoCache = self._topoCache
        if self._snapFeatCache is not None:
            o._snapFeatCache = self._snapFeatCache
        o._dirty = self._dirty
        return o

    # ---------------- 子元素选中（P2） ----------------

    def selectedFaces(self) -> Set[int]:
        return self._selectedFaces

    def selectedEdges(self) -> Set[int]:
        return self._selectedEdges

    def selectedVertices(self) -> Set[int]:
        return self._selectedVertices

    def clearSubSelection(self) -> None:
        self._selectedFaces.clear()
        self._selectedEdges.clear()
        self._selectedVertices.clear()

    # ---------------- 序列化 ----------------

    def serialize(self) -> dict:
        d = super().serialize()
        if self._ps is not None:
            if isinstance(self._ps, BrepShape):
                sd = self._ps.serialize()
                if sd:
                    d["brepShape"] = sd
            else:
                d["polySolid"] = self._ps.serialize()
        if self._sourceCircle is not None:
            d["sourceCircle"] = dict(self._sourceCircle)
        return d

    def deserialize(self, data: dict) -> None:
        super().deserialize(data)
        pd = data.get("polySolid")
        if isinstance(pd, dict):
            self.setShape(PolySolid.deserialize(pd))
        else:
            bs = data.get("brepShape")
            if isinstance(bs, dict):
                try:
                    if isinstance(bs.get("mesh"), dict):
                        # STEP 导出失败的降级网格 → 轻量 PolySolid（每三角一面）
                        verts = bs["mesh"].get("verts", [])
                        tris = bs["mesh"].get("tris", [])
                        faces = [[Point3D(*verts[i]) for i in tri]
                                 for tri in tris]
                        self.setShape(PolySolid.fromFacePoints(faces))
                        self._fromMesh = True
                    else:
                        self.setShape(BrepShape.deserialize(bs))
                except Exception:
                    self._ps = None
        sc = data.get("sourceCircle")
        self._sourceCircle = (dict(sc) if isinstance(sc, dict) else None)

    # ---------------- 撤销快照（内存克隆，绝不 export_step） ----------------

    def snapshot(self):
        """撤销快照：内存克隆 BRepObject（BrepShape 共享不可变 part）。

        不序列化到 STEP——加载恢复的 shape 再 export_step 会失败
        （build123d 限制），且每次命令全量导出又慢又易崩（用户报的
        RuntimeError: Failed to write STEP file）。
        """
        return self.clone()

    def restoreSnapshot(self, snap):
        """由 snapshot() 快照恢复：对象实例直接取 shape，dict 走 deserialize。"""
        if isinstance(snap, BRepObject):
            self._ps = snap._ps
            self._sourceCircle = (dict(snap._sourceCircle)
                                  if snap._sourceCircle else None)
            self._fromMesh = snap._fromMesh
            self._dirty = True
            self._meshCache = None
            self._topoCache = None
            self._snapFeatCache = None
        else:
            self.deserialize(snap)

    def matchesSnapshot(self, snap) -> bool:
        """当前内容与快照是否一致：part 引用比较（BrepShape 不可变，
        修改=生成新 part；PolySolid 按实例身份）。"""
        if isinstance(snap, BRepObject):
            return self._shapeSameAs(snap._ps)
        return False

    def _shapeSameAs(self, other_ps) -> bool:
        a, b = self._ps, other_ps
        if a is None or b is None:
            return a is b
        pa = getattr(a, "part", None)
        pb = getattr(b, "part", None)
        if pa is not None and pb is not None:
            try:
                return pa() is pb()
            except Exception:
                return False
        return a is b

    def __eq__(self, other):
        if not isinstance(other, BRepObject):
            return NotImplemented
        return self._shapeSameAs(other._ps)

    __hash__ = object.__hash__

    def snapshotSize(self, snap) -> int:
        """快照计费：按三角网格大小估算（BRep 精确数据不大，网格主导）。

        用 self 的缓存计费（与 snap 同一 part，网格一致）——对克隆体
        mesh() 会在克隆缓存缺失时触发整轮重三角化。"""
        try:
            if isinstance(snap, BRepObject):
                m = self.mesh()
                if m:
                    return int(len(m.get("tris", [])) * 8)
            else:
                ps = snap.get("polySolid") if isinstance(snap, dict) else None
                if isinstance(ps, dict):
                    return sum(len(f) for f in ps.get("faces", []))
        except Exception:
            pass
        return 0


# ---------------- 构造辅助 ----------------

def makeBox(dx: float, dy: float, dz: float) -> "BRepObject":
    """立方体（原点角点起，尺寸 dx×dy×dz）→ BRepObject。"""
    x0, y0, z0 = 0.0, 0.0, 0.0
    x1, y1, z1 = dx, dy, dz
    pts = lambda a, b, c: Point3D(a, b, c)
    faces = [
        # 底面（-Z）与顶面（+Z）：外侧逆时针
        [pts(x0, y0, z0), pts(x1, y0, z0), pts(x1, y1, z0), pts(x0, y1, z0)],
        [pts(x0, y1, z1), pts(x1, y1, z1), pts(x1, y0, z1), pts(x0, y0, z1)],
        # 侧面
        [pts(x0, y0, z0), pts(x0, y1, z0), pts(x0, y1, z1), pts(x0, y0, z1)],
        [pts(x1, y0, z0), pts(x1, y0, z1), pts(x1, y1, z1), pts(x1, y1, z0)],
        [pts(x0, y0, z0), pts(x0, y0, z1), pts(x1, y0, z1), pts(x1, y0, z0)],
        [pts(x0, y1, z0), pts(x1, y1, z0), pts(x1, y1, z1), pts(x0, y1, z1)],
    ]
    obj = BRepObject()
    obj.setShape(faces)
    return obj


def makeRingExtrude(cx, cy, cz, rOuter, rInner, height,
                    nseg: int = 96) -> "BRepObject":
    """圆环挤出：外圆 + 内孔（反向）+ 沿 Z 挤出 → BRepObject。"""
    import math
    outer = [Point3D(cx + rOuter * math.cos(2 * math.pi * i / nseg),
                     cy + rOuter * math.sin(2 * math.pi * i / nseg), cz)
             for i in range(nseg)]
    inner = [Point3D(cx + rInner * math.cos(2 * math.pi * i / nseg),
                     cy + rInner * math.sin(2 * math.pi * i / nseg), cz)
             for i in range(nseg)]
    zt = cz + height
    topOuter = [Point3D(p.x, p.y, zt) for p in outer]
    topInner = [Point3D(p.x, p.y, zt) for p in inner]
    faces = [outer, list(reversed(inner)),        # 底面（内孔反向）
             topOuter, list(reversed(topInner))]  # 顶面（内孔反向）
    for i in range(nseg):
        j = (i + 1) % nseg
        faces.append([outer[i], outer[j], topOuter[j], topOuter[i]])
        faces.append([inner[i], topInner[i], topInner[j], inner[j]])
    obj = BRepObject()
    obj.setShape(faces)
    return obj
