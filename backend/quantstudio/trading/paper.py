# -*- coding: utf-8 -*-
"""模拟盘 `PaperBroker`：本地撮合、T+1、原子落盘，**默认且唯一可直接使用的通道**。

撮合与拒单规则（接口层可直接透出给用户，见 `PaperBroker.rules`）
----------------------------------------------------------------
1. 数量：qty 必须 > 0 且为 lot_size（默认 100 股）整数倍，否则 ``status=rejected``。
2. 价格：市价单按「最新价 ± 滑点」（默认 2bps）成交；限价单仅在价格可成交时成交
   （买入：限价 ≥ 最新价；卖出：限价 ≤ 最新价），成交价取最新价（限价单不叠加滑点）。
3. 挂单：限价不可成交时状态为 ``new`` 并留在委托列表；**本模拟盘不做后台撮合，
   行情后续满足也不会自动成交，需要用户手动撤单**（``cancel(order_id)``）。
4. 资金：买入需「成交额 + 费用」≤ 可用现金；挂单时也做一次预检（按限价估算），不足即拒单。
5. T+1：当日买入部分 ``available_qty=0``，当日不可卖；跨自然日自动解禁（可注入 clock 便于测试）。
6. 标的：``provider.latest_quotes`` 取不到该标的最新价（或最新价为 0）时拒单；
   仅在**未注入 provider 的离线模式**下才允许用委托价作为参考价撮合。
7. 费用：与回测同口径 —— 佣金（双边，最低 5 元）+ 过户费（双边 0.001%）+ 印花税（仅卖出 0.05%）。
8. 记账：买入按含费用的加权平均成本摊薄；卖出按该成本结算已实现盈亏（与回测口径一致）。
9. 无实时推送：``positions()`` / ``account()`` 只在被调用时用最新快照估值。
10. 持久化：账户（现金/持仓/委托/成交/现金流）原子写 JSON 到 ``account_path``，启动即加载；
    文件损坏时备份 ``.bak``、打印 warning 并重新初始化，服务不崩。

以上所有拒单都**不抛异常**，而是返回带可读 ``reason`` 的 rejected 委托；接口层只需展示。
"""

import json
import os
from dataclasses import fields as _dc_fields
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..core import costs
from ..core.errors import OrderRejected, ValidationError
from ..core.models import Account, FeeConfig, Fill, Order, Position, Quote
from .broker_base import BaseBroker, compute_fee, log_warn, normalize_code

_STATE_VERSION = 1


def _rebuild(model_cls, row):
    """按 dataclass 字段白名单重建模型；结构异常返回 None（跳过该行）。"""
    try:
        names = {item.name for item in _dc_fields(model_cls)}
        return model_cls(**{key: value for key, value in row.items() if key in names})
    except Exception:
        return None


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


