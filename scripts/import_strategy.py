#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把外部平台（掘金 gm.api）策略转换成本项目策略，并输出**如实的**诊断报告。

为什么需要「诊断」而不是直接转换
--------------------------------
掘金策略能用指数成分股、全市场市值、财务报表、分钟线、股指期货——本项目的数据层没有
这些能力。硬转出来的策略取不到数据，回测要么报错、要么算出**看起来正常但实际是假的**结果。
所以本工具先判定「能忠实翻 / 翻了但语义有损 / 根本翻不了」，再决定产出形态。

用法::

    python scripts/import_strategy.py --list                       # 看支持映射的平台能力
    python scripts/import_strategy.py strategy.py                   # 转换单个文件
    python scripts/import_strategy.py strategy.py --out dir/        # 指定输出目录
    python scripts/import_strategy.py --dir ~/Downloads --all       # 批量转换 + 出总报告
    python scripts/import_strategy.py strategy.py --dry-run          # 只看诊断，不写文件

退出码：0 = 全部可转换（fidelity / approximate）；1 = 存在无法忠实转换的文件；2 = 用法错误。
产物：``<out>/<slug>.py``（策略骨架）与 ``<out>/CONVERSION-REPORT.md``（诊断报告）。
"""

import argparse
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from quantstudio import console  # noqa: E402
from quantstudio.strategies import gm_compat as G  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="掘金（gm.api）策略 → 本项目策略 转换器（含如实的诊断报告）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", nargs="?", default="", help="要转换的 .py 文件")
    parser.add_argument("--dir", default="", help="批量转换：扫描该目录下的 *.py")
    parser.add_argument("--all", action="store_true", help="配合 --dir：处理全部文件")
    parser.add_argument("--out", default="", help="输出目录（默认 strategies/local/_imported/）")
    parser.add_argument("--slug", default="", help="指定输出文件名（不含 .py）与策略 id 后缀")
    parser.add_argument("--name", default="", help="指定策略展示名")
    parser.add_argument("--dry-run", action="store_true", help="只做诊断，不写任何文件")
    parser.add_argument("--quiet", action="store_true", help="只输出汇总")
    parser.add_argument("--list", action="store_true", help="列出 gm.api → 本项目的映射表")
    return parser.parse_args(argv)


def default_out_dir() -> str:
    """默认输出到 ``strategies/local/_imported/``：下划线开头 → `discover_local` 不会加载。

    这是刻意的：转换产物**必须先过校验、改完 TODO** 才算策略，不能一转就混进策略列表。
    """
    from quantstudio.strategies.registry import LOCAL_DIR

    return os.path.join(LOCAL_DIR, "_imported")


def print_catalog() -> int:
    print("gm.api 模块级函数/常量 → 本项目映射")
    print("=" * 78)
    for name, (level, note) in sorted(G.GM_MODULE_API.items()):
        print("  %-34s %-12s %s" % (name, level, note))
    print()
    print("context 成员")
    print("=" * 78)
    for name, (level, note) in sorted(G.GM_CONTEXT_ATTRS.items()):
        print("  context.%-26s %-12s %s" % (name, level, note))
    print()
    print("第三方依赖")
    print("=" * 78)
    for name, (level, note) in sorted(G.THIRD_PARTY.items()):
        print("  %-34s %-12s %s" % (name, level, note))
    print()
    print("级别含义：fidelity=可忠实翻译；approximate=可翻译但语义有损；blocked=缺数据能力")
    return 0


def collect_sources(args) -> list:
    if args.dir:
        pattern = os.path.join(os.path.expanduser(args.dir), "*.py")
        files = sorted(glob.glob(pattern))
        return [item for item in files if not os.path.basename(item).startswith("_")]
    if args.source:
        return [os.path.expanduser(args.source)]
    return []


def main(argv=None) -> int:
    console.force_utf8_output()
    args = parse_args(argv)
    if args.list:
        return print_catalog()

    sources = collect_sources(args)
    if not sources:
        print("请给出要转换的文件，或用 --dir 指定目录（--list 可看映射表）", file=sys.stderr)
        return 2

    out_dir = args.out or default_out_dir()
    results = []
    # 同名消歧：不同源文件可能算出同一个 slug（实测「风格轮动(股票)」与
    # 「风格轮动(股票)(算法交易)」都会变成 feng_ge_lun_dong_gu_piao）——不处理就会互相覆盖，
    # 批量转换 9 个文件只剩 8 个产物。这里给后续重名加 _2 / _3 后缀。
    seen_slugs = {}
    for path in sources:
        if not os.path.isfile(path):
            print("跳过（文件不存在）：%s" % path, file=sys.stderr)
            continue
        usage = G.analyze(path)
        slug = args.slug if len(sources) == 1 else ""
        renamed = ""
        if not slug and not args.name:
            base_slug = G._slug(usage.title, brief=28)
            count = seen_slugs.get(base_slug, 0) + 1
            seen_slugs[base_slug] = count
            if count > 1:
                slug = "%s_%d" % (base_slug[:26], count)
                renamed = ("源文件名与已转换的另一个策略重名，输出改用 `%s`（策略 id 与文件名同步）"
                           % slug)
        if args.dry_run:
            target, report, level = "", G.render_report(usage), usage.level()
        else:
            target, report, level = G.convert(
                path, out_dir, slug=slug,
                name=args.name if len(sources) == 1 else "",
            )
        if renamed:
            report = report.replace("## 平台能力对照", "> %s\n\n## 平台能力对照" % renamed, 1)
        results.append({"path": path, "usage": usage, "target": target,
                        "report": report, "level": level, "renamed": renamed})
        if not args.quiet:
            print(G.render_report(usage, target=target))
            if renamed:
                print("> %s" % renamed)
            print("-" * 78)

    # ---- 汇总报告
    counts = {G.FIDELITY: 0, G.APPROXIMATE: 0, G.BLOCKED: 0}
    for item in results:
        counts[item["level"]] += 1
    print("=" * 78)
    print("转换汇总：共 %d 个文件" % len(results))
    print("  ✅ 可忠实翻译          : %d" % counts[G.FIDELITY])
    print("  ⚠️  可翻译但语义有损    : %d" % counts[G.APPROXIMATE])
    print("  ⛔ 缺数据能力，无法忠实 : %d" % counts[G.BLOCKED])
    if not args.dry_run:
        print("  产物目录              : %s" % out_dir)

    if not args.dry_run and results:
        os.makedirs(out_dir, exist_ok=True)
        report_path = os.path.join(out_dir, "CONVERSION-REPORT.md")
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write("# 掘金策略转换报告\n\n")
            handle.write("共 %d 个文件：可忠实翻译 %d、语义有损 %d、无法忠实转换 %d。\n\n"
                         % (len(results), counts[G.FIDELITY], counts[G.APPROXIMATE],
                            counts[G.BLOCKED]))
            for item in results:
                handle.write("---\n\n")
                handle.write(item["report"])
                handle.write("\n")
        print("  诊断报告              : %s" % report_path)
        print()
        print("下一步：转换产物在 `_imported/` 下**不会被自动加载**（下划线开头）。")
        print("       改完里面的 TODO、跑 `python scripts/check_strategies.py --path <文件>` 0 error，")
        print("       再把它移到 `strategies/local/` 或安装目录的 `strategies/` 即可出现在策略列表。")

    return 1 if counts[G.BLOCKED] else 0


if __name__ == "__main__":
    sys.exit(main())
