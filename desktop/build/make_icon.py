# -*- coding: utf-8 -*-
"""生成应用图标 ``icon.ico``（多尺寸）与预览图 ``icon.png``。

为什么不用图片库：本项目坚持**零新增依赖**，而 ``Pillow`` 只在打包机上可用。
这里用纯标准库（``struct`` / ``zlib``）直接渲染矢量形状并写出 ICO：

* 颜色取自 ``quantstudio_desktop.theme.COLORS``，与界面完全同色系；
* 圆角方块 + 上升折线 + 箭头 + 两根 K 线，形状简单，缩到 16px 依然可辨；
* 每个尺寸独立渲染（超采样抗锯齿），而不是缩放一张大图，小尺寸更锐利；
* ICO 内按经典 **32 位 BMP(DIB)** 格式写入（PNG 压缩条目在个别工具链上不兼容）。

注意：所有几何量都用**归一化坐标（0..1）**表示，而羽化/线宽阈值是**像素**概念，
因此比较前必须用 ``scale = 1/size`` 做单位换算（否则整张图会被误染，早期版本踩过）。

用法::

    python desktop/build/make_icon.py            # 写到 desktop/build/icon.ico

产物：
    desktop/build/icon.ico   多尺寸图标（16/24/32/48/64/128/256），打包用
    desktop/build/icon.png   256×256 预览图，文档与运行时窗口图标用
"""

import math
import os
import struct
import sys
import zlib
from typing import List, Sequence, Tuple

# --------------------------------------------------------------------------- 调色板（与 theme.COLORS 保持一致）
BG_TOP = (0x18, 0x22, 0x36)      # 圆角方块上部（panel 提亮）
BG_BOTTOM = (0x0E, 0x14, 0x20)   # 下部（theme bg）
EDGE = (0x33, 0x44, 0x69)        # 描边
BRAND = (0x4F, 0x8C, 0xFF)       # 主线（brand）
UP = (0xF6, 0x46, 0x5D)          # 涨（红）
DOWN = (0x2E, 0xBD, 0x85)        # 跌（绿）

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

RGB = Tuple[int, int, int]
Point = Tuple[float, float]

# --------------------------------------------------------------------------- 图形定义（归一化坐标，y 向下）
TILE_RADIUS = 0.235
LINE_POINTS: List[Point] = [(0.13, 0.79), (0.33, 0.65), (0.49, 0.71), (0.67, 0.43)]
LINE_HALF_WIDTH = 0.036
ARROW_TIP: Point = (0.865, 0.235)
ARROW_DIR: Point = (0.7071, -0.7071)
ARROW_LEN = 0.145
ARROW_HALF = 0.085

# (中心 x, 实体上沿, 实体下沿, 影线上沿, 影线下沿, 半宽, 颜色)
CANDLES = (
    (0.245, 0.560, 0.700, 0.470, 0.780, 0.042, DOWN),
    (0.405, 0.660, 0.790, 0.580, 0.860, 0.042, UP),
)
WICK_RATIO = 0.26                # 影线半径 = 实体半宽 × 该系数


# --------------------------------------------------------------------------- 几何工具
def _rounded_rect(x: float, y: float, radius: float) -> bool:
    """点是否落在铺满画布的圆角方块内。"""
    if radius <= 0:
        return True
    cx = min(max(x, radius), 1.0 - radius)
    cy = min(max(y, radius), 1.0 - radius)
    if x == cx and y == cy:
        return True
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius


def _edge_distance(x: float, y: float) -> float:
    """到圆角方块边界的距离（内部为正，越靠边越小），用于画描边。"""
    r = TILE_RADIUS
    if r < x < 1.0 - r or r < y < 1.0 - r:
        return min(x, y, 1.0 - x, 1.0 - y)
    cx = min(max(x, r), 1.0 - r)
    cy = min(max(y, r), 1.0 - r)
    return r - math.hypot(x - cx, y - cy)


def _distance_to_segment(px: float, py: float, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _distance_to_polyline(px: float, py: float, points: Sequence[Point]) -> float:
    """折线最近距离；线段端点自然形成圆角连接。"""
    best = float("inf")
    for i in range(len(points) - 1):
        d = _distance_to_segment(px, py, points[i], points[i + 1])
        if d < best:
            best = d
    return best


def _point_in_triangle(px: float, py: float, a: Point, b: Point, c: Point) -> bool:
    def sign(p1: Point, p2: Point, p3: Point) -> float:
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])

    d1, d2, d3 = sign((px, py), a, b), sign((px, py), b, c), sign((px, py), c, a)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def _stroke_coverage(distance_px: float, half_width_px: float) -> float:
    """像素距离 → 覆盖率（1px 羽化；配合超采样得到平滑边缘）。"""
    if half_width_px <= 0.0:
        return 0.0
    if distance_px <= half_width_px:
        return 1.0
    if distance_px >= half_width_px + 1.0:
        return 0.0
    return 1.0 - (distance_px - half_width_px)


def _arrow_geometry() -> Tuple[Point, Point, Point]:
    ux, uy = ARROW_DIR
    tip = ARROW_TIP
    base = (tip[0] - ux * ARROW_LEN, tip[1] - uy * ARROW_LEN)
    nx, ny = -uy, ux                     # 箭头底边的垂直方向
    left = (base[0] + nx * ARROW_HALF, base[1] + ny * ARROW_HALF)
    right = (base[0] - nx * ARROW_HALF, base[1] - ny * ARROW_HALF)
    return tip, left, right


def _blend(base: Sequence[float], rgb: RGB, alpha: float) -> List[float]:
    a = 0.0 if alpha < 0.0 else (1.0 if alpha > 1.0 else alpha)
    return [base[i] + (rgb[i] - base[i]) * a for i in range(3)]


