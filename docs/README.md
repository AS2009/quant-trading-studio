# 文档索引

| 文档 | 读者 | 内容 |
|---|---|---|
| [strategy-spec.md](strategy-spec.md) | 所有写策略的人 | **策略编写规范**：命名、目录结构、文件格式、必守规则、校验要求、上线检查清单 |
| [strategy-api.md](strategy-api.md) | 写策略的人 | **可用 API 参考**：`ctx` 上下文、`BaseStrategy` 助手、参数 schema 字段、返回值语义 |
| [strategy-examples.md](strategy-examples.md) | 写策略的人 | **完整示例**：单标的趋势、多标的轮动、可复制的骨架 + 常见错误对照 |
| [ai-strategy-guide.md](ai-strategy-guide.md) | AI 助手 / 用 AI 写策略的人 | **让 AI 一次写对**：必读文件、生成流程、提示词模板、自检循环 |
| [level2.md](level2.md) | 想看盘口 / L2 的人 | **盘口 / L2**：能力边界（免费源能给什么、十档为何要付费）、口径（盘口单位、逐笔方向、资金流分档）、HTTP 与 MCP 用法、接入付费 L2 的两条路 |
| [../backend/data/README.md](../backend/data/README.md) | 使用者 | 运行时数据目录、CSV 导入格式、持仓文件格式 |
| [desktop-gui.md](desktop-gui.md) | 桌面版使用者 / 打包维护者 | **Windows 原生 GUI**：安装运行、界面与快捷键、六个页面、数据目录、命令行参数、打包与 GitHub Actions 自动编译、排障 |
| [../README.md](../README.md) | 使用者 | 安装、启动、数据源、回测口径、API 一览 |
| [mcp.md](mcp.md) | 想让大模型操作项目的人 | **MCP 服务器**：客户端配置（Claude Desktop / Claude Code / Cursor / 桌面版 exe）、工具清单、典型工作流、安全护栏与排障 |

> 机器可执行的入口是 **`python scripts/check_strategies.py`**（等价于 `python -m quantstudio.strategies.lint`）：
> 规范里写的每一条硬性要求，都有对应的校验规则码；文档中的规则码可在 `backend/quantstudio/strategies/lint.py` 中检索到实现。
