# -*- coding: utf-8 -*-
"""集中配置：环境变量 > 配置文件 > 默认值。

环境变量（全部可选，前缀 QUANTSTUDIO_）
--------------------------------------
- QUANTSTUDIO_DATA_SOURCE   数据源：auto(默认) / sina / eastmoney / csv / sample
- QUANTSTUDIO_CACHE_DIR     缓存目录（默认 backend/data/cache）
- QUANTSTUDIO_DATA_DIR      数据目录（默认 backend/data）
- QUANTSTUDIO_HOST / PORT   监听地址与端口（默认 127.0.0.1:8000）
- QUANTSTUDIO_CACHE_TTL_*   各接口缓存秒数：QUOTES / KLINE / SECTORS / BREADTH
- QUANTSTUDIO_OFFLINE       1 = 只使用本地数据（缓存/CSV/示例），不发起网络请求
- QUANTSTUDIO_INITIAL_CASH  模拟盘初始资金（默认 1,000,000）
- QUANTSTUDIO_BENCHMARK     回测默认基准（默认 000300.SH 沪深300）
- QUANTSTUDIO_LOG_LEVEL     DEBUG/INFO/WARNING（默认 INFO）
"""

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .core.errors import ConfigError

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
PROJECT_DIR = os.path.dirname(BASE_DIR)                                  # 仓库根目录


def _env(name: str, default: str = "") -> str:
    return os.environ.get("QUANTSTUDIO_" + name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError("环境变量 QUANTSTUDIO_%s 需为整数，当前为 %r" % (name, raw))


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError("环境变量 QUANTSTUDIO_%s 需为数字，当前为 %r" % (name, raw))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


@dataclass
class CacheTTL:
    """缓存有效期（秒）。行情类数据短、K 线类数据长。"""

    quotes: int = 5
    kline_today: int = 300        # 当日未收盘的最后一根 bar
    kline_closed: int = 6 * 3600  # 已收盘 bar（历史不变，可长缓存）
    sectors: int = 30
    breadth: int = 60
    name: int = 24 * 3600

    def to_dict(self) -> Dict[str, int]:
        return {
            "quotes": self.quotes,
            "kline_today": self.kline_today,
            "kline_closed": self.kline_closed,
            "sectors": self.sectors,
            "breadth": self.breadth,
            "name": self.name,
        }


@dataclass
class Settings:
    """运行时配置。"""

    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8000))
    data_source: str = field(default_factory=lambda: _env("DATA_SOURCE", "auto") or "auto")
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", os.path.join(BASE_DIR, "data")))
    cache_dir: str = field(default_factory=lambda: _env("CACHE_DIR", ""))
    offline: bool = field(default_factory=lambda: _env_bool("OFFLINE", False))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO") or "INFO")
    initial_cash: float = field(default_factory=lambda: _env_float("INITIAL_CASH", 1_000_000.0))
    benchmark: str = field(default_factory=lambda: _env("BENCHMARK", "000300.SH") or "000300.SH")
    cache_ttl: CacheTTL = field(default_factory=CacheTTL)
    http_timeout: int = field(default_factory=lambda: _env_int("HTTP_TIMEOUT", 8))
    http_retries: int = field(default_factory=lambda: _env_int("HTTP_RETRIES", 2))

    # 指数看板默认标的（代码使用本项目规范化格式）
    index_symbols: List[str] = field(default_factory=lambda: [
        "000001.SH", "399001.SZ", "399006.SZ", "000688.SH",
    ])
    # 默认自选股（首次启动写入 data/watchlist.json，之后以文件为准）
    default_watchlist: List[str] = field(default_factory=lambda: [
        "600519.SH", "300750.SZ", "002594.SZ", "600036.SH", "601318.SH",
        "000858.SZ", "600900.SH", "601012.SH", "600276.SH", "300059.SZ",
    ])
    # 默认板块（东财行业板块）取前 N
    sector_limit: int = field(default_factory=lambda: _env_int("SECTOR_LIMIT", 20))
    # 行情看板默认 K 线天数
    default_kline_days: int = field(default_factory=lambda: _env_int("KLINE_DAYS", 250))
    #: 回测单次最多取多少根日线：5000 根 ≈ 20 年（腾讯实测可回溯到 2001 年，单次最多 8 页 × 800 = 6400 根）
    max_kline_days: int = field(default_factory=lambda: _env_int("MAX_KLINE_DAYS", 5000))

    def __post_init__(self) -> None:
        if not self.cache_dir:
            self.cache_dir = os.path.join(self.data_dir, "cache")
        if self.data_source not in ("auto", "sina", "eastmoney", "csv", "sample"):
            raise ConfigError(
                "QUANTSTUDIO_DATA_SOURCE 仅支持 auto/sina/eastmoney/csv/sample，当前为 %r"
                % self.data_source
            )
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)

    # ---- 路径工具 ----
    def path(self, *parts: str) -> str:
        return os.path.join(self.data_dir, *parts)

    @property
    def watchlist_path(self) -> str:
        return self.path("watchlist.json")

    @property
    def portfolio_path(self) -> str:
        return self.path("portfolio.json")

    @property
    def user_strategies_path(self) -> str:
        return self.path("user_strategies.json")

    @property
    def account_path(self) -> str:
        return self.path("paper_account.json")

    @property
    def csv_dir(self) -> str:
        return self.path("csv")

    def describe(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "data_source": self.data_source,
            "offline": self.offline,
            "data_dir": self.data_dir,
            "cache_dir": self.cache_dir,
            "benchmark": self.benchmark,
            "initial_cash": self.initial_cash,
            "cache_ttl": self.cache_ttl.to_dict(),
            "index_symbols": list(self.index_symbols),
            "default_kline_days": self.default_kline_days,
        }


_settings = None


def get_settings(reload: bool = False) -> Settings:
    """进程级单例配置。"""
    global _settings
    if _settings is None or reload:
        _settings = Settings()
    return _settings
