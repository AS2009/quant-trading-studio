# -*- coding: utf-8 -*-
"""QuantTrading Studio —— 面向真实市场的量化研究 / 模拟交易工具包。

包结构（分层，依赖单向：api -> services -> {data, backtest, strategies, trading, portfolio} -> core）
---------------------------------------------------------------------------
- core/       领域模型、抽象接口、交易日历、异常（无外部依赖）
- data/       行情数据层：可插拔 Provider（新浪实时 / 东方财富K线 / CSV自有 / 示例兜底）+ 磁盘缓存
- strategies/ 策略库：真实可运行策略实现 + 注册表（新增策略只需加一个文件）
- backtest/   回测引擎：撮合（T+1/涨跌停/费用/滑点）、账户、绩效指标
- trading/    交易执行：模拟盘 Broker（默认安全）+ 真实券商适配接口（预留）
- portfolio/  自有持仓账户（真实持仓文件 / 模拟盘成交记录）
- services/   业务服务层：把上述能力聚合成前端所需的数据结构（含缓存与降级）
- api/        HTTP 接口层（Flask 蓝图），统一响应信封 {data, as_of, meta}

设计原则
--------
1. **真实优先、离线可用**：优先拉取真实行情；网络异常时依次降级为「磁盘缓存」→「CSV 自有数据」→「示例数据」，
   并在响应 meta.source 中明确标注数据来源，绝不把演示数据伪装成真实行情。
2. **零重依赖**：核心逻辑只用标准库；pandas/numpy 为可选加速（`quantstudio.compat.HAS_PANDAS`），缺失自动回退。
3. **只读行情 + 模拟盘**：默认不会向任何券商发送真实委托；真实下单需显式接入 trading/adapters.py 中的适配器。
"""

__version__ = "2.0.0"

__all__ = ["__version__"]