def _shade(x: float, y: float, scale: float, size: int, arrow: Tuple[Point, Point, Point]):
    """单个采样点的颜色与不透明度（未预乘）。``scale`` = 1/边长，用于单位换算。"""
    # 1) 圆角方块底：竖直渐变 + 描边
    if not _rounded_rect(x, y, TILE_RADIUS):
        return 0.0, 0.0, 0.0, 0.0
    color = [BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * y for i in range(3)]
    edge_px = _edge_distance(x, y) / scale          # 到边界的像素距离
    stroke_px = max(1.0, size * 0.012)              # 描边宽度随尺寸略放大
    if edge_px <= 0.0:
        color = [float(c) for c in EDGE]
    elif edge_px < stroke_px:
        color = _blend(color, EDGE, 1.0 - edge_px / stroke_px)

    # 2) K 线（影线 → 实体，实体盖住影线）
    for cx, body_top, body_bottom, wick_top, wick_bottom, half_w, rgb in CANDLES:
        d_wick = _distance_to_segment(x, y, (cx, wick_top), (cx, wick_bottom))
        cov = _stroke_coverage(d_wick / scale, half_w * WICK_RATIO / scale)
        if cov > 0.0:
            color = _blend(color, rgb, cov)
        body_cov = min(half_w - abs(x - cx), y - body_top, body_bottom - y) / scale + 0.5
        if body_cov > 0.0:
            color = _blend(color, rgb, 1.0 if body_cov > 1.0 else body_cov)

    # 3) 主线：三层光晕 + 实线
    d_line = _distance_to_polyline(x, y, LINE_POINTS) / scale
    for extra, strength in ((0.055, 0.10), (0.032, 0.16), (0.014, 0.24)):
        if d_line <= (LINE_HALF_WIDTH + extra) / scale:
            color = _blend(color, BRAND, strength)
    color = _blend(color, BRAND, _stroke_coverage(d_line, LINE_HALF_WIDTH / scale))

    # 4) 箭头
    if _point_in_triangle(x, y, *arrow):
        color = _blend(color, BRAND, 1.0)
    return color[0], color[1], color[2], 1.0


def render(size: int, samples: int = 4) -> bytes:
    """渲染一个尺寸，返回 RGBA 原始字节（行优先，自上而下）。"""
    scale = 1.0 / size
    inv = 1.0 / (samples * samples)
    arrow = _arrow_geometry()
    out = bytearray()

    for py in range(size):
        for px in range(size):
            r = g = b = a = 0.0
            for sy in range(samples):
                yy = (py + (sy + 0.5) / samples) * scale
                for sx in range(samples):
                    xx = (px + (sx + 0.5) / samples) * scale
                    sr, sg, sb, sa = _shade(xx, yy, scale, size, arrow)
                    r += sr * sa
                    g += sg * sa
                    b += sb * sa
                    a += sa
            if a > 0.0:
                out += bytes((int(r / a + 0.5), int(g / a + 0.5), int(b / a + 0.5),
                              int(a * inv * 255.0 + 0.5)))
            else:
                out += b"\x00\x00\x00\x00"
    return bytes(out)


# --------------------------------------------------------------------------- 文件写出
def png_bytes(width: int, height: int, rgba: bytes) -> bytes:
    """把 RGBA 原始数据编码成 PNG（标准库实现，无损压缩）。"""
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)                                    # filter type 0
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def dib_entry(size: int, rgba: bytes) -> bytes:
    """ICO 条目数据：BITMAPINFOHEADER + 32 位 XOR 位图 + 1 位 AND 掩码。"""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    xor = bytearray()
    for y in range(size - 1, -1, -1):                    # BMP 自下而上
        base = y * size * 4
        for x in range(size):
            i = base + x * 4
            xor += bytes((rgba[i + 2], rgba[i + 1], rgba[i], rgba[i + 3]))   # BGRA
    mask_stride = ((size + 31) // 32) * 4                # 1bpp，行按 4 字节对齐
    return header + bytes(xor) + b"\x00" * (mask_stride * size)


def ico_bytes(images: Sequence[Tuple[int, bytes]]) -> bytes:
    """组装 ICO：目录 + 各尺寸的 32 位 BMP 条目。"""
    count = len(images)
    offset = 6 + 16 * count
    directory = bytearray(struct.pack("<HHH", 0, 1, count))
    payload = bytearray()
    for size, data in images:
        side = size if size < 256 else 0                 # 256 在目录里记 0
        directory += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        payload += data
        offset += len(data)
    return bytes(directory + payload)


def build_ico(sizes: Sequence[int] = ICO_SIZES):
    """渲染各尺寸，返回 ``(ico字节, 256px RGBA, 尺寸列表)``。"""
    images = []
    preview = None
    for size in sorted(sizes):
        samples = 3 if size >= 256 else 4
        rgba = render(size, samples)
        images.append((size, dib_entry(size, rgba)))
        if size == max(sizes):
            preview = (size, rgba)
    return ico_bytes(images), preview


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ico_path = os.path.join(here, "icon.ico")
    png_path = os.path.join(here, "icon.png")

    ico, preview = build_ico()
    with open(ico_path, "wb") as handle:
        handle.write(ico)
    size, rgba = preview
    with open(png_path, "wb") as handle:
        handle.write(png_bytes(size, size, rgba))

    print("已生成 %s（%d 个尺寸：%s）" % (
        ico_path, len(ICO_SIZES), ", ".join("%dpx" % s for s in sorted(ICO_SIZES))))
    print("已生成 %s（%d×%d 预览）" % (png_path, size, size))
    print("ICO 体积 %.1f KB" % (os.path.getsize(ico_path) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
