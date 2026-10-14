"""规整单线字形生成器：为单线字体生成端正的数字与字母。

SGL 单线字体文件里的 ASCII 数字/字母多为手工调点，解析后笔画歪斜不顺。
本模块按几何参数化重新生成端正对称的单线字形，加载后替换。

坐标系：em=1.0，Y 向上，基线 y=0，大写顶 y=0.7，半角字宽 0.5，字形水平居中 cx=0.25。
角度约定：0°=右 90°=上 180°=左 270°=下；弧从 a0 线性插值到 a1。
"""
from __future__ import annotations
import os
import math
from typing import List, Tuple, Optional
from .font_data import GlyphData, Point

_CX = 0.25          # 字形水平中心
_W = 0.5            # 半角字宽（前进量）
_HT = 0.7           # 大写字母高度
_XHT = 0.5          # 小写 x-height
_DESC = -0.18       # 降部底
_R = 0.15           # 圆弧默认半径
_N = 28             # 圆弧段数


def _line(p1: Point, p2: Point) -> List[Point]:
    return [p1, p2]


def _arc(cx: float, cy: float, rx: float, ry: float,
         a0: float, a1: float, n: int = _N) -> List[Point]:
    pts = []
    for i in range(n + 1):
        t = a0 + (a1 - a0) * i / n
        r = math.radians(t)
        pts.append((cx + rx * math.cos(r), cy + ry * math.sin(r)))
    return pts


def _oval(cx: float, cy: float, rx: float, ry: float) -> List[Point]:
    return _arc(cx, cy, rx, ry, 0, 360)


def _top(cx, cy, rx, ry, l2r=True):
    """上半弧（经顶）。l2r=左→右，否则右→左。"""
    return _arc(cx, cy, rx, ry, 180, 0) if l2r else _arc(cx, cy, rx, ry, 0, 180)


def _bot(cx, cy, rx, ry, l2r=True):
    """下半弧（经底）。l2r=左→右，否则右→左。"""
    return _arc(cx, cy, rx, ry, 180, 360) if l2r else _arc(cx, cy, rx, ry, 0, -180)


def _right(cx, cy, rx, ry, t2b=True):
    """右半弧（经右）。t2b=上→下，否则下→上。"""
    return _arc(cx, cy, rx, ry, 90, -90) if t2b else _arc(cx, cy, rx, ry, -90, 90)


def _left(cx, cy, rx, ry, t2b=True):
    """左半弧（经左）。t2b=上→下，否则下→上。"""
    return _arc(cx, cy, rx, ry, 90, 270) if t2b else _arc(cx, cy, rx, ry, 270, 90)


def _concat(*segs: List[Point]) -> List[Point]:
    out = list(segs[0])
    for seg in segs[1:]:
        if not seg:
            continue
        if out and math.hypot(out[-1][0] - seg[0][0],
                              out[-1][1] - seg[0][1]) < 1e-6:
            out.extend(seg[1:])
        else:
            out.extend(seg)
    return out


def _glyph(ch: str, paths: List[List[Point]],
           width: float = _W) -> GlyphData:
    g = GlyphData()
    g.character = ch
    g.paths = paths
    xs = [x for p in paths for x, _ in p]
    ys = [y for p in paths for _, y in p]
    g.bbMin = (min(xs), min(ys))
    g.bbMax = (max(xs), max(ys))
    g.width = width
    g.height = 1.0
    return g