class PaperBroker(BaseBroker):
    """模拟盘通道（`Broker` 协议实现；`is_live` 恒为 False）。"""

    name = "paper"
    is_live = False
    kind = "simulated"
    available = True
    order_prefix = "PB"
    description = "内置模拟盘：本地撮合、T+1、最新价±滑点成交，账户原子落盘（默认通道）"
    rules = [
        "数量必须 > 0 且为 100 股的整数倍（1 手 = 100 股），否则拒单。",
        "市价单按最新价 ± 滑点（默认 2bps）成交；限价单需价格可成交（买：限价≥最新价，卖：限价≤最新价），成交价取最新价。",
        "限价不成交时挂为 new 且不会自动撮合，需手动撤单（cancel）。",
        "买入资金不足（成交额+费用 > 可用现金）拒单；卖出超过可卖数量拒单。",
        "T+1：当日买入的股票 available_qty=0，当日不可卖，次一交易日自动解禁。",
        "最新价取不到（标的不存在/行情不可用/停牌价为 0）时拒单。",
        "费用：佣金（双边，最低 5 元）+ 过户费（双边 0.001%）+ 印花税（仅卖出 0.05%），与回测口径一致。",
        "账户落盘到 account_path（原子写）；文件损坏时自动备份 .bak 并重置。",
    ]

    def __init__(
        self,
        provider=None,
        account_path: Optional[str] = None,
        initial_cash: float = 1_000_000.0,
        fee=None,
        slippage_bps: float = 2.0,
        clock: Optional[Callable[[], Any]] = None,
    ):
        super().__init__(fee=fee, clock=clock)
        self.provider = provider
        self.account_path = os.path.abspath(account_path) if account_path else None
        self.slippage_bps = float(slippage_bps)
        self.initial_cash = float(initial_cash)
        self._cash = float(initial_cash)
        self._positions: Dict[str, Dict[str, Any]] = {}
        self._cash_flow: List[Dict[str, Any]] = []
        self._provider_missing = False
        self._load()

    # ------------------------------------------------------------------ 行情

    def _provider(self):
        """懒加载默认 provider（引用注入优先；导入失败则降级为离线模式）。"""
        if self.provider is not None:
            return self.provider
        if self._provider_missing:
            return None
        try:
            from ..data import get_provider  # 数据层由 quantstudio.data 提供

            self.provider = get_provider()
        except Exception as exc:  # pragma: no cover - 取决于运行环境
            self._provider_missing = True
            log_warn("未注入行情 provider，模拟盘以离线模式运行（无法按最新价撮合）：%s" % exc)
            return None
        return self.provider

    def _quotes(self, codes: Sequence[str]) -> Dict[str, Quote]:
        provider = self._provider()
        if provider is None or not codes:
            return {}
        try:
            rows = provider.latest_quotes(list(codes)) or []
        except Exception as exc:
            log_warn("获取最新价失败：%s" % exc)
            return {}
        out: Dict[str, Quote] = {}
        for row in rows:
            code = str(getattr(row, "code", "") or "")
            if not code:
                continue
            try:
                code = normalize_code(code)
            except ValidationError:
                code = code.upper()
            out[code] = row
        return out

    def _name_of(self, code: str, quote: Optional[Quote] = None) -> str:
        if quote is not None and getattr(quote, "name", ""):
            return str(quote.name)
        provider = self._provider()
        if provider is None:
            return ""
        try:
            return str(provider.resolve_name(code) or "")
        except Exception:
            return ""

    # ------------------------------------------------------------------ 撮合

    def _do_submit(self, order: Order) -> Order:
        today = self.today()
        quote = self._quotes([order.code]).get(order.code)
        if not order.name:
            order.name = self._name_of(order.code, quote)

        reference = self._reference_price(order, quote)
        pending_reason = None
        if order.price is not None:
            if order.side == "buy" and order.price < reference:
                pending_reason = "限价 %.2f 低于最新价 %.2f，挂单等待（本模拟盘不做后台撮合，需手动撤单）" % (
                    order.price, reference,
                )
            elif order.side == "sell" and order.price > reference:
                pending_reason = "限价 %.2f 高于最新价 %.2f，挂单等待（本模拟盘不做后台撮合，需手动撤单）" % (
                    order.price, reference,
                )
            fill_price = round(reference, 2)      # 限价可成交时按最新价成交（对用户更优）
        else:
            # 成交价与回测同一口径（core.costs）：比例滑点用模拟盘的 slippage_bps，tick 滑点沿用费率配置
            fill_cfg = FeeConfig(**self.fee.to_dict())
            fill_cfg.slippage_bps = float(self.slippage_bps or 0.0)
            fill_price = costs.exec_price(order.side, reference, fill_cfg)

        if fill_price <= 0:
            raise OrderRejected("成交价非法（%.4f），已拒单" % fill_price)

        amount = round(fill_price * order.qty, 2)
        fee = compute_fee(amount, order.side, self.fee)

        if order.side == "buy":
            self._precheck_buy(order, amount, fee, pending_reason)
            if pending_reason:
                return self._transition(order, "new", reason=pending_reason)
            self._fill_buy(order, quote, fill_price, amount, fee, today)
        else:
            self._precheck_sell(order, today)
            if pending_reason:
                return self._transition(order, "new", reason=pending_reason)
            self._fill_sell(order, quote, fill_price, amount, fee, today)

        fill = Fill(
            order_id=order.order_id,
            code=order.code,
            name=order.name,
            side=order.side,
            price=fill_price,
            qty=order.qty,
            amount=amount,
            fee=fee,
            ts=self.now(),
            reason=order.reason,
        )
        self._record_fill(fill)
        return self._transition(
            order, "filled", reason=order.reason, filled_qty=order.qty, avg_price=fill_price, fee=fee
        )

    def _reference_price(self, order: Order, quote: Optional[Quote]) -> float:
        if quote is not None:
            price = _num(getattr(quote, "price", 0.0), 0.0)
            if price > 0:
                return price
            raise OrderRejected("最新价为 0（可能停牌），无法撮合 %s，已拒单" % order.code)
        if self._provider() is None and order.price is not None:
            return float(order.price)          # 离线模式：以委托价为参考价
        raise OrderRejected("无法获取 %s 的最新价（标的不存在或行情不可用），已拒单" % order.code)

    def _precheck_buy(self, order: Order, amount: float, fee: float, pending_reason: Optional[str]) -> None:
        if pending_reason and order.price is not None:
            amount = round(order.price * order.qty, 2)   # 挂单按限价预估占用
            fee = compute_fee(amount, "buy", self.fee)
        need = round(amount + fee, 2)
        if need > self._cash + 1e-6:
            raise OrderRejected(
                "资金不足：买入 %s %d 股需要 %.2f 元（成交额 %.2f + 费用 %.2f），可用现金 %.2f 元"
                % (order.code, order.qty, need, amount, fee, self._cash)
            )

    def _precheck_sell(self, order: Order, today: str) -> None:
        pos = self._positions.get(order.code)
        if pos is not None:
            self._settle(pos, today)
        held = int(pos["qty"]) if pos else 0
        today_buy = int(pos.get("today_buy_qty", 0)) if pos else 0
        available = max(held - today_buy, 0)
        if available < order.qty:
            raise OrderRejected(
                "可卖数量不足：%s 当前持有 %d 股、可卖 %d 股（T+1：当日买入 %d 股次日才可卖），委托卖出 %d 股"
                % (order.code, held, available, today_buy, order.qty)
            )

    # ------------------------------------------------------------------ 记账

    @staticmethod
    def _settle(pos: Dict[str, Any], today: str) -> None:
        """跨日结算：昨日买入的部分今日自动可卖。"""
        if str(pos.get("today") or "") != today:
            pos["today"] = today
            pos["today_buy_qty"] = 0

    @staticmethod
    def _touch_price(pos: Dict[str, Any], quote: Optional[Quote]) -> None:
        if quote is None:
            return
        price = _num(getattr(quote, "price", 0.0), 0.0)
        prev_close = _num(getattr(quote, "prev_close", 0.0), 0.0)
        if price > 0:
            pos["price"] = price
        if prev_close > 0:
            pos["prev_close"] = prev_close

    def _fill_buy(self, order: Order, quote: Optional[Quote], price: float, amount: float, fee: float, today: str) -> None:
        need = round(amount + fee, 2)
        pos = self._positions.get(order.code)
        if pos is None:
            pos = {
                "code": order.code, "name": order.name, "qty": 0, "cost": 0.0,
                "today": today, "today_buy_qty": 0, "price": price, "prev_close": 0.0,
            }
            self._positions[order.code] = pos
        self._settle(pos, today)
        old_value = int(pos["qty"]) * _num(pos.get("cost"), 0.0)
        new_qty = int(pos["qty"]) + order.qty
        pos["cost"] = round((old_value + amount + fee) / new_qty, 4)   # 摊薄成本（含买入费用）
        pos["qty"] = new_qty
        pos["today_buy_qty"] = int(pos.get("today_buy_qty", 0)) + order.qty
        pos["name"] = order.name or pos.get("name") or ""
        self._touch_price(pos, quote)
        self._cash = round(self._cash - need, 2)
        self._cash_flow.append({
            "ts": self.now(), "type": "buy", "code": order.code, "order_id": order.order_id,
            "amount": -need, "balance": self._cash,
            "note": "买入 %d 股 @ %.2f（费用 %.2f）" % (order.qty, price, fee),
        })

    def _fill_sell(self, order: Order, quote: Optional[Quote], price: float, amount: float, fee: float, today: str) -> None:
        pos = self._positions.get(order.code) or {}
        self._settle(pos, today)
        cost = _num(pos.get("cost"), 0.0)
        realized = round((price - cost) * order.qty - fee, 4)           # 加权平均成本结算
        proceeds = round(amount - fee, 2)
        pos["qty"] = int(pos["qty"]) - order.qty
        pos["name"] = order.name or pos.get("name") or ""
        self._touch_price(pos, quote)
        if int(pos["qty"]) <= 0:
            self._positions.pop(order.code, None)
        self._cash = round(self._cash + proceeds, 2)
        self._cash_flow.append({
            "ts": self.now(), "type": "sell", "code": order.code, "order_id": order.order_id,
            "amount": proceeds, "balance": self._cash, "realized_pnl": realized,
            "note": "卖出 %d 股 @ %.2f（费用 %.2f，实现盈亏 %.2f）" % (order.qty, price, fee, realized),
        })

    # ------------------------------------------------------------------ 账户视图

    def _position_view(self, pos: Dict[str, Any], quote: Optional[Quote]) -> Position:
        qty = int(pos.get("qty") or 0)
        cost = _num(pos.get("cost"), 0.0)
        cost_value = round(cost * qty, 2)
        price = _num(getattr(quote, "price", 0.0), 0.0) if quote is not None else 0.0
        if price <= 0:
            price = _num(pos.get("price"), 0.0) or cost      # 行情缺失：退回最近一次价格/成本
        prev_close = _num(getattr(quote, "prev_close", 0.0), 0.0) if quote is not None else 0.0
        if prev_close <= 0:
            prev_close = _num(pos.get("prev_close"), 0.0)
        market_value = round(price * qty, 2)
        total_pnl = round(market_value - cost_value, 2)
        return Position(
            code=str(pos.get("code") or ""),
            name=str(pos.get("name") or ""),
            qty=qty,
            available_qty=max(qty - int(pos.get("today_buy_qty") or 0), 0),
            cost=round(cost, 4),
            price=round(price, 4),
            market_value=market_value,
            cost_value=cost_value,
            day_pnl=round((price - prev_close) * qty, 2) if prev_close > 0 else 0.0,
            total_pnl=total_pnl,
            return_pct=round(total_pnl / cost_value * 100, 2) if cost_value else 0.0,
        )

    def positions(self) -> List[Position]:
        """持仓（用最新价估值；行情缺失时退回最近一次价格或成本价，不崩）。"""
        today = self.today()
        if not self._positions:
            return []
        quotes = self._quotes(list(self._positions.keys()))
        out: List[Position] = []
        for code, pos in list(self._positions.items()):
            self._settle(pos, today)
            out.append(self._position_view(pos, quotes.get(code)))
        out.sort(key=lambda item: item.market_value, reverse=True)
        return out

    @staticmethod
    def _allocation(cash: float, positions: Sequence[Position], total: float) -> List[Dict[str, Any]]:
        def pct(value: float) -> float:
            return round(value / total * 100, 2) if total > 0 else 0.0

        rows = [{"code": "CASH", "name": "现金", "value": round(cash, 2), "pct": pct(cash)}]
        for pos in positions:
            rows.append({
                "code": pos.code,
                "name": pos.name or pos.code,
                "value": round(pos.market_value, 2),
                "pct": pct(pos.market_value),
            })
        return rows

    def account(self) -> Account:
        """账户总览：总资产 = 现金 + 市值；含累计收益与 allocation。"""
        positions = self.positions()
        cash = round(self._cash, 2)
        market_value = round(sum(item.market_value for item in positions), 2)
        total = round(cash + market_value, 2)
        day_pnl = round(sum(item.day_pnl for item in positions), 2)
        total_pnl = round(total - self.initial_cash, 2)
        return Account(
            total_assets=total,
            market_value=market_value,
            cash=cash,
            available_cash=cash,
            frozen_cash=0.0,
            day_pnl=day_pnl,
            total_pnl=total_pnl,
            total_return_pct=round(total_pnl / self.initial_cash * 100, 2) if self.initial_cash else 0.0,
            positions_count=len([item for item in positions if item.qty > 0]),
            allocation=self._allocation(cash, positions, total),
            as_of=self.now(),
        )

    def cash_flow(self, limit: int = 100) -> List[Dict[str, Any]]:
        """资金流水，最新的在前。"""
        items = [dict(row) for row in reversed(self._cash_flow)]
        return items[: int(limit)] if limit and limit > 0 else items

    # ------------------------------------------------------------------ 持久化

    def _state(self) -> Dict[str, Any]:
        today = self.today()
        positions = []
        for pos in self._positions.values():
            row = dict(pos)
            row["available_qty"] = max(int(row.get("qty") or 0) - int(row.get("today_buy_qty") or 0), 0)
            positions.append(row)
        return {
            "version": _STATE_VERSION,
            "name": self.name,
            "is_live": False,
            "initial_cash": self.initial_cash,
            "cash": self._cash,
            "slippage_bps": self.slippage_bps,
            "seq": self._seq,
            "today": today,
            "positions": positions,
            "orders": [item.to_dict() for item in self._orders],
            "fills": [item.to_dict() for item in self._fills],
            "cash_flow": list(self._cash_flow),
            "updated_at": self.now(),
        }

    def _save(self) -> None:
        """原子写账户文件（临时文件 + os.replace）。"""
        path = self.account_path
        if not path:
            return
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state(), fh, ensure_ascii=False, indent=2, allow_nan=False)
            os.replace(tmp, path)
        except OSError as exc:
            log_warn("模拟盘账户落盘失败：%s" % exc)

    def _reset_state(self, initial_cash: float) -> None:
        self.initial_cash = float(initial_cash)
        self._cash = float(initial_cash)
        self._positions = {}
        self._orders = []
        self._fills = []
        self._cash_flow = []
        self._seq = 0

    def _load(self) -> None:
        path = self.account_path
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict):
                raise ValueError("账户文件顶层不是 JSON 对象")
            self._from_payload(payload)
        except Exception as exc:
            self._backup_and_reset(exc)

    def _backup_and_reset(self, exc) -> None:
        target = self.account_path + ".bak"
        log_warn("模拟盘账户文件损坏（%s: %s），已备份为 %s 并重新初始化" % (self.account_path, exc, target))
        try:
            os.replace(self.account_path, target)
        except OSError as err:
            log_warn("备份损坏账户文件失败：%s" % err)
        self._reset_state(self.initial_cash)
        self._save()

    def _from_payload(self, payload: Dict[str, Any]) -> None:
        self.initial_cash = _num(payload.get("initial_cash"), self.initial_cash)
        self._cash = round(_num(payload.get("cash"), self.initial_cash), 2)
        self.slippage_bps = _num(payload.get("slippage_bps"), self.slippage_bps)

        positions: Dict[str, Dict[str, Any]] = {}
        for row in payload.get("positions") or []:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "")
            qty = int(_num(row.get("qty"), 0))
            if not code or qty <= 0:
                continue
            positions[code] = {
                "code": code,
                "name": str(row.get("name") or ""),
                "qty": qty,
                "cost": round(_num(row.get("cost"), 0.0), 4),
                "today": str(row.get("today") or ""),
                "today_buy_qty": int(_num(row.get("today_buy_qty"), 0)),
                "price": _num(row.get("price"), 0.0),
                "prev_close": _num(row.get("prev_close"), 0.0),
            }
        self._positions = positions

        self._orders = []
        for row in payload.get("orders") or []:
            item = _rebuild(Order, row) if isinstance(row, dict) else None
            if item is not None:
                self._orders.append(item)
        self._fills = []
        for row in payload.get("fills") or []:
            item = _rebuild(Fill, row) if isinstance(row, dict) else None
            if item is not None:
                self._fills.append(item)
        self._cash_flow = [dict(row) for row in payload.get("cash_flow") or [] if isinstance(row, dict)]

        self._seq = int(_num(payload.get("seq"), 0))
        self._seq = max(self._seq, len(self._orders))   # 避免重放委托号
        if len(self._orders) > self.max_records:
            self._orders = self._orders[-self.max_records:]
        if len(self._fills) > self.max_records:
            self._fills = self._fills[-self.max_records:]

    def _record_order(self, order: Order) -> Order:
        super()._record_order(order)
        self._save()
        return order

    def _record_fill(self, fill: Fill) -> Fill:
        super()._record_fill(fill)
        self._save()
        return fill

    def reset(self, initial_cash: Optional[float] = None) -> Account:
        """清空账户（持仓/委托/成交/现金流），现金回到 initial_cash（默认构造时的值）。"""
        value = self.initial_cash if initial_cash is None else float(initial_cash)
        if value <= 0:
            raise ValidationError("initial_cash 必须大于 0，当前为 %s" % value, field="initial_cash")
        self._reset_state(value)
        self._cash_flow.append({
            "ts": self.now(), "type": "reset", "amount": self._cash, "balance": self._cash,
            "note": "账户重置，初始资金 %.2f" % self._cash,
        })
        self._save()
        return self.account()

    # ------------------------------------------------------------------ 元信息

    def describe(self) -> Dict[str, Any]:
        data = super().describe()
        data.update({
            "account_path": self.account_path,
            "persistent": bool(self.account_path),
            "initial_cash": self.initial_cash,
            "cash": round(self._cash, 2),
            "slippage_bps": self.slippage_bps,
            "positions_count": len(self._positions),
        })
        return data
