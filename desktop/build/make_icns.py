# -*- coding: utf-8 -*-
"""生成 macOS 应用图标 ``icon.icns``（纯标准库渲染 + 系统 ``iconutil`` 打包）。

背景
----
Windows 用 ``icon.ico``（``make_icon.py`` 生成，纯标准库直接写 ICO），
macOS 要的是 ``.icns``：它由一组 PNG（``.iconset`` 目录 + ``iconutil -c icns``）组成。
本脚本复用 ``make_icon.render()/png_bytes()`` 渲染像素，**不引入 Pillow 等任何依赖**；
唯一的系统命令是 macOS 自带的 ``iconutil``（Xcode / 系统提供）。

用法::

    python desktop/build/make_icns.py                # 生成 desktop/build/icon.icns
    python desktop/build/make_icns.py --samples 4    # 更平滑（更慢）
    python desktop/build/make_icns.py --keep         # 保留中间 .iconset 目录

说明
----
* Apple 要求的 10 个条目 = 5 种逻辑尺寸 × (1x, 2x)：16/32/128/256/512 与它们的 2x
  （32/64/256/512/1024），逐个尺寸原生渲染，**不做放大**，避免糊边；
* 1024px 在纯 Python 里逐像素渲染较慢，所以按尺寸给不同的超采样：小图多采样、
  大图少采样（视觉差异可忽略，总耗时从分钟级降到秒级）；
* 非 macOS 上直接报错退出（Windows 产物用 ``icon.ico``，不需要 ``.icns``）。
"""

import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from make_icon import png_bytes, render          # noqa: E402  （同目录工具脚本）

#: ``.iconset`` 里 Apple 要求的条目 → 像素尺寸（同一个 PNG 会被 @1x/@2x 复用）
ICONSET = (
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
)

#: 每个像素尺寸的渲染超采样（越大越平滑、越慢）：大图降采样以控制纯 Python 耗时
SAMPLES_BY_SIZE = {16: 4, 32: 4, 64: 3, 128: 3, 256: 2, 512: 2, 1024: 1}


def _check_tools() -> None:
    if sys.platform != "darwin":
        raise SystemExit("本脚本只能在 macOS 上生成 .icns（Windows 用 icon.ico；"
                         "Linux 无 .icns 需求）。当前平台：%s" % sys.platform)
    if shutil.which("iconutil") is None:
        raise SystemExit("找不到系统命令 iconutil（macOS 自带，属 Xcode 命令行工具）。"
                         "请先执行：xcode-select --install")


def _render_pngs(iconset_dir: str, samples_override, log) -> None:
    """按尺寸渲染 PNG 写进 iconset 目录（同一尺寸只渲染一次）。"""
    if os.path.isdir(iconset_dir):
        shutil.rmtree(iconset_dir)
    os.makedirs(iconset_dir)

    rendered = {}
    for name, size in ICONSET:
        if size not in rendered:
            samples = samples_override or SAMPLES_BY_SIZE.get(size, 2)
            started = time.time()
            rendered[size] = png_bytes(size, size, render(size, samples=samples))
            log("  渲染 %4dpx（超采样 %d）：%.1fs，%.1f KB"
                % (size, samples, time.time() - started, len(rendered[size]) / 1024.0))
        with open(os.path.join(iconset_dir, name), "wb") as handle:
            handle.write(rendered[size])


def build_icns(out_path: str, samples_override=None, keep: bool = False, log=print) -> str:
    """生成 ``.icns``；``keep=True`` 时保留 ``.iconset`` 目录。"""
    _check_tools()
    iconset_dir = os.path.join(os.path.dirname(out_path) or ".", "icon.iconset")
    log("1) 渲染图标位图 → %s" % iconset_dir)
    _render_pngs(iconset_dir, samples_override, log)

    log("2) iconutil 打包 → %s" % out_path)
    subprocess.run(["iconutil", "-c", "icns", iconset_dir, "-o", out_path], check=True)
    if not keep:
        shutil.rmtree(iconset_dir, ignore_errors=True)
    log("已完成：%s（%.1f KB）" % (out_path, os.path.getsize(out_path) / 1024.0))
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 macOS 应用图标 icon.icns")
    parser.add_argument("--out", default=os.path.join(HERE, "icon.icns"),
                        help="输出的 .icns 路径（默认 desktop/build/icon.icns）")
    parser.add_argument("--samples", type=int, default=0,
                        help="强制所有尺寸使用该超采样（默认按尺寸 1~4，兼顾质量与耗时）")
    parser.add_argument("--keep", action="store_true", help="保留中间 .iconset 目录")
    args = parser.parse_args()
    build_icns(args.out, samples_override=args.samples or None, keep=args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