# ---------- 数字 0-9 ----------
def _digitPaths() -> dict:
    cx = _CX
    yt = _HT
    yb = 0.0
    ym = yt * 0.5          # 0.35
    rx = _R               # 0.15
    d = {}

    # 0：椭圆
    d['0'] = [_oval(cx, ym, rx, ym)]

    # 1：斜起笔 + 竖线 + 底横
    d['1'] = [
        _line((cx - rx * 0.6, yt - 0.12), (cx, yt)),
        _line((cx, yt), (cx, yb)),
        _line((cx - rx, yb), (cx + rx, yb)),
    ]

    # 2：上半弧(左→右，满高圆滑) + 斜线到左下 + 底横
    d['2'] = [
        _concat(
            _top(cx, ym, rx, yt - ym, l2r=True),
            _line((cx + rx, ym), (cx - rx, yb)),
            _line((cx - rx, yb), (cx + rx, yb)),
        )
    ]

    # 3：上弧 + 下弧，开口在左，满高（ry=yt-ym）+ 大开口(120°)避免像 0/8
    ry3 = yt - ym
    d['3'] = [
        _concat(
            _arc(cx, ym, rx, ry3, 120, 0),
            _arc(cx, ym, rx, ry3, 0, -120),
        )
    ]

    # 4：左斜线(连到右竖顶) + 横 + 右竖
    ycross = yb + 0.28
    d['4'] = [
        _line((cx - rx, ycross), (cx + rx, yt)),
        _line((cx - rx, ycross), (cx + rx, ycross)),
        _line((cx + rx, yb), (cx + rx, yt)),
    ]

    # 5：顶横 + 左竖(上半) + 下弧(满高+不对称避免像 U)
    d['5'] = [
        _concat(
            _line((cx + rx, yt), (cx - rx, yt)),
            _line((cx - rx, yt), (cx - rx, ym)),
            _arc(cx, ym, rx, ym, 180, 350),
        )
    ]

    # 6：上弧(右上→左经顶) + 左竖 + 下圆
    ry6b = ym * 0.5
    d['6'] = [
        _concat(
            _arc(cx, ym, rx, yt - ym, 30, 180),
            _line((cx - rx, ym), (cx - rx, ry6b)),
        ),
        _oval(cx, ry6b, rx, ry6b),
    ]

    # 7：顶横 + 斜线
    d['7'] = [
        _concat(
            _line((cx - rx, yt), (cx + rx, yt)),
            _line((cx + rx, yt), (cx - rx + 0.02, yb)),
        )
    ]

    # 8：上圆(小) + 下圆(大)，相切
    ry8t = ym * 0.42
    ry8b = ym * 0.58
    d['8'] = [
        _oval(cx, yt - ry8t, rx * 0.85, ry8t),
        _oval(cx, yb + ry8b, rx, ry8b),
    ]

    # 9：上圆 + 右竖(从圆右端起，避免断开) + 下弧
    ry9t = ym * 0.45
    cy9 = yt - ry9t
    ry9b = ym * 0.45
    d['9'] = [
        _oval(cx, cy9, rx, ry9t),
        _concat(
            _line((cx + rx, cy9), (cx + rx, ry9b)),
            _arc(cx, ry9b, rx, ry9b, 0, -150),
        ),
    ]

    return d


