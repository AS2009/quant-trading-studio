#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略规范校验入口（等价于 python -m quantstudio.strategies.lint）。

用法：
    python scripts/check_strategies.py                      # 校验内置 + local/ 全部策略
    python scripts/check_strategies.py --path backend/quantstudio/strategies/local/x.py
    python scripts/check_strategies.py --json               # CI 用机器可读输出
    python scripts/check_strategies.py --no-smoke           # 跳过烟雾回测（更快）

退出码 0 = 全部通过（warning 不影响），1 = 存在 error。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from quantstudio.strategies.lint import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
