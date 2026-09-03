"""组合（Join）：把端点重合的开放曲线首尾连接成一条多段曲线。

- JOIN_TOLERANCE：端点判定为“重合”的距离容差（模型单位）。
- objectToSegments：把可合并的开放曲线对象转为 PolyCurve 段列表。
- joinObjects：对一组对象执行合并，返回每组合并结果及其来源对象。

设计：
- 参与合并的对象：LINE / CURVE / POLYCURVE / ARC / NURBS_CURVE 中“开放”者
  （首尾端点不重合）。CIRCLE / ELLIPSE / 闭合曲线没有可接端点，跳过。
- 用端点连接图贪心连链：任一端点距离 < 容差即连接；按需反转段保持连续。
- 结果生成 PolyCurveObject（保留每段原始几何与控制点），外观继承链首对象。
"""
from __future__ import annotations
import math
from typing import List, Optional, Tuple

from .geometry_types import Point3D
from .poly_curve_object import PolyCurveObject

JOIN_TOLERANCE = 1e-4  # 模型单位；吸附对齐的端点通常误差远小于此


def _copySeg(s: dict) -> dict:
    c = {"points": [list(p) for p in s.get("points", [])]}
    if s.get("inputPoints"):
        c["inputPoints"] = [list(p) for p in s["inputPoints"]]
    return c


def _reverseSeg(s: dict) -> dict:
    c = {"points": [list(p) for p in reversed(s.get("points", []))]}
    if s.get("inputPoints"):
        c["inputPoints"] = [list(p) for p in reversed(s["inputPoints"])]
    return c


def _pts(seq) -> List[Point3D]:
    return [Point3D(*p) for p in seq]


def _segmentsOf(obj) -> Optional[List[dict]]:
    """把对象转为段列表；不可合并（闭合/无端点/不支持）返回 None。"""
    t = obj.type().name
    if t == "LINE":
        if obj.isClosed():
            return None
        pts = obj.points()
        if len(pts) < 2:
            return None
        return [{"points": [[p.x, p.y, p.z] for p in pts]}]
    if t == "CURVE":
        if obj.isEmpty() or obj.isClosed():
            return None
        samples = obj._sampledPoints or obj.getSampledPoints()
        if len(samples) < 2:
            return None
        seg = {"points": [[p.x, p.y, p.z] for p in samples]}
        inp = obj.inputPoints()
        if inp:
            seg["inputPoints"] = [[p.x, p.y, p.z] for p in inp]
        return [seg]
    if t == "NURBS_CURVE":
        pts = obj.getSampledPoints()
        if len(pts) < 2:
            return None
        return [{"points": [[p.x, p.y, p.z] for p in pts]}]
    if t == "POLYCURVE":
        segs = [_copySeg(s) for s in obj.segments()]
        if not segs:
            return None
        return segs
    if t == "ARC":
        poly = obj.toPolycurve()
        if poly is None or not poly.segments():
            return None
        return [_copySeg(s) for s in poly.segments()]
    return None


def _buildChains(entries, tolerance: float):
    """贪心连链。entries: [{head, tail, ...}]。
    返回链列表，每条链为 [(idx, reverse), ...]（按首→尾方向）。"""
    unused = set(range(len(entries)))
    chains = []
    while unused:
        start = unused.pop()
        chain = [(start, False)]
        cur_head = entries[start]["head"]
        cur_tail = entries[start]["tail"]
        extended = True
        while extended:
            extended = False
            # 向后扩展：当前尾 接 另一条的首/尾
            for i in list(unused):
                e = entries[i]
                if e["head"].distanceTo(cur_tail) < tolerance:
                    chain.append((i, False))
                    cur_tail = e["tail"]
                    unused.discard(i)
                    extended = True
                    break
                if e["tail"].distanceTo(cur_tail) < tolerance:
                    chain.append((i, True))
                    cur_tail = e["head"]
                    unused.discard(i)
                    extended = True
                    break
            if extended:
                continue
            # 向前扩展：当前首 接 另一条的首/尾（插入链头）
            for i in list(unused):
                e = entries[i]
                if e["tail"].distanceTo(cur_head) < tolerance:
                    chain.insert(0, (i, False))
                    cur_head = e["head"]
                    unused.discard(i)
                    extended = True
                    break
                if e["head"].distanceTo(cur_head) < tolerance:
                    chain.insert(0, (i, True))
                    cur_head = e["tail"]
                    unused.discard(i)
                    extended = True
                    break
        chains.append(chain)
    return chains


def joinObjects(objects, tolerance: float = JOIN_TOLERANCE):
    """合并一组对象中端点重合的开放曲线。

    返回 [(poly_object, [source_objects]), ...]：
    每组合并结果为一个新的 PolyCurveObject，source_objects 为组成该链的原对象。
    """
    entries = []
    for obj in objects:
        segs = _segmentsOf(obj)
        if not segs:
            continue
        head_pt = segs[0].get("points", [])
        tail_pt = segs[-1].get("points", [])
        if not head_pt or not tail_pt:
            continue
        head = Point3D(*head_pt[0])
        tail = Point3D(*tail_pt[-1])
        if head.distanceTo(tail) < tolerance:
            continue  # 自身闭合（如圆/闭合多段线），无可接端点
        entries.append({"obj": obj, "segs": segs, "head": head, "tail": tail})

    chains = _buildChains(entries, tolerance)
    results = []
    for chain in chains:
        if len(chain) < 2:
            continue
        poly = PolyCurveObject()
        objs = []
        for idx, rev in chain:
            segs = entries[idx]["segs"]
            if rev:
                for s in reversed(segs):
                    poly.addSegment(_reverseSeg(s))
            else:
                for s in segs:
                    poly.addSegment(_copySeg(s))
            objs.append(entries[idx]["obj"])
        first = entries[chain[0][0]]["obj"]
        poly._color = first._color
        poly._lineWidth = first._lineWidth
        poly._layerId = first.layerId()
        results.append((poly, objs))
    return results
