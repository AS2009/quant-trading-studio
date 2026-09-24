# -*- coding: utf-8 -*-
"""MCP 服务器入口（stdio）：``python scripts/mcp_server.py``。

与其它 ``scripts/*.py`` 一样自己处理 ``sys.path``，因此 MCP 客户端里可以直接写相对路径：

    {"command": "python", "args": ["scripts/mcp_server.py"]}          # 可读写
    {"command": "python", "args": ["scripts/mcp_server.py", "--read-only"]}

常用参数见 ``--help``；``--selftest`` 会跑一遍握手 → 工具清单 → 调一个只读工具，
并把报告写到 ``%TEMP%/quantstudio_mcp_selftest.txt``（CI 与打包产物验证用）。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "backend") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "backend"))

from quantstudio import console  # noqa: E402
from quantstudio.mcp.cli import main  # noqa: E402

if __name__ == "__main__":
    # 协议消息里会出现中文（策略名、行情来源说明），必须先固定 UTF-8：
    # Windows 控制台/管道默认代码页不是 UTF-8 时，写中文会抛 UnicodeEncodeError。
    console.force_utf8_output()
    sys.exit(main())
