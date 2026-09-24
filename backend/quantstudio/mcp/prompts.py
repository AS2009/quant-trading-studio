# -*- coding: utf-8 -*-
"""MCP 提示词模板：把「怎么用本服务器的工具干活」写成可直接调用的中文 prompts。

客户端（Claude Desktop / Claude Code / Cursor 等）会把 ``prompts/list`` 里的模板展示给用户，
用户在界面上填参数后，``prompts/get`` 返回的消息文本直接进入模型上下文——所以每个模板都
必须把**工作流、工具名、硬性约束、交付格式**讲清楚，模型照着做就能少走弯路。

约定：

* 参数缺失/类型不对时用中性默认值（模板永远可用，不报错）；多余参数直接忽略；
* 模板里只出现本服务器真实存在的工具与资源 uri（改工具名时同步改这里）。
"""

from typing import Any, Dict, List, Optional

#: 资源 uri（与 ``resources.py`` 的白名单一致）
SPEC_URI = "quantstudio://docs/strategy-spec.md"
EXAMPLES_URI = "quantstudio://docs/strategy-examples.md"
API_URI = "quantstudio://docs/strategy-api.md"

_MISSING_IDEA = ("用户没有给出具体想法。请先用 strategy_list 列出可用策略，" 
                 "再给出 2-3 个可行的策略方向让用户选择，确认后再动手写代码。")
_MISSING_UNIVERSE = ("未指定——默认使用自选池；若自选池为空，选一个代表性宽基（如沪深300）"
                     "并在报告中说明选择依据。")
_MISSING_STRATEGY = ("未指定——请先调用 strategy_list 查看可用策略；若用户仍未指明，"
                     "先向用户确认要复盘哪一个，不要随机挑一个就回测。")
_MISSING_SYMBOL = ("未指定——选择该策略默认或最有代表性的标的，并在报告中说明为什么选它。")
_MISSING_SYMBOLS = ("未指定——使用自选池（market_quotes 的默认标的）；"
                    "若自选池为空，用沪深300 的代表性成分股并在简报里说明。")


def _text(value: Any, default: str) -> str:
    """把模板参数转成文本：缺失/空/非字符串都退回中性默认值。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip() or default
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return "、".join(parts) if parts else default
    if isinstance(value, dict):
        return ", ".join("%s=%s" % (key, value[key]) for key in sorted(value)) or default
    return str(value)


def _arg(name: str, description: str, required: bool) -> Dict[str, Any]:
    return {"name": name, "description": description, "required": required}


# --------------------------------------------------------------------------- 提示词正文
WRITE_STRATEGY = """你是一名 A 股量化策略工程师。请把下面的想法写成符合本项目规范、能通过校验并跑出回测的**代码策略**。

【策略想法】
{idea}

【标的池 / 研究范围】
{universe}

【工作流（按顺序执行，不要跳步）】
1. **读参考实现**：调用 `strategy_read_source` 读一个结构最接近的内置策略源码；不确定读哪个就先 `strategy_list` / `strategy_detail`。
2. **对照规范**：读资源 `{spec_uri}`（硬性规则）与 `{examples_uri}`（可复制骨架）；需要查 API 时读 `{api_uri}`。
3. **写代码**：调用 `strategy_write_source` 落盘。写工具会自动运行策略校验器并返回问题清单。
4. **迭代到通过**：按返回的问题逐条修改后重写；必要时调用 `strategy_lint` 复查，直到没有任何校验问题。
5. **回测验证**：调用 `backtest_run`，用真实历史数据跑足够长的区间（建议至少 3 年，覆盖上涨/震荡/下跌），并与基准（默认沪深300）对比。
6. **汇报交付**：给出策略逻辑（信号、仓位、止损止盈）、参数与取值、回测指标（累计/年化收益、最大回撤、夏普、胜率、换手率与费用占比）、适用行情与失效场景，并声明「历史回测不代表未来收益」。

【硬性约束，违反会被校验器直接判错】
- 策略代码**只允许** import 规范里列出的模块。网络（requests/urllib/socket）、文件（open/os/shutil）、时间（datetime/time）、随机（random）等 import 与调用一律被禁用，**不要试图绕过**。
- 策略参数**必须定义完整 schema**：名称、类型、默认值、取值范围或枚举、说明；界面与模型都要能安全调参。
- 数据只来自本服务器工具返回的结果，**不要臆造**行情、回测数字或标的代码。
- 只改 `strategies/local/` 下的代码策略；不要动内置策略与仓库其它文件。"""

REVIEW_BACKTEST = """请对策略 **{strategy_id}** 的回测做一次严格、可复现的复盘（标的：{symbol}）。

【步骤】
1. 确认对象：`strategy_detail` 查看该策略的参数与说明（必要时先 `strategy_list`）。
2. 跑回测：调用 `backtest_run`，**带上 include_series=true** 拿净值序列与回撤序列；区间默认至少 3 年，且必须包含至少一段熊市或大幅震荡区间。
3. 解读数据（每条都要给具体数字，不要只给形容词）：
   - 收益：累计收益、年化收益，与基准（默认沪深300）对比是否跑赢；
   - 风险：最大回撤、回撤修复时间、波动率；
   - 风险调整：夏普、卡玛比率；
   - 交易质量：胜率、盈亏比、换手率，以及手续费+滑点吃掉了多少收益；
   - 集中度：单票/单行业最大贡献，收益是否靠极少数交易支撑。