# ---------- 大写字母 A-Z ----------
def _upperPaths() -> dict:
    cx = _CX
    yt = _HT
    yb = 0.0
    ym = yt * 0.5
    rx = _R
    ry = ym
    d = {}

    # A：左斜 + 右斜 + 横
    d['A'] = [
        _line((cx - rx, yb), (cx, yt)),
        _line((cx, yt), (cx + rx, yb)),
        _line((cx - rx * 0.55, ym), (cx + rx * 0.55, ym)),
    ]

    # B：左竖 + 上凸(右半弧) + 下凸(右半弧)
    d['B'] = [
        _line((cx - rx, yb), (cx - rx, yt)),
        _concat(
            _line((cx - rx, yt), (cx, yt)),
            _right(cx, ym + 0.01, rx, ym - 0.01, t2b=True),   # 上→下经右
            _line((cx - rx, ym), (cx, ym)),
            _right(cx, yb + 0.01, rx, yb + 0.01, t2b=True),
        ),
    ]

    # C：左开口弧（右上→左→右下）
    d['C'] = [_arc(cx, ym, rx, ry, 35, 325)]

    # D：左竖 + 右半弧(上→下)
    d['D'] = [
        _line((cx - rx, yb), (cx - rx, yt)),
        _concat(
            _line((cx - rx, yt), (cx - rx, yb)),
            _right(cx - rx, ym, rx, ym, t2b=True),
        ),
    ]

    # E：左竖 + 三横
    d['E'] = [
        _line((cx - rx, yb), (cx - rx, yt)),
        _line((cx - rx, yt), (cx + rx, yt)),
        _line((cx - rx, ym), (cx + rx * 0.7, ym)),
        _line((cx - rx, yb), (cx + rx, yb)),
    ]

    # F：左竖 + 两横
    d['F'] = [
        _line((cx - rx, yb), (cx - rx, yt)),
        _line((cx - rx, yt), (cx + rx, yt)),
        _line((cx - rx, ym), (cx + rx * 0.7, ym)),
    ]

    # G：左开口弧 + 底横 + 中横
    g_arc = _arc(cx, ym, rx, ry, 35, 325)
    d['G'] = [
        g_arc,
        _line(g_arc[-1], (cx + rx * 0.3, yb)),
        _line((cx + rx * 0.3, ym), (cx + rx, ym)),
    ]

    # H：两竖 + 横
    d['H'] = [
        _line((cx - rx, yb), (cx - rx, yt)),
        _line((cx + rx, yb), (cx + rx, yt)),
        _line((cx - rx, ym), (cx + rx, ym)),
    ]

    # I：上横 + 竖 + 下横
    d['I'] = [
        _line((cx - rx, yt), (cx + rx, yt)),
        _line((cx, yt), (cx, yb)),
        _line((cx - rx, yb), (cx + rx, yb)),
    ]

    # J：上横 + 右竖弯到底
    d['J'] = [
        _line((cx - rx, yt), (cx + rx, yt)),
        _concat(
            _line((cx + rx, yt), (cx + rx, ym)),
            _right(cx, ym - 0.02, rx, ym - 0.02, t2b=True),
        ),
    ]

    # K：左竖 + 两斜
    d['K'] = [
        _line((cx - rx, yb), (cx - rx, yt)),
        _line((cx - rx, ym), (cx + rx, yt)),
        _line((cx - rx, ym), (cx + rx, yb)),
    ]

    # L：左竖 + 底横
    d['L'] = [
        _line((cx - rx, yt), (cx - rx, yb)),
        _line((cx - rx, yb), (cx + rx, yb)),
    ]

    # M：左竖 + 左斜 + 右斜 + 右竖
    d['M'] = [
        _line((cx - rx, yb), (cx - rx, yt)),
        _line((cx - rx, yt), (cx, ym)),
        _line((cx, ym), (cx + rx, yt)),
        _line((cx + rx, yt), (cx + rx, yb)),
    ]

    # N：左竖 + 斜 + 右竖
    d['N'] = [
        _line((cx - rx, yb), (cx - rx, yt)),
        _line((cx - rx, yt), (cx + rx, yb)),
        _line((cx + rx, yb), (cx + rx, yt)),
    ]

    # O：椭圆
    d['O'] = [_oval(cx, ym, rx, ry)]

    # P：左竖 + 上凸
    d['P'] = [
        _line((cx - rx, yb), (cx - rx, yt)),
        _concat(
            _line((cx - rx, yt), (cx, yt)),
            _right(cx, ym + 0.01, rx, ym - 0.01, t2b=True),
        ),
    ]

    # Q：椭圆 + 斜尾
    d['Q'] = [
        _oval(cx, ym, rx, ry),
        _line((cx, ym - 0.05), (cx + rx, yb - 0.02)),
    ]

    # R：左竖 + 上凸 + 右下斜
    d['R'] = [
        _line((cx - rx, yb), (cx - rx, yt)),
        _concat(
            _line((cx - rx, yt), (cx, yt)),
            _right(cx, ym + 0.01, rx, ym - 0.01, t2b=True),
        ),
        _line((cx, ym), (cx + rx, yb)),
    ]

    # S：上弧 + 下弧
    d['S'] = [
        _arc(cx, ym, rx, ry, 200, 0),      # 左上→右经顶
        _arc(cx, ym, rx, ry, 0, -160),     # 右→左下经底
    ]

    # T：上横 + 中竖
    d['T'] = [
        _line((cx - rx, yt), (cx + rx, yt)),
        _line((cx, yt), (cx, yb)),
    ]

    # U：左竖 + 底弧 + 右竖
    d['U'] = [
        _concat(
            _line((cx - rx, yt), (cx - rx, ym)),
            _bot(cx, ym, rx, ry, l2r=True),
            _line((cx + rx, ym), (cx + rx, yt)),
        )
    ]

    # V：左斜 + 右斜
    d['V'] = [
        _line((cx - rx, yt), (cx, yb)),
        _line((cx, yb), (cx + rx, yt)),
    ]

    # W：四斜
    d['W'] = [
        _line((cx - rx, yt), (cx - rx * 0.5, yb)),
        _line((cx - rx * 0.5, yb), (cx, ym)),
        _line((cx, ym), (cx + rx * 0.5, yb)),
        _line((cx + rx * 0.5, yb), (cx + rx, yt)),
    ]

    # X：两斜
    d['X'] = [
        _line((cx - rx, yb), (cx + rx, yt)),
        _line((cx - rx, yt), (cx + rx, yb)),
    ]

    # Y：两斜 + 中竖
    d['Y'] = [
        _line((cx - rx, yt), (cx, ym)),
        _line((cx, ym), (cx + rx, yt)),
        _line((cx, ym), (cx, yb)),
    ]

    # Z：上横 + 斜 + 下横
    d['Z'] = [
        _concat(
            _line((cx - rx, yt), (cx + rx, yt)),
            _line((cx + rx, yt), (cx - rx, yb)),
            _line((cx - rx, yb), (cx + rx, yb)),
        )
    ]

    return d


