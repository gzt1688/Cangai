"""验证倒角拖动映射的几何一致性（不启动 GUI）。"""
import math, sys
sys.path.insert(0, r"F:\softai\Cangai\cangai_relief")
from core.vector.geometry_types import Point3D
from core.vector.fillet_processor import (cornerDragFrame, computeFillet,
                                          isSharpCorner)

# 直角：两条边从原点沿 +x / +y，各长 100
p0 = Point3D(0, 0, 0)
pX = Point3D(100, 0, 0)
pY = Point3D(0, 100, 0)

frame = cornerDragFrame(p0, pX, p0, pY)
assert frame is not None, "cornerDragFrame 返回 None"
inter, dir1, dir2, bisector, sinHalf, tanHalf, far1, far2 = frame
print(f"交点=({inter.x:.1f},{inter.y:.1f}) sinHalf={sinHalf:.4f} "
      f"tanHalf={tanHalf:.4f} far1={far1:.1f} far2={far2:.1f}")
# 直角：半角 45°，sinHalf=tanHalf≈0.7071
assert abs(sinHalf - math.sin(math.pi/4)) < 1e-9, "sinHalf 错误"
assert abs(tanHalf - math.tan(math.pi/4)) < 1e-9, "tanHalf 错误"
assert far1 == 100 and far2 == 100, "可修剪长度错误"

# 拖动映射：沿角平分线移动 L，半径应增加 L*sinHalf
L = 10.0
r0 = 5.0
deltaR = L * sinHalf
r1 = r0 + deltaR
print(f"沿平分线拖动 {L} -> 半径增量 {deltaR:.4f} -> 新半径 {r1:.4f}")

# 用 computeFillet 验证两个半径都能算出有效结果
fr0 = computeFillet(p0, pX, p0, pY, r0)
fr1 = computeFillet(p0, pX, p0, pY, r1)
assert fr0.valid and fr1.valid, "computeFillet 无效"
# 圆心到交点距离应等于 r/sinHalf
d0 = fr0.center.distanceTo(inter)
d1 = fr1.center.distanceTo(inter)
assert abs(d0 - r0/sinHalf) < 1e-9, f"圆心距离不匹配 {d0}"
assert abs(d1 - r1/sinHalf) < 1e-9, f"圆心距离不匹配 {d1}"
# 圆心位移应恰好等于拖动距离 L（沿角平分线）
centerShift = d1 - d0
assert abs(centerShift - L) < 1e-9, f"圆心位移 {centerShift} != {L}"
print(f"圆心位移 {centerShift:.4f} == 拖动距离 {L} -> 映射正确")

# 钳制上限：直角半径上限 = min(far)*tanHalf = 100*1 = 100
maxR = min(far1, far2) * tanHalf
assert abs(maxR - 100.0) < 1e-9, "半径上限错误"
print(f"半径上限 {maxR:.1f} 正确")

# isSharpCorner 检查
assert isSharpCorner(pX, p0, pY), "直角应判为尖角"
assert not isSharpCorner(Point3D(-100,0,0), p0, pX), "共线不应判为尖角"

# 锐角验证：两条边夹角 60°（半角 30°）
q0 = Point3D(0, 0, 0)
qA = Point3D(100, 0, 0)
qB = Point3D(100*math.cos(math.pi/3), 100*math.sin(math.pi/3), 0)
fr = cornerDragFrame(q0, qA, q0, qB)
assert fr is not None
_, _, _, _, sh, th, f1, f2 = fr
assert abs(sh - math.sin(math.pi/6)) < 1e-6, f"锐角 sinHalf 错误 {sh}"
print(f"60°角 sinHalf={sh:.4f} (期望 {math.sin(math.pi/6):.4f}) 半径上限={min(f1,f2)*th:.2f}")

print("ALL GEOMETRY TESTS PASSED")
