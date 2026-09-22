# -*- coding: utf-8 -*-
"""统一异常类型（供数据层 / 回测层 / 接口层共享，避免互相 import 形成环）。"""


class QuantStudioError(Exception):
    """本工程所有自定义异常的基类。"""


class ConfigError(QuantStudioError):
    """配置非法（环境变量/配置文件内容错误）。"""


class ValidationError(QuantStudioError):
    """入参校验失败（接口层应返回 400）。"""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


class DataSourceError(QuantStudioError):
    """数据源不可用或返回异常（网络、限流、解析失败）。"""


class SymbolNotFound(DataSourceError):
    """标的代码不存在或无法识别。"""


class ProviderUnavailable(DataSourceError):
    """该 Provider 在当前环境不可用（缺少依赖 / 未配置）。"""


class InsufficientData(QuantStudioError):
    """历史数据不足（回测所需的最少 bar 数不满足）。"""


class OrderRejected(QuantStudioError):
    """委托被拒绝（资金不足、涨跌停、T+1 未解禁、最小交易单位等）。"""


class BrokerUnavailable(QuantStudioError):
    """交易通道不可用（模拟盘未启动 / 真实券商未接入）。"""