# ---------- 小写字母 a-z ----------
def _lowerPaths() -> dict:
    cx = _CX
    xt = _XHT       # 小写顶 0.5
    yb = 0.0
    xm = xt * 0.5   # 0.25
    rx = _R * 0.85
    ry = xm
    d = {}

    # a：右半圆 + 底圆
    d['a'] = [
        _concat(
            _line((cx + rx, xt), (cx + rx, xm)),
            _oval(cx, xm, rx, ry),
        )
    ]

    # b：左竖(全高) + 右凸
    d['b'] = [
        _line((cx - rx, yb), (cx - rx, _HT)),
        _concat(
            _line((cx - rx, xt), (cx + rx, xt)),
            _right(cx, xm, rx, ry, t2b=True),
        ),
    ]

    # c：左开口弧(小)
    d['c'] = [_arc(cx, xm, rx, ry, 35, 325)]

    # d：右竖(全高) + 左凸
    d['d'] = [
        _line((cx + rx, yb), (cx + rx, _HT)),
        _concat(
            _line((cx + rx, xt), (cx - rx, xt)),
            _left(cx, xm, rx, ry, t2b=True),
        ),
    ]

    # e：中横 + 左开口弧
    d['e'] = [
        _arc(cx, xm, rx, ry, 35, 325),
        _line((cx - rx, xm), (cx + rx * 0.6, xm)),
    ]

    # f：顶横 + 竖
    d['f'] = [
        _line((cx - rx, xt), (cx + rx * 0.5, xt)),
        _line((cx, _HT), (cx, yb)),
    ]

    # g：圆 + 降部弯
    d['g'] = [
        _oval(cx, xm, rx, ry),
        _line((cx + rx, xm), (cx + rx, _DESC)),
        _line((cx + rx, _DESC), (cx, _DESC)),
    ]

    # h：左竖(全高) + 右上半弧 + 右竖
    d['h'] = [
        _line((cx - rx, yb), (cx - rx, _HT)),
        _concat(
            _line((cx - rx, xt), (cx + rx, xt)),
            _right(cx, xm, rx, ry, t2b=True),
            _line((cx + rx, xm), (cx + rx, yb)),
        ),
    ]

    # i：竖 + 点
    d['i'] = [
        _line((cx, yb), (cx, xt)),
        _line((cx, xt + 0.08), (cx, xt + 0.08)),
    ]

    # j：竖弯 + 点
    d['j'] = [
        _concat(
            _line((cx + rx * 0.5, xt), (cx + rx * 0.5, yb)),
            _right(cx, yb - 0.02, rx * 0.5, yb - 0.02, t2b=True),
        ),
        _line((cx + rx * 0.5, xt + 0.08), (cx + rx * 0.5, xt + 0.08)),
    ]

    # k：左竖 + 两斜
    d['k'] = [
        _line((cx - rx, yb), (cx - rx, _HT)),
        _line((cx - rx, xm), (cx + rx, xt)),
        _line((cx - rx, xm), (cx + rx, yb)),
    ]

    # l：竖
    d['l'] = [_line((cx, yb), (cx, _HT))]

    # m：左竖 + 两凸
    d['m'] = [
        _line((cx - rx, yb), (cx - rx, xt)),
        _concat(
            _line((cx - rx, xt), (cx - rx * 0.5, xt)),
            _right(cx - rx * 0.5, xm, rx * 0.5, xm, t2b=True),
        ),
        _concat(
            _line((cx, xt), (cx + rx * 0.5, xt)),
            _right(cx + rx * 0.5, xm, rx * 0.5, xm, t2b=True),
            _line((cx + rx, xm), (cx + rx, yb)),
        ),
    ]

    # n：左竖 + 右上半弧 + 右竖
    d['n'] = [
        _line((cx - rx, yb), (cx - rx, xt)),
        _concat(
            _line((cx - rx, xt), (cx + rx, xt)),
            _right(cx, xm, rx, ry, t2b=True),
            _line((cx + rx, xm), (cx + rx, yb)),
        ),
    ]

    # o：椭圆(小)
    d['o'] = [_oval(cx, xm, rx, ry)]

    # p：左竖(降部) + 上凸
    d['p'] = [
        _line((cx - rx, _DESC), (cx - rx, xt)),
        _concat(
            _line((cx - rx, xt), (cx + rx, xt)),
            _right(cx, xm, rx, ry, t2b=True),
        ),
    ]

    # q：右竖(降部) + 左凸
    d['q'] = [
        _line((cx + rx, _DESC), (cx + rx, xt)),
        _concat(
            _line((cx + rx, xt), (cx - rx, xt)),
            _left(cx, xm, rx, ry, t2b=True),
        ),
    ]

    # r：左竖 + 小凸
    d['r'] = [
        _line((cx - rx, yb), (cx - rx, xt)),
        _arc(cx - rx, xt - 0.02, rx * 0.6, xt - 0.02, -90, 30),
    ]

    # s：两弧(小)
    d['s'] = [
        _arc(cx, xm, rx, ry, 200, 0),
        _arc(cx, xm, rx, ry, 0, -160),
    ]

    # t：横 + 竖弯
    d['t'] = [
        _line((cx - rx, xt * 0.7), (cx + rx * 0.5, xt * 0.7)),
        _concat(
            _line((cx, _HT), (cx, yb + 0.05)),
            _right(cx - rx * 0.3, yb + 0.03, rx * 0.3, yb + 0.03, t2b=True),
        ),
    ]

    # u：左竖 + 底弧 + 右竖
    d['u'] = [
        _concat(
            _line((cx - rx, xt), (cx - rx, xm)),
            _bot(cx, xm, rx, ry, l2r=True),
            _line((cx + rx, xm), (cx + rx, xt)),
        )
    ]

    # v：两斜
    d['v'] = [
        _line((cx - rx, xt), (cx, yb)),
        _line((cx, yb), (cx + rx, xt)),
    ]

    # w：四斜
    d['w'] = [
        _line((cx - rx, xt), (cx - rx * 0.5, yb)),
        _line((cx - rx * 0.5, yb), (cx, xm)),
        _line((cx, xm), (cx + rx * 0.5, yb)),
        _line((cx + rx * 0.5, yb), (cx + rx, xt)),
    ]

    # x：两斜
    d['x'] = [
        _line((cx - rx, yb), (cx + rx, xt)),
        _line((cx - rx, xt), (cx + rx, yb)),
    ]

    # y：两斜 + 降部
    d['y'] = [
        _line((cx - rx, xt), (cx, yb)),
        _line((cx, yb), (cx + rx, xt)),
        _line((cx, yb), (cx, _DESC)),
    ]

    # z：上横 + 斜 + 下横
    d['z'] = [
        _concat(
            _line((cx - rx, xt), (cx + rx, xt)),
            _line((cx + rx, xt), (cx - rx, yb)),
            _line((cx - rx, yb), (cx + rx, yb)),
        )
    ]

    return d


