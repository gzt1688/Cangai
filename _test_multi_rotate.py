# -*- coding: utf-8 -*-
"""多选整体旋转验证：2 圆 + 2 矩形，绕共同中心转 90°。"""
import math
import sys
sys.path.insert(0, '.')
from core.vector.line_object import LineObject
from core.vector.circle_object import CircleObject
from core.vector.geometry_types import Point3D


def rotateAround(o, angle, c):
    """复刻 BaseObject.rotateAround。"""
    o.translate(-c.x, -c.y, -c.z)
    o.rotate(angle, 0, 0, 1)
    o.translate(c.x, c.y, c.z)


def center(obj):
    bb = obj.getBoundingBox()
    return bb.center()


ok = True
objs = [
    LineObject([Point3D(10, 10, 0), Point3D(40, 10, 0), Point3D(40, 30, 0),
                Point3D(10, 30, 0), Point3D(10, 10, 0)]),
    LineObject([Point3D(60, 10, 0), Point3D(90, 10, 0), Point3D(90, 30, 0),
                Point3D(60, 30, 0), Point3D(60, 10, 0)]),
    CircleObject(20, 50, 0, 8),
    CircleObject(80, 50, 0, 8),
]
centers0 = [center(o) for o in objs]
dists0 = [[centers0[i].distanceTo(centers0[j]) for j in range(4)]
          for i in range(4)]

C = Point3D(50, 30, 0)
for o in objs:
    rotateAround(o, math.pi / 2, C)

centers1 = [center(o) for o in objs]
moved = [centers0[i].distanceTo(centers1[i]) for i in range(4)]
print("4 个对象位移:", ["%.2f" % d for d in moved])
# 每个对象都必须动（位移>0）
for i, d in enumerate(moved):
    if d < 1e-6:
        print("FAIL: 对象 %d 未移动" % i)
        ok = False

# 相对位置（两两距离）保持 = 整体旋转
dists1 = [[centers1[i].distanceTo(centers1[j]) for j in range(4)]
          for i in range(4)]
maxErr = max(abs(dists0[i][j] - dists1[i][j])
             for i in range(4) for j in range(4))
print("两两距离最大误差: %.2e (整体旋转应≈0)" % maxErr)
ok &= maxErr < 1e-6

# 圆心应绕 C 转 90°：C+(dx,dy) -> C+(-dy,dx)
for i in (2, 3):
    dx0, dy0 = centers0[i].x - C.x, centers0[i].y - C.y
    ex, ey = C.x - dy0, C.y + dx0
    err = math.hypot(centers1[i].x - ex, centers1[i].y - ey)
    print("圆 %d 圆心误差: %.2e" % (i, err))
    ok &= err < 1e-6

print("== 全部通过 ==" if ok else "== 存在失败 ==")
