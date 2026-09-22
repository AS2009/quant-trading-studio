# -*- coding: utf-8 -*-
"""用户真实持仓账户服务（``PortfolioService``）。

两种模式（默认 ``manual``）
--------------------------
- ``manual``：用户在页面/API 手工录入的真实持仓，落盘 ``data/portfolio.json``，
  结构 ``{"mode": "manual", "cash": 12345.67, "holdings": [{"code": "600519.SH", "name": "贵州茅台",
  "qty": 100, "cost": 1500.0, "available_qty": 100}]}``；文件不存在时创建空结构。
- ``paper``：直接读 ``PaperBroker`` 模拟盘账户（同一 ``account_path`` 单例）。

口径
----
- ``holdings()`` 用 provider 实时快照估值；行情取不到时用成本价兜底、``price=0``（不崩、不伪装行情）。
- ``account()``：总资产 = 现金 + 市值；当日盈亏 = Σ持仓 day_pnl（pre_close 计算）；
  manual 模式下累计盈亏 = Σ持仓浮盈浮亏，收益率按成本基线计算。
- ``equity_curve()``：**真实**权益曲线。每次查询把当日真实总资产幂等写入
  ``data/equity_history.json``（``{"2026-09-21": 1234567.89}``），按日期升序返回；
  历史不足请求天数时如实返回已有天数，绝不回溯伪造数据，并在 ``notes`` 里说明。
- 原子写（临时文件 + os.replace）+ 损坏兜底（备份 ``.bak``、打印 warning、重建空结构）。
"""

import json
import os
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..config import get_settings
from ..core.errors import ValidationError
from ..core.models import Account, Position, Quote
from ..trading import get_paper_broker
from ..trading.broker_base import log_warn, normalize_code

MANUAL = "manual"
PAPER = "paper"
MODES = (MANUAL, PAPER)
_EQUITY_FILE = "equity_history.json"


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _now_text(clock=None) -> str:
    value = clock() if callable(clock) else datetime.now()
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d 15:00:00")
    if isinstance(value, str):
        text = value.strip().replace("/", "-")
        return text if len(text) > 10 else (text + " 15:00:00")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today_text(clock=None) -> str:
    return _now_text(clock)[:10]


