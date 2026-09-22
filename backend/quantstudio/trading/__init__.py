# -*- coding: utf-8 -*-
"""交易执行层：模拟盘（默认安全）+ 真实券商适配占位。

对外导出
--------
- ``BaseBroker``           共用逻辑基类（委托号/状态机/校验/流水裁剪）
- ``PaperBroker``          模拟盘：本地撮合、T+1、原子落盘（默认且唯一可用通道）
- ``LiveBrokerStub``       真实券商占位：任何下单都抛 BrokerUnavailable
- ``available_brokers()``  通道清单（供 /api/system/status 展示）
- ``get_paper_broker()``   进程级模拟盘单例（默认使用 settings.account_path）
- ``compute_fee`` / ``apply_slippage`` / ``normalize_code``  供回测与接口层复用的纯函数
"""

from typing import Dict
from ..config import get_settings
from .adapters import LIVE_TRADING_ENV, LiveBrokerStub, available_brokers
from .broker_base import BaseBroker, apply_slippage, compute_fee, normalize_code
from .paper import PaperBroker

__all__ = [
    "BaseBroker",
    "PaperBroker",
    "LiveBrokerStub",
    "available_brokers",
    "get_paper_broker",
    "compute_fee",
    "apply_slippage",
    "normalize_code",
    "LIVE_TRADING_ENV",
]

#: 按 account_path 缓存模拟盘实例，避免同进程内重复读写同一账户文件
_PAPER_BROKERS: Dict[str, PaperBroker] = {}


def get_paper_broker(settings=None, provider=None, reload: bool = False) -> PaperBroker:
    """进程级模拟盘单例（默认使用 ``settings.account_path`` 作为账户文件）。

    同一 ``account_path`` 只保留一个实例，保证接口层与后台任务看到一致的账户状态。
    """
    config = settings or get_settings()
    key = config.account_path
    if reload or key not in _PAPER_BROKERS:
        _PAPER_BROKERS[key] = PaperBroker(
            provider=provider,
            account_path=key,
            initial_cash=config.initial_cash,
        )
    return _PAPER_BROKERS[key]
