# -*- coding: utf-8 -*-
"""真实券商适配层（**占位实现**）：接入位置、步骤与风控要求。

本项目默认**只做模拟盘**（``quantstudio.trading.paper.PaperBroker``）。本模块提供的
``LiveBrokerStub`` 是真实通道的占位：``is_live = True``，但任何提交都抛
`BrokerUnavailable`，绝不会向券商发出真实委托。真实下单风险自负。

一、接入位置（只改本文件与 trading/__init__.py 的导出，不要改既有通道）
--------------------------------------------------------------------
1. 在本文件新增子类，继承 ``BaseBroker`` 并实现：

   - ``_do_submit(order) -> Order``：把 ``Order`` 翻译成券商 API 调用；
     下单被拒时抛 ``OrderRejected``（基类会转成 ``status="rejected"`` 并保留 reason）；
     通道不可用时抛 ``BrokerUnavailable``。
   - ``positions() / account()``：以券商回报为准构造 ``Position`` / ``Account``。
   - ``cancel(order_id)``：若券商撤单是异步回调，可在收到回报后调用
     ``BaseBroker._transition(order, "cancelled")``；``BaseBroker`` 已实现本地撤单状态机。
   - 构造时把 ``name`` 设为券商名、``is_live = True``、``available`` 按登录状态动态判断。

2. 把新子类注册进 ``available_brokers()`` 的返回值，并在 ``trading/__init__.py`` 导出。

3. 必须显式设置环境变量 ``QUANTSTUDIO_ENABLE_LIVE_TRADING=1`` 才允许实例化真实通道
   （见 ``LIVE_TRADING_ENV``），且**必须自行实现 _do_submit**——占位实现永不成交。
   建议在 ``__init__`` 里直接检查该开关，未开启时抛 ``BrokerUnavailable``。

4. 常见券商接入点：
   - QMT / miniQMT（迅投 xtquant）：Windows + 客户端登录；用 ``xtquant.xttrader.XtQuantTrader``
     下单，``order_callback`` / ``trade_callback`` 回报委托与成交，再把状态同步回 BaseBroker。
   - 富途 OpenAPI：需要 ``futu`` SDK + OpenD 网关常驻 + 交易解锁；下单走
     ``OpenSecTradeContext.place_order``（TrdMarket/TrdSide/OrderType 需显式映射）。
   - easytrader：依赖同花顺/券商客户端 UI 自动化，稳定性与合规性最差，仅建议小资金人工盯盘验证。

二、风控要求（缺一不可）
--------------------
1. 影子模式先行：真实行情 + 模拟成交，至少对比 20 个交易日的信号/成交差异后再考虑实盘。
2. 限额：单笔金额、单日累计成交额、单票集中度、最大持仓数上限；超限直接 ``OrderRejected``。
3. 价格保护：涨跌停/停牌过滤；委托价与最新价偏离超过阈值时拒绝或改限价。
4. 幂等与审计：客户端委托号唯一（复用 ``next_order_id()``），全部下单/撤单/回报落盘留痕。
5. 资金与持仓以券商回报为准，禁止用本地估算覆盖回报；对账（positions/fills）需每日跑一次。
6. 异常处理：网络中断后先查单再重试，禁止盲目重发；涨跌停/废单原因原样透传给用户。

三、安全声明
----------
本项目默认只做模拟盘；以上接入点仅为说明，未附带任何券商依赖。启用真实下单前请自行
完成合规、风控与资金隔离，风险自负。
"""

import os
from typing import Any, Dict, List

from ..core.errors import BrokerUnavailable
from ..core.models import Account, Order, OrderRequest, Position
from .broker_base import BaseBroker
from .paper import PaperBroker

#: 启用真实通道的显式开关（缺省关闭）
LIVE_TRADING_ENV = "QUANTSTUDIO_ENABLE_LIVE_TRADING"

#: 接入真实券商的最小步骤（供 /api/system/status 展示）
LIVE_TRADING_STEPS = [
    "1) 在 backend/quantstudio/trading/adapters.py 新增 BaseBroker 子类，实现 _do_submit / positions / account / cancel。",
    "2) 接入券商 SDK：QMT/miniQMT（xtquant.xttrader）、富途 OpenAPI（futu + OpenD）、easytrader（客户端 UI 自动化，稳定性最差）。",
    "3) 用券商回报同步委托/成交状态，客户端委托号复用 BaseBroker.next_order_id() 保证幂等。",
    "4) 在 available_brokers() 注册并在 trading/__init__.py 导出。",
    "5) 设置环境变量 " + LIVE_TRADING_ENV + "=1 后才允许实例化；未设置一律拒绝。",
]

