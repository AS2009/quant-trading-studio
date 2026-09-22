# -*- coding: utf-8 -*-
"""本地/第三方策略目录（drop-in）。

把符合《策略编写规范》的 ``<slug>.py`` 放进本目录即可被自动发现并出现在策略列表中，
**无需修改 registry.py**：

- 文件名 = 策略 id 去掉 ``st_`` 前缀（``breakout_atr.py`` ↔ ``st_breakout_atr``）；
- 类名 = 文件名转 PascalCase + ``Strategy``（``BreakoutAtrStrategy``）；
- 以 ``_`` 开头的文件不会被加载（``_template.py`` 是模板）；
- 单个文件加载失败只会在 ``strategies.local_status()`` 里记录原因并跳过，绝不会导致整个应用起不来。

规范与示例见仓库 ``docs/strategy-spec.md``、``docs/strategy-examples.md``、
``docs/ai-strategy-guide.md``；校验命令::

    python -m quantstudio.strategies.lint            # 校验全部策略
    python -m quantstudio.strategies.lint --path local/breakout_atr.py
"""

__all__ = []
