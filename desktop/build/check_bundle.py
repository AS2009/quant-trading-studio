#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 ``.github/workflows/build-macos.yml`` 用的小工具：把 .app 的 Info.plist 关键字段打印出来。

CI 里用它做「构建产物是否真的带上了正确版本/标识/架构」的断言，避免出了包才发现 Info.plist 没写对。
用法::

    python desktop/build/check_bundle.py <path/to/QuantTradingStudio.app> [--expect-version 1.4.0]
"""

import argparse
import os
import plistlib
import subprocess
import sys


def read_info(app_path):
    plist = os.path.join(app_path, "Contents", "Info.plist")
    with open(plist, "rb") as handle:
        return plistlib.load(handle)


def arch_of(binary):
    try:
        out = subprocess.run(["lipo", "-info", binary], capture_output=True, text=True, check=False)
    except OSError:
        return "未知（lipo 不可用）"
    text = (out.stdout or "").strip()
    return text.split(":", 1)[1].strip() if ":" in text else text or "未知"


def main():
    parser = argparse.ArgumentParser(description="检查 macOS .app 的 Info.plist 与架构")
    parser.add_argument("app", help="QuantTradingStudio.app 路径")
    parser.add_argument("--expect-version", default="", help="期望的 CFBundleShortVersionString")
    parser.add_argument("--expect-id", default="com.quantstudio.desktop", help="期望的 CFBundleIdentifier")
    args = parser.parse_args()

    if not os.path.isdir(args.app):
        raise SystemExit("找不到 .app：%s" % args.app)
    info = read_info(args.app)
    executable = info.get("CFBundleExecutable") or "QuantTradingStudio"
    binary = os.path.join(args.app, "Contents", "MacOS", executable)

    print("路径          : %s" % args.app)
    print("CFBundleName  : %s" % info.get("CFBundleName"))
    print("Bundle ID     : %s" % info.get("CFBundleIdentifier"))
    print("版本          : %s（build %s）" % (info.get("CFBundleShortVersionString"),
                                              info.get("CFBundleVersion")))
    print("最小系统      : %s" % info.get("LSMinimumSystemVersion"))
    print("图标          : %s" % (info.get("CFBundleIconFile") or "（默认）"))
    print("可执行文件    : %s" % binary)
    print("架构          : %s" % arch_of(binary))

    problems = []
    if args.expect_version and info.get("CFBundleShortVersionString") != args.expect_version:
        problems.append("版本号不是 %s（Info.plist=%s）"
                        % (args.expect_version, info.get("CFBundleShortVersionString")))
    if args.expect_id and info.get("CFBundleIdentifier") != args.expect_id:
        problems.append("Bundle ID 不是 %s（Info.plist=%s）"
                        % (args.expect_id, info.get("CFBundleIdentifier")))
    if not os.path.isfile(os.path.join(args.app, "Contents", "MacOS", executable)):
        problems.append("找不到可执行文件 %s" % binary)
    if problems:
        for item in problems:
            print("[失败] %s" % item, file=sys.stderr)
        return 1
    print("检查通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