def buildGlyphs() -> dict:
    """生成全部数字与字母的规整字形 {char: GlyphData}。"""
    out = {}
    for ch, paths in _digitPaths().items():
        out[ch] = _glyph(ch, paths)
    for ch, paths in _upperPaths().items():
        out[ch] = _glyph(ch, paths)
    for ch, paths in _lowerPaths().items():
        out[ch] = _glyph(ch, paths)
    return out


def _resourcesFontDir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(3):
        here = os.path.dirname(here)
    return os.path.join(here, "resources", "fonts")


def _adaptTTFToSGL(g: GlyphData, ch: str) -> Optional[GlyphData]:
    """把 TTF 字形（em=1.0, Y向上, 基线 y=0）变换到 SGL 半角坐标系。

    SGL: width=0.5, 大写/数字顶 y=0.7, 小写顶 y=0.5, 基线 y=0。
    等比缩放（以高度为主，宽度不超出 0.5），水平居中，基线对齐。
    """
    if g.bbMin is None or g.bbMax is None:
        return None
    tw = g.bbMax[0] - g.bbMin[0]
    top = g.bbMax[1]
    if tw <= 0 or top <= 0:
        return None

    targetH = _HT if (ch.isupper() or ch.isdigit()) else _XHT
    scaleH = targetH / top
    scaleW = _W / tw
    scale = min(scaleH, scaleW)

    offsetX = (_W - (g.bbMin[0] + g.bbMax[0]) * scale) * 0.5

    newPaths = []
    for path in g.paths:
        newPaths.append([(x * scale + offsetX, y * scale) for x, y in path])

    g2 = GlyphData()
    g2.character = ch
    g2.paths = newPaths
    g2.width = _W
    g2.height = 1.0
    g2.bbMin = (g.bbMin[0] * scale + offsetX, g.bbMin[1] * scale)
    g2.bbMax = (g.bbMax[0] * scale + offsetX, g.bbMax[1] * scale)
    return g2


