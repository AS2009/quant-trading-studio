# -*- coding: utf-8 -*-
"""交易通道基类：把「委托号生成 / 状态机 / 入参校验 / 流水裁剪」等共用逻辑收敛在一处。

子类只负责：
1. `_do_submit(order)`  —— 撮合与拒单（返回终态 Order，或抛 OrderRejected 由基类转成 rejected）；
2. `positions()` / `account()` —— 账户视图。

另外本模块提供两处**口径唯一**的纯函数，模拟盘与回测应共用，避免两套算法漂移：
- `compute_fee(amount, side, fee)`        —— A 股费用：佣金（双边，最低 5 元）+ 过户费（双边）+ 印花税（仅卖出）
- `apply_slippage(price, side, bps)`      —— 滑点后的成交价（买入上浮、卖出下浮）

约定
----
- 委托状态机：``new -> filled / partial / rejected / cancelled``；终态不可再流转。
- 所有校验错误在 `submit()` 内被捕获并转成 ``status="rejected"`` 的委托返回，
  **不向接口层抛异常**；只有「通道本身不可用」才抛 `BrokerUnavailable`
  （真实券商适配器必须如此，见 adapters.py）。
- 委托与成交流水在内存中保留最近 `max_records` 条（默认 500）并在读取时倒序返回。
"""

import sys
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from ..core import costs
from ..core.errors import BrokerUnavailable, OrderRejected, ValidationError  # noqa: F401
from ..core.models import Account, FeeConfig, Fill, Order, OrderRequest, Position

ORDER_STATUSES = ("new", "filled", "partial", "rejected", "cancelled")
TERMINAL_STATUSES = ("filled", "rejected", "cancelled")
_ALLOWED_TRANSITIONS: Dict[str, tuple] = {
    "new": ("new", "partial", "filled", "rejected", "cancelled"),
    "partial": ("partial", "filled", "rejected", "cancelled"),
    "filled": (),
    "rejected": (),
    "cancelled": (),
}


# --------------------------------------------------------------------------- 工具

def log_warn(message: str) -> None:
    """统一警告输出（账户文件损坏 / 行情不可用等，不抛异常、不中断服务）。"""
    sys.stderr.write("[quantstudio.trading] 警告: %s\n" % message)
    sys.stderr.flush()


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def normalize_code(code) -> str:
    """把用户输入的代码规范化为 ``600519.SH`` 形式；无法识别时抛 ValidationError。

    规则：已带 .SH/.SZ/.BJ 后缀则校验后原样返回；否则按代码段推断交易所
    （60/68/9/5 开头 -> SH，4/8/920 开头 -> BJ，其余 -> SZ）。
    注意 ``000001`` 这种歧义代码（深市股票 / 上证指数）默认按深市股票处理，
    指数需显式写 ``000001.SH``。
    """
    text = str(code or "").strip().upper().replace(" ", "")
    if not text:
        raise ValidationError("标的代码不能为空", field="code")
    if "." in text:
        body, _, market = text.partition(".")
        market = market.strip()
        if not body.isdigit() or len(body) != 6 or market not in ("SH", "SZ", "BJ"):
            raise ValidationError("无法识别的标的代码：%s" % text, field="code")
        return "%s.%s" % (body, market)
    if not text.isdigit() or len(text) != 6:
        raise ValidationError("无法识别的标的代码：%s" % text, field="code")
    if text.startswith("920") or text[0] in ("4", "8"):
        market = "BJ"
    elif text[0] in ("6", "9") or text[0] == "5":
        market = "SH"
    else:
        market = "SZ"
    return "%s.%s" % (text, market)


def compute_fee(amount: float, side: str, fee: Optional[FeeConfig] = None) -> float:
    """A 股单边费用（元）——**直接复用回测口径** :func:`quantstudio.core.costs.fee_total`。

    ``amount`` 为成交额；买入无印花税；含每笔固定流量费 ``flow_fee``；金额 <= 0 时不收费。
    模拟盘与回测（`SimulatedBroker`）因此永远同一套公式，改费率只需改 ``FeeConfig``。
    """
    value = _as_float(amount)
    if value <= 0:
        return 0.0
    return costs.fee_total(side, value, costs.normalize_fee(fee))


