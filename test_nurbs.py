# -*- coding: utf-8 -*-
"""临时验证：NURBS 采样重构正确性 + 性能。"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.spline_utils import sampleNurbs, nurbsBasisVector, evaluateNurbsBasis
from core.vector.geometry_types import Point3D
from core.vector.nurbs_curve_object import NurbsCurveObject

fails = []


def check(name, ok):
    print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    if not ok:
        fails.append(name)


# 1) 端点插值：clamped 节向量下采样首末点 = 首末控制点
cps = [Point3D(0, 0, 0), Point3D(10, 20, 0), Point3D(30, 10, 0), Point3D(40, 30, 0)]
n = len(cps)
k = 3
knots = [0.0] * (k + 1) + [float(i) for i in range(1, n - k)] + [float(n - k)] * (k + 1)
pts = sampleNurbs(3, cps, None, knots, 200)
check("首点=首控制点", math.hypot(pts[0].x - cps[0].x, pts[0].y - cps[0].y) < 1e-6)
check("末点=末控制点", math.hypot(pts[-1].x - cps[-1].x, pts[-1].y - cps[-1].y) < 1e-6)

# 2) 向量版与逐 i 版本一致 + 贝塞尔段基函数精确
N = nurbsBasisVector(3, knots, 0.37)
ok = all(abs(N[i] - evaluateNurbsBasis(3, knots, i, 0.37)) < 1e-12 for i in range(n))
check("基函数向量与逐项求值一致", ok)
check("基函数归一（ΣN=1）", abs(sum(N) - 1.0) < 1e-9)
# 贝塞尔段真值：(1-t)³, 3t(1-t)², 3t²(1-t), t³
truth = ((1 - 0.37) ** 3, 3 * 0.37 * (1 - 0.37) ** 2,
         3 * 0.37 ** 2 * (1 - 0.37), 0.37 ** 3)
check("贝塞尔段基函数精确（旧实现失真）",
      all(abs(N[i] - truth[i]) < 1e-9 for i in range(4)))

# 3) 采样缓存：变换后失效、未变换时复用
obj = NurbsCurveObject.create(3, cps)
s1 = obj.getSampledPoints()
s2 = obj.getSampledPoints()
check("采样缓存复用同一列表", s1 is s2)
obj.translate(5, 0, 0)
s3 = obj.getSampledPoints()
check("变换后缓存失效", s3 is not s1 and abs(s3[0].x - 5.0) < 1e-6)

# 4) 性能：100 控制点 × 200 采样（旧实现 O(samples·n²·degree)）
big = [Point3D(i * 10, (i * 37) % 100, 0) for i in range(100)]
kn = [0.0] * 4 + [float(i) for i in range(1, 97)] + [97.0] * 4
t0 = time.perf_counter()
res = sampleNurbs(3, big, None, kn, 200)
dt = (time.perf_counter() - t0) * 1000
print(f"  (信息) 100 控制点 × 200 采样: {dt:.0f} ms, {len(res)} 点")
check("大曲线采样 < 500ms", dt < 500.0)

print("=== NURBS 采样测试 ===")
print("=== 全部通过 ===" if not fails else f"=== 失败 {fails} ===")
sys.exit(1 if fails else 0)