#: 风控要求（供 /api/system/status 展示）
LIVE_TRADING_RISK = [
    "先跑影子模式：真实行情 + 模拟成交至少对比 20 个交易日的信号差异。",
    "限额：单笔金额、单日累计成交额、单票集中度、最大持仓数；超限直接拒单。",
    "价格保护：涨跌停/停牌过滤，委托价与最新价偏离超阈值时拒绝。",
    "幂等与审计：委托号唯一，下单/撤单/回报全程落盘留痕；网络中断后先查单再重试。",
    "资金与持仓以券商回报为准，每日自动对账；禁止用本地估算覆盖回报。",
]

_UNAVAILABLE_TEMPLATE = (
    "未接入真实券商通道（LiveBrokerStub 占位）：请在 quantstudio/trading/adapters.py 中实现 "
    "BaseBroker 子类的 _do_submit，并设置环境变量 %s=1 后再启用；本项目默认只做模拟盘，"
    "真实下单风险自负。" % LIVE_TRADING_ENV
)


class LiveBrokerStub(BaseBroker):
    """真实券商通道占位：``is_live = True``，但任何提交都抛 ``BrokerUnavailable``。"""

    name = "live"
    is_live = True
    kind = "live"
    available = False
    order_prefix = "LIVE"
    description = "真实券商通道占位（未接入）：任何下单都会抛 BrokerUnavailable，不会真实成交"
    rules = [
        "占位实现：未接入任何券商 SDK，任何提交/撤单/查询都抛 BrokerUnavailable。",
        "启用条件：自行实现 _do_submit 且显式设置 " + LIVE_TRADING_ENV + "=1。",
        "未接入前请在 /api/system/status 中向用户明示「真实通道未接入」，避免误以为可以实盘下单。",
    ]

    @property
    def enabled(self) -> bool:
        """是否显式打开了真实交易开关（仅表示用户意图，占位实现仍不可用）。"""
        return os.environ.get(LIVE_TRADING_ENV, "").strip().lower() in ("1", "true", "yes", "on")

    def _blocked(self, action: str) -> BrokerUnavailable:
        return BrokerUnavailable("真实券商通道%s不可用：%s" % (action, _UNAVAILABLE_TEMPLATE))

    def submit(self, order: OrderRequest) -> Order:
        raise self._blocked("下单")

    def _do_submit(self, order: Order) -> Order:  # pragma: no cover - 占位实现
        raise self._blocked("下单")

    def cancel(self, order_id: str) -> Order:
        raise self._blocked("撤单")

    def orders(self, limit: int = 100) -> List[Order]:
        raise self._blocked("委托查询")

    def fills(self, limit: int = 100) -> List[Any]:
        raise self._blocked("成交查询")

    def positions(self) -> List[Position]:
        raise self._blocked("持仓查询")

    def account(self) -> Account:
        raise self._blocked("账户查询")


def available_brokers() -> Dict[str, Dict[str, Any]]:
    """可用通道清单（供 ``/api/system/status`` 与下单页展示）。

    返回 ``{"paper": {...}, "live": {...}}``：paper 永远可用；live 为未接入占位。
    """
    paper = PaperBroker.describe_static()
    paper["impl"] = "quantstudio.trading.paper.PaperBroker"
    paper["default"] = True

    live = LiveBrokerStub.describe_static()
    live["impl"] = "quantstudio.trading.adapters.LiveBrokerStub"
    live["default"] = False
    live["enable_flag"] = LIVE_TRADING_ENV
    live["enabled"] = LiveBrokerStub().enabled
    live["steps"] = list(LIVE_TRADING_STEPS)
    live["risk_control"] = list(LIVE_TRADING_RISK)
    live["notes"] = [
        "本项目默认只做模拟盘；真实下单需自行接入并承担全部风险。",
        "占位实现在任何情况下都不会向券商发送委托。",
    ]
    return {"paper": paper, "live": live}
