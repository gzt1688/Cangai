"""阵列算法核心：矩形/圆形/曲线阵列（移植自 Rhino 插件 ArrayGenerator.cs）。

所有阵列副本均通过 clone + translate/rotate 生成，不依赖原对象。
"""
from __future__ import annotations
import math
from typing import List, Optional, Tuple

from .geometry_types import Point3D, BoundingBox3D


def getObjectsBBox(objects) -> BoundingBox3D:
    """取一组对象并集包围盒。"""
    bb = BoundingBox3D()
    for obj in objects:
        objBb = obj.getBoundingBox()
        if objBb.isValid():
            bb.expand(objBb.min)
            bb.expand(objBb.max)
    return bb


def getObjectsCenter(objects) -> Point3D:
    """取包围盒中心作为对象"中心"。"""
    bb = getObjectsBBox(objects)
    if not bb.isValid():
        return Point3D()
    return bb.center()


def duplicateAndAdd(doc, objects, dx: float, dy: float, dz: float,
                    angle: float = 0.0) -> None:
    """复制每个对象，先绕自身包围盒中心旋转 angle（仅改朝向不动位置基准），
    再平移 (dx,dy,dz) 到目标位置后加入文档。"""
    for obj in objects:
        clone = obj.clone()
        if abs(angle) > 1e-9:
            c = clone.getBoundingBox().center()
            clone.rotateAround(angle, c)
        clone.translate(dx, dy, dz)
        doc.addObject(clone)


# ==================== 矩形阵列 ====================

def rectangularArray(doc, objects, xCount: int, yCount: int,
                      xGap: float, yGap: float) -> None:
    """矩形阵列：以包围盒尺寸+间隙为步距，二重循环平移复制。"""
    if not objects or xCount < 1 or yCount < 1:
        return
    bb = getObjectsBBox(objects)
    if not bb.isValid():
        return
    stepX = bb.width() + xGap
    stepY = bb.height() + yGap
    for i in range(xCount):
        for j in range(yCount):
            if i == 0 and j == 0:
                continue  # 跳过原始位置
            duplicateAndAdd(doc, objects, i * stepX, j * stepY, 0)


# ==================== 圆形阵列 ====================

def circularArray(doc, objects, center: Point3D, count: int,
                  radius: float, startAngle: float, angleRange: float,
                  rotateObjects: bool = True, knownR: bool = False,
                  ringCount: int = 1, ringSpacing: float = 0.0) -> None:
    """圆形阵列：绕 center 沿圆周分布 count 个副本，支持多圈环形阵列。

    knownR=True：已知半径模式，首圈跳过 i=0（选中对象为阵列之一）。
    ringCount：圈数，ringSpacing：圈间距，每圈半径叠加 ring*ringSpacing。
    """
    if not objects or count < 2:
        return
    objCenter = getObjectsCenter(objects)
    if radius <= 0:
        radius = objCenter.distanceTo(center)
    if radius < 1e-6:
        return
    baseAngle = math.atan2(objCenter.y - center.y, objCenter.x - center.x)
    startRad = math.radians(startAngle)
    rangeRad = math.radians(angleRange)
    stepAngle = rangeRad / count
    for ring in range(ringCount):
        ringRadius = radius + ring * ringSpacing
        if ringRadius < 1e-6:
            continue
        isFirstRing = (ring == 0)
        startI = 1 if (knownR and isFirstRing) else 0
        for i in range(startI, count):
            angle = baseAngle + startRad + i * stepAngle
            targetX = center.x + ringRadius * math.cos(angle)
            targetY = center.y + ringRadius * math.sin(angle)
            dx = targetX - objCenter.x
            dy = targetY - objCenter.y
            rotAngle = i * stepAngle if rotateObjects else 0.0
            duplicateAndAdd(doc, objects, dx, dy, 0, rotAngle)


# ==================== 曲线阵列 ====================

def _curveLengthAt(pts: List[Point3D], idx: int, t: float) -> float:
    """采样点列上到第 idx 段参数 t 处的累积弧长。"""
    total = 0.0
    for k in range(idx):
        total += pts[k].distanceTo(pts[k + 1])
    if idx < len(pts) - 1:
        total += pts[idx].distanceTo(pts[idx + 1]) * t
    return total