class PortfolioService:
    """真实持仓账户：查询估值、手工维护、权益快照。"""

    def __init__(self, provider=None, settings=None, clock: Optional[Callable[[], Any]] = None):
        self.provider = provider
        self.settings = settings or get_settings()
        self._clock = clock
        self.path = self.settings.portfolio_path
        self.equity_path = os.path.join(self.settings.data_dir, _EQUITY_FILE)
        self._state = self._default_state()
        self._load()

    # ------------------------------------------------------------------ 基础

    @staticmethod
    def _default_state() -> Dict[str, Any]:
        return {"mode": MANUAL, "cash": 0.0, "holdings": [], "updated_at": ""}

    @property
    def mode(self) -> str:
        return str(self._state.get("mode") or MANUAL)

    def _paper_broker(self):
        return get_paper_broker(settings=self.settings, provider=self.provider)

    def _save(self) -> None:
        self._state["updated_at"] = _now_text(self._clock)
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, ensure_ascii=False, indent=2, allow_nan=False)
            os.replace(tmp, self.path)
        except OSError as exc:
            log_warn("持仓文件落盘失败：%s" % exc)

    def _load(self) -> None:
        path = self.path
        if not path or not os.path.exists(path):
            self._state = self._default_state()
            self._save()          # 文件不存在时创建空结构
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict):
                raise ValueError("持仓文件顶层不是 JSON 对象")
            self._from_payload(payload)
        except Exception as exc:
            log_warn("持仓文件损坏（%s: %s），已备份为 %s.bak 并重新初始化" % (path, exc, path))
            try:
                os.replace(path, path + ".bak")
            except OSError as err:
                log_warn("备份损坏持仓文件失败：%s" % err)
            self._state = self._default_state()
            self._save()

    def _from_payload(self, payload: Dict[str, Any]) -> None:
        mode = str(payload.get("mode") or MANUAL).strip().lower()
        holdings: List[Dict[str, Any]] = []
        for row in payload.get("holdings") or []:
            if not isinstance(row, dict):
                continue
            try:
                code = normalize_code(row.get("code"))
            except ValidationError:
                log_warn("持仓文件含非法代码 %r，已跳过" % row.get("code"))
                continue
            qty = int(_num(row.get("qty"), 0))
            cost = _num(row.get("cost"), 0.0)
            if qty <= 0 or cost <= 0:
                log_warn("持仓文件 %s 的数量/成本非法（qty=%s cost=%s），已跳过" % (code, qty, cost))
                continue
            available = int(_num(row.get("available_qty"), qty))
            holdings.append({
                "code": code,
                "name": str(row.get("name") or ""),
                "qty": qty,
                "cost": round(cost, 4),
                "available_qty": max(0, min(available, qty)),
            })
        self._state = {
            "mode": mode if mode in MODES else MANUAL,
            "cash": round(_num(payload.get("cash"), 0.0), 2),
            "holdings": holdings,
            "updated_at": str(payload.get("updated_at") or ""),
        }

    # ------------------------------------------------------------------ 行情

    def _quotes(self, codes: Sequence[str]) -> Dict[str, Quote]:
        provider = self.provider
        if provider is None or not codes:
            return {}
        try:
            rows = provider.latest_quotes(list(codes)) or []
        except Exception as exc:
            log_warn("获取最新价失败（持仓按成本价兜底）：%s" % exc)
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

    def _name_of(self, code: str) -> str:
        provider = self.provider
        if provider is None:
            return ""
        try:
            return str(provider.resolve_name(code) or "")
        except Exception:
            return ""

    # ------------------------------------------------------------------ 持仓视图

    def _to_position(self, row: Dict[str, Any], quote: Optional[Quote]) -> Position:
        qty = int(row.get("qty") or 0)
        cost = _num(row.get("cost"), 0.0)
        available = max(0, min(int(row.get("available_qty", qty)), qty))
        cost_value = round(cost * qty, 2)
        name = str(row.get("name") or "")
        price = _num(getattr(quote, "price", 0.0), 0.0) if quote is not None else 0.0
        if quote is not None and not name:
            name = str(getattr(quote, "name", "") or "")
        if quote is not None and price > 0:
            market_value = round(price * qty, 2)
            prev_close = _num(getattr(quote, "prev_close", 0.0), 0.0)
            total_pnl = round(market_value - cost_value, 2)
            return Position(
                code=str(row.get("code") or ""),
                name=name or self._name_of(str(row.get("code") or "")),
                qty=qty,
                available_qty=available,
                cost=round(cost, 4),
                price=price,
                market_value=market_value,
                cost_value=cost_value,
                day_pnl=round((price - prev_close) * qty, 2) if prev_close > 0 else 0.0,
                total_pnl=total_pnl,
                return_pct=round(total_pnl / cost_value * 100, 2) if cost_value else 0.0,
            )
        # 行情缺失：用成本价兜底估值，price=0 明示「无行情」，不做任何伪装
        return Position(
            code=str(row.get("code") or ""),
            name=name or self._name_of(str(row.get("code") or "")),
            qty=qty,
            available_qty=available,
            cost=round(cost, 4),
            price=0.0,
            market_value=cost_value,
            cost_value=cost_value,
            day_pnl=0.0,
            total_pnl=0.0,
            return_pct=0.0,
        )

    def _holdings_manual(self) -> List[Position]:
        rows = [row for row in (self._state.get("holdings") or []) if isinstance(row, dict)]
        codes = [str(row.get("code") or "") for row in rows]
        quotes = self._quotes(codes)
        out = [self._to_position(row, quotes.get(str(row.get("code") or ""))) for row in rows]
        out.sort(key=lambda item: item.market_value, reverse=True)
        return out

    def holdings(self) -> List[Position]:
        """持仓列表：manual 模式读持仓文件并实时估值；paper 模式读模拟盘账户。"""
        if self.mode == PAPER:
            return self._paper_broker().positions()
        return self._holdings_manual()

    # ------------------------------------------------------------------ 账户视图

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
        """账户总览：总资产 = 现金 + 市值（勾稽一致），含当日/累计盈亏与 allocation。"""
        if self.mode == PAPER:
            return self._paper_broker().account()
        positions = self._holdings_manual()
        cash = round(_num(self._state.get("cash"), 0.0), 2)
        market_value = round(sum(item.market_value for item in positions), 2)
        total = round(cash + market_value, 2)
        day_pnl = round(sum(item.day_pnl for item in positions), 2)
        total_pnl = round(sum(item.total_pnl for item in positions), 2)
        cost_basis = round(total - total_pnl, 2)
        return Account(
            total_assets=total,
            market_value=market_value,
            cash=cash,
            available_cash=cash,
            frozen_cash=0.0,
            day_pnl=day_pnl,
            total_pnl=total_pnl,
            total_return_pct=round(total_pnl / cost_basis * 100, 2) if cost_basis > 0 else 0.0,
            positions_count=len([item for item in positions if item.qty > 0]),
            allocation=self._allocation(cash, positions, total),
            as_of=_now_text(self._clock),
        )

    # ------------------------------------------------------------------ 维护

    @staticmethod
    def _as_int(value, field: str) -> int:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValidationError("%s 需为整数，当前为 %r" % (field, value), field=field)
        if number != int(number):
            raise ValidationError("%s 需为整数，当前为 %r" % (field, value), field=field)
        return int(number)

    @staticmethod
    def _as_float(value, field: str) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValidationError("%s 需为数字，当前为 %r" % (field, value), field=field)

    def _find(self, code: str) -> Optional[Dict[str, Any]]:
        for row in self._state.get("holdings") or []:
            if row.get("code") == code:
                return row
        return None

    def upsert_holding(self, payload: Dict[str, Any]) -> Position:
        """新增/更新一条真实持仓（按 code upsert）。校验失败抛 ValidationError。"""
        data = dict(payload or {})
        code = normalize_code(data.get("code"))
        existing = self._find(code)

        if data.get("qty") in (None, ""):
            raise ValidationError("qty 不能为空", field="qty")
        qty = self._as_int(data.get("qty"), "qty")
        if qty <= 0:
            raise ValidationError("qty 必须大于 0，当前为 %s" % qty, field="qty")

        if data.get("cost") not in (None, ""):
            cost = self._as_float(data.get("cost"), "cost")
            if cost <= 0:
                raise ValidationError("cost 必须大于 0，当前为 %s" % cost, field="cost")
        elif existing is not None and _num(existing.get("cost")) > 0:
            cost = _num(existing.get("cost"))
        else:
            raise ValidationError("cost 必须大于 0（新建持仓必须提供成本价）", field="cost")

        if data.get("available_qty") not in (None, ""):
            available = self._as_int(data.get("available_qty"), "available_qty")
            if available < 0 or available > qty:
                raise ValidationError(
                    "available_qty 需在 0..qty 之间，当前为 %s（qty=%s）" % (available, qty),
                    field="available_qty",
                )
        elif existing is not None:
            available = max(0, min(int(existing.get("available_qty", qty)), qty))
        else:
            available = qty

        name = str(data.get("name") or "").strip()
        if not name:
            name = str((existing or {}).get("name") or "") or self._name_of(code)
        row = {
            "code": code,
            "name": name,
            "qty": qty,
            "cost": round(cost, 4),
            "available_qty": available,
        }
        if existing is None:
            self._state.setdefault("holdings", []).append(row)
        else:
            existing.update(row)
        self._save()
        return self._to_position(row, self._quotes([code]).get(code))

    def delete_holding(self, code: str) -> bool:
        """删除一条持仓；不存在时抛 ValidationError。"""
        target = normalize_code(code)
        rows = self._state.get("holdings") or []
        remain = [row for row in rows if row.get("code") != target]
        if len(remain) == len(rows):
            raise ValidationError("未找到持仓：%s" % target, field="code")
        self._state["holdings"] = remain
        self._save()
        return True

    def set_cash(self, amount) -> float:
        """设置账户可用现金（>= 0）。"""
        value = self._as_float(amount, "cash")
        if value < 0:
            raise ValidationError("cash 不能为负数，当前为 %s" % value, field="cash")
        self._state["cash"] = round(value, 2)
        self._save()
        return self._state["cash"]

    def set_mode(self, mode: str) -> str:
        """切换数据来源：manual（手工持仓文件）/ paper（模拟盘账户）。"""
        text = str(mode or "").strip().lower()
        if text not in MODES:
            raise ValidationError("mode 只支持 manual / paper，当前为 %r" % (mode,), field="mode")
        self._state["mode"] = text
        self._save()
        return text

    # ------------------------------------------------------------------ 权益曲线

    def _load_equity(self) -> Dict[str, float]:
        path = self.equity_path
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict):
                raise ValueError("权益文件顶层不是 JSON 对象")
            history: Dict[str, float] = {}
            for key, value in payload.items():
                text = str(key)[:10]
                if len(text) == 10 and text[4] == "-" and text[7] == "-":
                    history[text] = round(_num(value), 2)
            return history
        except Exception as exc:
            log_warn("权益历史文件损坏（%s: %s），已备份为 %s.bak 并重置" % (path, exc, path))
            try:
                os.replace(path, path + ".bak")
            except OSError as err:
                log_warn("备份损坏权益文件失败：%s" % err)
            return {}

    def _save_equity(self, history: Dict[str, float]) -> None:
        try:
            directory = os.path.dirname(self.equity_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = self.equity_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(history, fh, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True)
            os.replace(tmp, self.equity_path)
        except OSError as exc:
            log_warn("权益历史落盘失败：%s" % exc)

    def snapshot_equity(self, day: Optional[str] = None) -> Dict[str, Any]:
        """把当日真实总资产写入权益历史（同一日幂等覆盖）。"""
        target = str(day or _today_text(self._clock))[:10]
        value = round(float(self.account().total_assets), 2)
        history = self._load_equity()
        history[target] = value
        self._save_equity(history)
        return {"date": target, "total_assets": value, "days": len(history)}

    def equity_curve(self, days: int = 90) -> Dict[str, Any]:
        """真实权益曲线：先幂等记录当日快照，再按日期升序返回最近 ``days`` 天。"""
        self.snapshot_equity()
        history = self._load_equity()
        rows = sorted((day, value) for day, value in history.items())
        limit = int(days or 0)
        picked = rows[-limit:] if limit > 0 else rows
        notes: List[str] = []
        if limit > 0 and len(rows) < limit:
            notes.append(
                "权益快照仅 %d 个交易日，不足请求的 %d 天；这里只如实返回已有快照，不回溯伪造历史。"
                % (len(rows), limit)
            )
        notes.append("每个自然日的账户总资产在查询时幂等落盘一次（%s）。" % _EQUITY_FILE)
        return {
            "items": [{"date": day, "total_assets": value} for day, value in picked],
            "count": len(picked),
            "total_days": len(rows),
            "days": limit,
            "start": picked[0][0] if picked else "",
            "end": picked[-1][0] if picked else "",
            "notes": notes,
            "path": self.equity_path,
        }

    # ------------------------------------------------------------------ 元信息

    def describe(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "path": self.path,
            "equity_path": self.equity_path,
            "cash": round(_num(self._state.get("cash"), 0.0), 2),
            "holdings_count": len(self._state.get("holdings") or []),
            "provider": str(getattr(self.provider, "name", "") or ""),
        }
