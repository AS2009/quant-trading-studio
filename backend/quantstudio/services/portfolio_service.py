# -*- coding: utf-8 -*-
"""持仓 + 模拟盘交易业务服务。

- 真实持仓（manual 模式）：包装 ``quantstudio.portfolio.PortfolioService``
- 模拟盘（paper 模式）：包装 ``quantstudio.trading.get_paper_broker()`` 的 PaperBroker

下单 / 撤单 / 增删持仓后统一刷新持仓缓存（进程内 TTL 缓存）。
底层模块一律惰性导入，测试可注入替身。
"""

import threading
from typing import Any, Dict, List, Optional, Tuple

from ..config import Settings, get_settings
from ..core.errors import BrokerUnavailable, OrderRejected, ValidationError
from ..core.models import OrderRequest
from .common import (
    NotFound,
    cached,
    cache_clear,
    normalize_code,
    now_str,
    parse_float,
    parse_int,
    to_dict,
    to_dict_list,
)

MANUAL = "manual"
PAPER = "paper"


class PortfolioService:
    """账户总览 / 持仓 / 权益曲线 / 模拟盘委托与成交。"""

    HOLDINGS_TTL = 3

    def __init__(
        self,
        provider: Any = None,
        settings: Optional[Settings] = None,
        broker: Any = None,
        backend: Any = None,
    ):
        self.settings = settings or get_settings()
        self._provider = provider
        self._broker = broker
        self._backend = backend
        self._mode = MANUAL
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 惰性依赖

    @property
    def backend(self):
        """真实持仓账户（quantstudio.portfolio.PortfolioService）。"""
        if self._backend is None:
            from ..portfolio import PortfolioService as BackendPortfolioService

            self._backend = BackendPortfolioService(provider=self._provider, settings=self.settings)
        return self._backend

    @property
    def broker(self):
        """模拟盘通道（quantstudio.trading.PaperBroker）。"""
        if self._broker is None:
            try:
                from ..trading import get_paper_broker

                self._broker = get_paper_broker()
            except ImportError as exc:
                raise BrokerUnavailable("模拟盘通道不可用：%s" % exc)
        return self._broker

    # ------------------------------------------------------------------ 模式

    def mode(self) -> str:
        try:
            value = getattr(self.backend, "mode", None)
            if callable(value):
                value = value()
            if value in (MANUAL, PAPER):
                return value
        except Exception:
            pass
        return self._mode

    def set_mode(self, mode: str) -> Dict[str, str]:
        text = str(mode or "").strip().lower()
        if text not in (MANUAL, PAPER):
            raise ValidationError("mode 仅支持 manual/paper，当前为 %r" % mode, field="mode")
        setter = getattr(self.backend, "set_mode", None)
        if callable(setter):
            setter(text)
        with self._lock:
            self._mode = text
        self._invalidate_holdings()
        return {"mode": text}

    # ------------------------------------------------------------------ 账户 / 持仓

    def _account_obj(self):
        if self.mode() == PAPER:
            return self.broker.account()
        return self.backend.account()

    def overview(self) -> Dict[str, Any]:
        return to_dict(self._account_obj())

    def _holdings_key(self) -> str:
        return "portfolio:holdings:" + self.mode()

    def holdings(self) -> List[Dict[str, Any]]:
        return cached(self._holdings_key(), self.HOLDINGS_TTL, self._load_holdings)

    def _load_holdings(self) -> List[Dict[str, Any]]:
        if self.mode() == PAPER:
            return to_dict_list(self.broker.positions())
        return to_dict_list(self.backend.holdings())

    def _invalidate_holdings(self) -> None:
        cache_clear("portfolio:holdings:" + MANUAL)
        cache_clear("portfolio:holdings:" + PAPER)

    def equity_curve(self, days: int = 90) -> Tuple[List[Dict[str, Any]], List[str]]:
        """返回 (points, notes)；points 兼容旧前端的 ``[{date, equity}]`` 数组。

        上游可能返回 dict（``items`` / ``points``，权益字段为 ``equity`` 或 ``total_assets``）。
        """
        result = self.backend.equity_curve(days)
        notes: List[str] = []
        if isinstance(result, dict):
            points = result.get("points") or result.get("items") or []
            notes = [str(x) for x in (result.get("notes") or [])]
        else:
            points = result or []
        normalized: List[Dict[str, Any]] = []
        for item in points:
            if hasattr(item, "to_dict"):
                item = item.to_dict()
            elif not isinstance(item, dict):
                raise TypeError("权益曲线元素需为 dict：%r" % (type(item),))
            row = dict(item)
            if "equity" not in row and "total_assets" in row:
                row["equity"] = row["total_assets"]
            normalized.append(row)
        return normalized, notes

    # ------------------------------------------------------------------ 持仓增删改 / 现金

    def upsert_holding(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("请求体需为 JSON 对象", field="body")
        code = normalize_code(payload.get("code"), field="code")
        if payload.get("qty") in (None, ""):
            raise ValidationError("qty 不能为空", field="qty")
        qty = parse_int(payload.get("qty"), "qty", minimum=1)
        item: Dict[str, Any] = {"code": code, "qty": qty}
        if payload.get("cost") not in (None, ""):
            cost = parse_float(payload.get("cost"), "cost", minimum=0.0)
            if cost <= 0:
                raise ValidationError("cost 必须大于 0，当前为 %s" % cost, field="cost")
            item["cost"] = cost
        if payload.get("available_qty") not in (None, ""):
            available = parse_int(payload.get("available_qty"), "available_qty", minimum=0)
            if available > qty:
                raise ValidationError(
                    "available_qty 不能大于 qty（%s > %s）" % (available, qty),
                    field="available_qty",
                )
            item["available_qty"] = available
        for key in ("name", "industry"):
            value = payload.get(key)
            if value not in (None, ""):
                item[key] = str(value)
        if payload.get("price") is not None:
            item["price"] = parse_float(payload.get("price"), "price", minimum=0.0)

        result = self.backend.upsert_holding(item)
        self._invalidate_holdings()
        return to_dict(result)

    def delete_holding(self, code: str) -> Dict[str, Any]:
        code = normalize_code(code)
        try:
            result = self.backend.delete_holding(code)
        except ValidationError as exc:
            raise NotFound(str(exc) or ("持仓不存在：%s" % code))
        if result is False:
            raise NotFound("持仓不存在：%s" % code)
        self._invalidate_holdings()
        return {"code": code, "deleted": True}

    def set_cash(self, amount: Any) -> Dict[str, Any]:
        cash = parse_float(amount, "amount", minimum=0.0, maximum=1e12)
        result = self.backend.set_cash(cash)
        self._invalidate_holdings()
        if result is None or isinstance(result, (bool, int, float)):
            return self.overview()
        return to_dict(result)

    # ------------------------------------------------------------------ 模拟盘交易

    def orders(self, limit: Any = 100) -> List[Dict[str, Any]]:
        count = parse_int(limit, "limit", default=100, minimum=1, maximum=1000, clamp=True)
        return to_dict_list(self.broker.orders(count))

    def fills(self, limit: Any = 100) -> List[Dict[str, Any]]:
        count = parse_int(limit, "limit", default=100, minimum=1, maximum=1000, clamp=True)
        return to_dict_list(self.broker.fills(count))

    def _find_fill(self, order_id: Any) -> Optional[Dict[str, Any]]:
        if not order_id:
            return None
        try:
            recent = to_dict_list(self.broker.fills(50))
        except Exception:
            return None
        for item in recent:
            if item.get("order_id") == order_id:
                return item
        return None

    def submit_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("请求体需为 JSON 对象", field="body")
        code = normalize_code(payload.get("code"), field="code")
        side = str(payload.get("side") or "").strip().lower()
        if side not in ("buy", "sell"):
            raise ValidationError(
                "side 仅支持 buy/sell，当前为 %r" % payload.get("side"), field="side"
            )
        qty = parse_int(payload.get("qty"), "qty", minimum=1, maximum=100000000)
        price = parse_float(
            payload.get("price"), "price", allow_none=True, minimum=0.0, maximum=10000000.0
        )
        if price is not None and price <= 0:
            raise ValidationError("price 需大于 0", field="price")

        request = OrderRequest(
            code=code,
            side=side,
            qty=qty,
            price=price,
            reason=str(payload.get("reason") or "manual"),
            ts=now_str(),
        )
        order = to_dict(self.broker.submit(request))
        if order.get("status") == "rejected":
            raise OrderRejected(order.get("reason") or "委托被拒绝：%s" % code)
        fill = self._find_fill(order.get("order_id"))
        self._invalidate_holdings()
        return {"order": order, "fill": fill}

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        if not order_id:
            raise ValidationError("order_id 不能为空", field="order_id")
        try:
            order = self.broker.cancel(order_id)
        except LookupError:
            raise NotFound("委托不存在：%s" % order_id)
        except ValidationError as exc:
            # PaperBroker 对未知委托抛 ValidationError("未找到委托：...") → 语义为 404
            raise NotFound(str(exc) or ("委托不存在：%s" % order_id))
        except ValueError as exc:
            raise ValidationError(str(exc) or "该委托无法撤销")
        if order is None:
            raise NotFound("委托不存在：%s" % order_id)
        self._invalidate_holdings()
        return to_dict(order)

    def reset(self, initial_cash: Any = None) -> Dict[str, Any]:
        cash = None
        if initial_cash not in (None, ""):
            cash = parse_float(initial_cash, "initial_cash", minimum=0.0, maximum=1e12)
        broker = self.broker
        try:
            account = broker.reset(cash) if cash is not None else broker.reset()
        except TypeError:
            account = broker.reset()
        if account is None or isinstance(account, bool):
            account = broker.account()
        self._invalidate_holdings()
        return to_dict(account)