4. 过拟合检查：换一组邻近参数（例如均线周期 ±20%）复跑对比敏感性；换一个时间区间或标的池看是否稳定；交易次数过少要明确指出结论不可靠。
5. 结论：从「值得继续研究 / 需要改进 / 不建议继续」中明确三选一，并给出 1-3 条可执行的改进方向。

【要求】
- 如实说明回测口径与费率假设；**必须同时报告最差区间与最大回撤**，不要只挑好看的数字。
- 如果 `backtest_run` 因数据不足或参数非法失败，先把原因和最小修复方案报告给用户，不要反复盲试。"""

DAILY_WATCH = """请生成一份 A 股市场简报（关注标的：{symbols}）。

【数据采集（全部走本服务器工具，不要凭记忆编数字）】
1. 大盘：调用 `market_overview` 获取指数、市场广度（涨跌家数/资金）与板块表现。
2. 快照：调用 `market_quotes` 获取关注标的与自选池的实时行情。
3. 逐票：对每只关注标的调用 `market_kline` 取**近 60 根**日线（必要时切换周期），判断趋势方向、量能变化与关键价位。

【输出格式：三段式】
一、市场情绪：指数涨跌与量能、涨跌家数与资金流向、领涨/领跌板块，最后用一句话定调（偏强 / 中性 / 偏弱）。
二、个股异动：逐票列出涨跌幅、量比或换手变化、是否触及关键均线/前高前低；能解释的原因才写原因，数据不足就写「原因不明」，不要编故事。
三、关注点：下一交易日值得盯的价格、量能与事件，每条都要带触发条件（例如「放量站上 XX 元」）。

【必须声明】
- 写明数据来源（`market_*` 工具返回值里给出的来源说明）与数据时间；
- 结尾固定写上：**以上内容仅供研究参考，不构成投资建议。**"""


class BuiltinPrompts:
    """内置提示词模板（鸭子类型接口见 ``mcp/server.py``）。"""

    def __init__(self, context: Any = None) -> None:
        self.context = context

    # ------------------------------------------------------------------ 清单
    def list(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "write_strategy",
                "title": "写一个策略（从想法到回测验证）",
                "description": "把一个策略想法写成规范的代码策略：读参考实现 → 按规范写 → 校验迭代 → 回测验证。",
                "arguments": [
                    _arg("idea", "策略想法：想解决什么问题、用什么信号、偏好周期。", True),
                    _arg("universe", "标的池，如 ['600519.SH', '000001.SZ'] 或“沪深300”；缺省用自选池。", False),
                ],
            },
            {
                "name": "review_backtest",
                "title": "复盘一次回测（收益 / 风险 / 过拟合）",
                "description": "对指定策略跑回测并做严格复盘：指标解读、过拟合检查、是否值得继续研究的结论。",
                "arguments": [
                    _arg("strategy_id", "策略 id，如 dual_ma、st_breakout_atr。", True),
                    _arg("symbol", "标的代码，如 600519.SH；缺省由模型选代表性标的。", False),
                ],
            },
            {
                "name": "daily_watch",
                "title": "盘中 / 盘后看盘简报",
                "description": "汇总指数与板块、自选与关注标的行情，输出「市场情绪 → 个股异动 → 关注点」三段式简报。",
                "arguments": [
                    _arg("symbols", "关注的标的代码列表，如 ['600519.SH', '300750.SZ']；缺省用自选池。", False),
                ],
            },
        ]

    # ------------------------------------------------------------------ 取用
    def get(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """按名称渲染提示词；未知名称返回 ``None``，参数缺失用中性默认值（不报错）。"""
        args = arguments if isinstance(arguments, dict) else {}
        if name == "write_strategy":
            text = WRITE_STRATEGY.format(
                idea=_text(args.get("idea"), _MISSING_IDEA),
                universe=_text(args.get("universe"), _MISSING_UNIVERSE),
                spec_uri=SPEC_URI,
                examples_uri=EXAMPLES_URI,
                api_uri=API_URI,
            )
            description = "把策略想法写成符合规范的代码策略，并用回测验证。"
        elif name == "review_backtest":
            text = REVIEW_BACKTEST.format(
                strategy_id=_text(args.get("strategy_id"), _MISSING_STRATEGY),
                symbol=_text(args.get("symbol"), _MISSING_SYMBOL),
            )
            description = "复盘策略回测：收益、风险、交易质量、过拟合与结论。"
        elif name == "daily_watch":
            text = DAILY_WATCH.format(symbols=_text(args.get("symbols"), _MISSING_SYMBOLS))
            description = "盘中/盘后看盘简报：市场情绪、个股异动与关注点。"
        else:
            return None
        return {
            "description": description,
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }


__all__ = ["BuiltinPrompts", "WRITE_STRATEGY", "REVIEW_BACKTEST", "DAILY_WATCH"]