_ttfGlyphsCache: Optional[dict] = None


def _loadTTFGlyphs() -> Optional[dict]:
    """从"标准数字字母.ttf"提取数字字母字形，变换到 SGL 坐标系。"""
    global _ttfGlyphsCache
    if _ttfGlyphsCache is not None:
        return _ttfGlyphsCache
    ttfPath = os.path.join(_resourcesFontDir(), "标准数字字母.ttf")
    if not os.path.isfile(ttfPath):
        return None
    try:
        from .ttf_reader import loadFromFile as loadTTF
        ttf = loadTTF(ttfPath)
    except Exception:
        return None
    if not ttf.isLoaded:
        return None
    out = {}
    for ch in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz":
        g = ttf.getGlyph(ch)
        if g is None or not g.paths:
            continue
        g2 = _adaptTTFToSGL(g, ch)
        if g2 is not None:
            out[ch] = g2
    if out:
        _ttfGlyphsCache = out
    return out if out else None


def override(fd) -> None:
    """用"标准数字字母.ttf"的数字字母字形替换字体中的对应字符（原地修改）。

    TTF 加载失败或缺少字符时，回退到 buildGlyphs() 程序生成。
    """
    ttfGlyphs = _loadTTFGlyphs() or {}
    fallback = buildGlyphs()
    for ch, g in fallback.items():
        fd.glyphs[ch] = ttfGlyphs.get(ch, g)
