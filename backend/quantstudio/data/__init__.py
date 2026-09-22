# -*- coding: utf-8 -*-
"""驱动（driver）——行情数据层入口。

工厂 :func:`build_provider` 按 ``settings.data_source`` 选择实现：

===============  ==========================================================
``auto``(默认)    :class:`CompositeProvider`：实时 sina 优先、历史 eastmoney 优先
                 （东财被网络阻断时自动兜到腾讯），失败逐级降级到
                 「磁盘缓存 → CSV → 示例数据」
``sina``          新浪实时快照优先；K 线 / 板块 / 广度新浪不提供，自动兜到
                 东方财富 / 腾讯（用 ``mode="sina"`` 的 Composite 实现同一语义）
``tencent``       腾讯优先（``mode="tencent"`` 的 Composite）：K 线 / 板块 / 广度 / 快照
                 全部走腾讯，东方财富与新浪作为备源
``eastmoney``     :class:`EastmoneyProvider`（快照 / K 线 / 板块 / 广度齐全）
``csv``           :class:`CsvProvider`（只用用户自备的本地 CSV）
``sample``        :class:`SampleProvider`（旧示例数据引擎，离线兜底，非真实行情）
===============  ==========================================================

:func:`get_provider` 提供进程级单例（服务层直接调用即可），
``QUANTSTUDIO_OFFLINE=1`` 时所有真实源都不会发起网络请求。

注意：``config.Settings`` 目前不允许 ``data_source="tencent"``（其允许列表由 config.py 维护），
需要腾讯优先时请用 :func:`settings_with_source` 派生配置。
"""

import copy
from typing import Optional

from ..config import Settings, get_settings
from .base import BaseHTTPProvider
from .cache import DiskCache, make_key
from .composite import CompositeProvider
from .csv_provider import CsvProvider
from .eastmoney import EastmoneyProvider
from .sample import SampleProvider
from .sina import SinaProvider
from .tencent import TencentProvider

from ..core.interfaces import DataProvider  # noqa: F401  （Protocol 转出，供类型标注与断言）
from . import symbols  # noqa: F401  （便于 data.symbols.normalize 使用）

__all__ = [
    "build_provider",
    "get_provider",
    "reset_provider",
    "settings_with_source",
    "DataProvider",
    "BaseHTTPProvider",
    "CompositeProvider",
    "CsvProvider",
    "EastmoneyProvider",
    "TencentProvider",
    "SinaProvider",
    "SampleProvider",
    "DiskCache",
    "make_key",
    "symbols",
]

#: 进程级单例缓存：``(settings 指纹, provider)``
_provider = None
_provider_key = None


def _settings_key(settings: Settings):
    """用于判断单例是否失效的配置指纹。"""
    return (
        getattr(settings, "data_source", ""),
        bool(getattr(settings, "offline", False)),
        getattr(settings, "cache_dir", ""),
        getattr(settings, "data_dir", ""),
        int(getattr(settings, "http_timeout", 0) or 0),
        int(getattr(settings, "http_retries", 0) or 0),
    )


def build_provider(settings: Optional[Settings] = None):
    """按 ``settings.data_source`` 构造数据源（不缓存，每次调用都是新实例）。"""
    conf = settings if settings is not None else get_settings()
    source = (getattr(conf, "data_source", "") or "auto").strip().lower()
    if source == "sample":
        return SampleProvider(conf)
    if source == "csv":
        return CsvProvider(conf)
    if source == "eastmoney":
        return EastmoneyProvider(conf)
    if source == "tencent":
        # 腾讯支持快照 / K 线 / 板块 / 广度，全部首选腾讯；
        # name 按要求仍为 "composite"，东方财富与新浪作为备源。
        return CompositeProvider(conf, mode="tencent")
    if source == "sina":
        # 新浪只提供实时快照；K 线 / 板块 / 广度自动兜到东方财富 / 腾讯
        # （再往下还有缓存 / CSV / 示例）。用 mode="sina" 的 Composite 实现这一语义，
        # 同时保持 name == "sina"。
        return CompositeProvider(conf, mode="sina")
    return CompositeProvider(conf, mode="auto")


def get_provider(settings: Optional[Settings] = None, reload: bool = False):
    """进程级单例 Provider（服务层调用入口）。

    ``settings`` 变化（数据源 / 离线开关 / 缓存目录等）时自动重建单例。
    """
    global _provider, _provider_key
    conf = settings if settings is not None else get_settings()
    key = _settings_key(conf)
    if reload or _provider is None or _provider_key != key:
        _provider = build_provider(conf)
        _provider_key = key
    return _provider


def settings_with_source(settings: Optional[Settings] = None, source: str = "auto") -> Settings:
    """返回一份 ``data_source=<source>`` 的 Settings 副本。

    背景：``config.Settings`` 会校验 ``QUANTSTUDIO_DATA_SOURCE`` 的取值（当前允许
    ``auto/sina/eastmoney/csv/sample``，**不含 tencent**，且 config.py 不在数据层的职责范围内）。
    因此选择腾讯源请用本函数派生配置，例如::

        from quantstudio.data import build_provider, settings_with_source
        provider = build_provider(settings_with_source(None, "tencent"))

    等 ``config.py`` 的允许列表补上 ``tencent`` 后，直接设环境变量即可，无需改这里。
    """
    conf = settings if settings is not None else get_settings()
    clone = copy.copy(conf)
    clone.data_source = str(source or "auto").strip().lower()
    return clone
def reset_provider() -> None:
    """丢弃单例（测试 / 配置热更新用）。"""
    global _provider, _provider_key
    _provider = None
    _provider_key = None