def _lengthToParam(pts: List[Point3D], targetLen: float) -> Optional[Tuple[int, float, Point3D]]:
    """由弧长反求 (段索引, 段参数 t, 点坐标)。"""
    if len(pts) < 2:
        return None
    cumul = 0.0
    for i in range(len(pts) - 1):
        segLen = pts[i].distanceTo(pts[i + 1])
        if cumul + segLen >= targetLen:
            if segLen < 1e-9:
                return i, 0.0, pts[i]
            t = (targetLen - cumul) / segLen
            t = max(0.0, min(1.0, t))
            p = Point3D(
                pts[i].x + (pts[i + 1].x - pts[i].x) * t,
                pts[i].y + (pts[i + 1].y - pts[i].y) * t,
                pts[i].z + (pts[i + 1].z - pts[i].z) * t,
            )
            return i, t, p
        cumul += segLen
    return None


def _tangentAt(pts: List[Point3D], idx: int) -> Point3D:
    """采样点列在 idx 处的切向（单位向量）。"""
    if idx >= len(pts) - 1:
        idx = len(pts) - 2
    if idx < 0:
        return Point3D(1, 0, 0)
    d = pts[idx + 1] - pts[idx]
    L = d.length()
    if L < 1e-9:
        return Point3D(1, 0, 0)
    return Point3D(d.x / L, d.y / L, d.z / L)


def curveArray(doc, objects, curvePts: List[Point3D], count: int,
               spacing: float, fillCurve: bool, startPosPercent: float,
               rotateTangent: bool, onCurve: bool) -> None:
    """曲线阵列：沿采样点列弧长分布副本。

    curvePts: 路径曲线采样点列
    fillCurve: 铺满模式（按 count-1 等分）
    startPosPercent: 起点百分比 0-100
    rotateTangent: 旋转跟随切线
    onCurve: True=物体中心贴曲线；False=保留原相对偏移
    """
    if not objects or not curvePts or count < 1:
        return
    baseCenter = getObjectsCenter(objects)
    totalLength = sum(curvePts[i].distanceTo(curvePts[i + 1])
                      for i in range(len(curvePts) - 1))
    if totalLength < 1e-6:
        return

    startLen = totalLength * startPosPercent / 100.0

    # 计算每个副本沿曲线的弧长位置
    targetLengths: List[float] = []
    if fillCurve:
        if count < 2:
            return
        remaining = totalLength - startLen
        step = remaining / (count - 1)
        for i in range(count):
            targetLengths.append(startLen + i * step)
    else:
        if spacing <= 0:
            return
        for i in range(count):
            l = startLen + i * spacing
            if l <= totalLength + 1e-6:
                targetLengths.append(l)

    # 由弧长反求点位与切线
    points: List[Point3D] = []
    tangents: List[Point3D] = []
    for l in targetLengths:
        res = _lengthToParam(curvePts, l)
        if res is None:
            continue
        idx, t, p = res
        points.append(p)
        tangents.append(_tangentAt(curvePts, idx))
    if not points:
        return

    # 起点偏移
    startPt = points[0]
    offsetVec = Point3D(baseCenter.x - startPt.x, baseCenter.y - startPt.y,
                        baseCenter.z - startPt.z)
    startTan = tangents[0]

    for i in range(len(points)):
        pt = points[i]
        tan = tangents[i]
        if onCurve:
            targetPt = pt
        else:
            targetPt = Point3D(pt.x + offsetVec.x, pt.y + offsetVec.y,
                               pt.z + offsetVec.z)
        dx = targetPt.x - baseCenter.x
        dy = targetPt.y - baseCenter.y
        dz = targetPt.z - baseCenter.z
        rotAngle = 0.0
        if rotateTangent and i > 0:
            # 起切线与当前切线的夹角（XY 平面）
            angle1 = math.atan2(startTan.y, startTan.x)
            angle2 = math.atan2(tan.y, tan.x)
            rotAngle = angle2 - angle1
        duplicateAndAdd(doc, objects, dx, dy, dz, rotAngle)