def apply_slippage(price: float, side: str, slippage_bps: float) -> float:
    """按**跳数滑点为 0** 的比例滑点算成交价（= :func:`quantstudio.core.costs.exec_price` 的退化情形）。

    需要 tick 滑点（``slippage_ticks``）时请直接调用 ``core.costs.exec_price(side, price, fee)``。
    """
    fee = FeeConfig(slippage_bps=_as_float(slippage_bps), slippage_ticks=0.0)
    return costs.exec_price(side, price, fee)


# --------------------------------------------------------------------------- 基类


class BaseBroker:
    """交易通道基类（`Broker` 协议的结构匹配实现，见 core/interfaces.py）。"""

    name = "base"
    is_live = False                # 真实下单通道才允许为 True
    kind = "base"                  # simulated / live / shadow
    available = False              # 当前进程内是否可直接使用
    description = "交易通道基类（抽象，不应直接实例化）"
    rules: List[str] = []
    order_prefix = "ORD"
    max_records = 500              # 委托/成交流水在内存中的保留条数

    def __init__(self, fee: Optional[FeeConfig] = None, clock: Optional[Callable[[], Any]] = None):
        """`clock` 为可注入的当前时间来源（返回 datetime/date/str），便于测试 T+1。"""
        self.fee = fee if isinstance(fee, FeeConfig) else FeeConfig(**(fee or {}))
        self._clock = clock
        self._orders: List[Order] = []
        self._fills: List[Fill] = []
        self._seq = 0

    # ---- 时间 ----

    def _now_dt(self) -> datetime:
        value = self._clock() if callable(self._clock) else datetime.now()
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, datetime.now().time())
        if isinstance(value, str):
            text = value.strip().replace("/", "-")
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(text[:19], fmt)
                except ValueError:
                    continue
        return datetime.now()

    def now(self) -> str:
        return self._now_dt().strftime("%Y-%m-%d %H:%M:%S")

    def today(self) -> str:
        return self._now_dt().strftime("%Y-%m-%d")

    # ---- 委托号 / 状态机 ----

    def next_order_id(self) -> str:
        """委托号：前缀 + 日期 + 当日序号，如 ``PB20260921000001``。"""
        self._seq += 1
        return "%s%s%06d" % (self.order_prefix, self._now_dt().strftime("%Y%m%d"), self._seq)

    def _transition(self, order: Order, status: str, reason: Optional[str] = None, **updates: Any) -> Order:
        """状态机流转（非法流转抛 ValidationError，属实现缺陷而非用户错误）。"""
        if status not in ORDER_STATUSES:
            raise ValidationError("非法委托状态：%s" % status)
        if status not in _ALLOWED_TRANSITIONS.get(order.status, ()):
            raise ValidationError(
                "非法委托状态流转：%s -> %s（order_id=%s）" % (order.status, status, order.order_id)
            )
        order.status = status
        if reason is not None:
            order.reason = reason
        for key, value in updates.items():
            setattr(order, key, value)
        order.updated_at = self.now()
        return order

    def reject(self, order: Order, reason: str) -> Order:
        """把委托置为 rejected（终态），reason 直接展示给用户。"""
        return self._transition(order, "rejected", reason=reason)

    # ---- 校验 ----

    def assert_side(self, side) -> None:
        """side 只能是 buy / sell。"""
        if str(side or "").strip().lower() not in ("buy", "sell"):
            raise ValidationError("side 只能是 buy 或 sell，当前为 %r" % (side,), field="side")

    def assert_lot(self, qty, lot_size: Optional[int] = None) -> None:
        """qty 必须 > 0 且为 lot_size（默认 100 股 = 1 手）的整数倍。"""
        size = int(lot_size or getattr(self.fee, "lot_size", 100) or 100)
        try:
            value = int(qty)
            exact = float(qty)
        except (TypeError, ValueError):
            raise ValidationError("qty 需为整数，当前为 %r" % (qty,), field="qty")
        if exact != value:
            raise ValidationError("qty 需为整数，当前为 %r" % (qty,), field="qty")
        if value <= 0:
            raise ValidationError("qty 必须大于 0，当前为 %s" % value, field="qty")
        if size > 0 and value % size != 0:
            raise ValidationError(
                "qty 必须为 %d 的整数倍（A 股 1 手 = %d 股），当前为 %s" % (size, size, value), field="qty"
            )

    # ---- 流水 ----

    def _record_order(self, order: Order) -> Order:
        for index, item in enumerate(self._orders):
            if item.order_id == order.order_id:
                self._orders[index] = order
                break
        else:
            self._orders.append(order)
        if len(self._orders) > self.max_records:
            self._orders = self._orders[-self.max_records:]
        return order

    def _record_fill(self, fill: Fill) -> Fill:
        self._fills.append(fill)
        if len(self._fills) > self.max_records:
            self._fills = self._fills[-self.max_records:]
        return fill

    # ---- 对外主流程 ----

    def submit(self, order: OrderRequest) -> Order:
        """提交委托（模板方法）：校验 -> 子类撮合 -> 入流水。

        校验失败/被拒返回 ``status="rejected"`` 的委托，不抛异常；
        `BrokerUnavailable`（通道不可用）会向上抛出。
        """
        req = order if isinstance(order, OrderRequest) else OrderRequest(**dict(order or {}))
        now = self.now()
        created = Order(
            order_id=self.next_order_id(),
            code=str(req.code or "").strip().upper(),
            name="",
            side=str(req.side or "").strip().lower(),
            qty=_as_int(req.qty),
            price=None if req.price is None else _as_float(req.price, 0.0),
            status="new",
            reason=str(req.reason or ""),
            created_at=now,
            updated_at=now,
        )
        try:
            created.code = normalize_code(created.code)
            self.assert_side(created.side)
            self.assert_lot(created.qty)
            if created.price is not None and created.price <= 0:
                raise ValidationError("限价必须大于 0，当前为 %s" % created.price, field="price")
        except ValidationError as exc:
            return self._record_order(self.reject(created, str(exc)))
        try:
            result = self._do_submit(created)
        except (OrderRejected, ValidationError) as exc:
            return self._record_order(self.reject(created, str(exc)))
        return self._record_order(result or created)

    def _do_submit(self, order: Order) -> Order:
        """子类实现：撮合委托并返回其状态（已成交/挂单/拒绝）。"""
        raise NotImplementedError("子类必须实现 _do_submit(order)")

    def cancel(self, order_id: str) -> Order:
        """撤单：仅 new / partial 可撤；终态幂等返回原委托；未知委托抛 ValidationError。"""
        target = str(order_id or "").strip()
        for item in self._orders:
            if item.order_id != target:
                continue
            if item.status in TERMINAL_STATUSES:
                return item
            self._transition(item, "cancelled", reason=item.reason or "用户撤单")
            return self._record_order(item)
        raise ValidationError("未找到委托：%s" % target, field="order_id")

    def orders(self, limit: int = 100) -> List[Order]:
        """委托流水，最新的在前。"""
        items = list(reversed(self._orders))
        return items[: int(limit)] if limit and limit > 0 else items

    def fills(self, limit: int = 100) -> List[Fill]:
        """成交流水，最新的在前。"""
        items = list(reversed(self._fills))
        return items[: int(limit)] if limit and limit > 0 else items

    def positions(self) -> List[Position]:
        raise NotImplementedError("子类必须实现 positions()")

    def account(self) -> Account:
        raise NotImplementedError("子类必须实现 account()")

    # ---- 元信息 ----

    @classmethod
    def describe_static(cls) -> Dict[str, Any]:
        """无需实例化即可获得的通道元信息（供 /api/system/status 使用）。"""
        return {
            "name": cls.name,
            "kind": cls.kind,
            "is_live": bool(cls.is_live),
            "available": bool(cls.available),
            "description": cls.description,
            "rules": list(cls.rules),
        }

    def describe(self) -> Dict[str, Any]:
        data = self.describe_static()
        data["orders_kept"] = len(self._orders)
        data["fills_kept"] = len(self._fills)
        data["fee"] = self.fee.to_dict()
        return data
