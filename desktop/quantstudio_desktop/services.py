# -*- coding: utf-8 -*-
"""服务桥接层：把 ``quantstudio`` 服务层的同步能力包装成「后台线程 + 主线程回调」。

两件事
------
1. ``TaskRunner``：ThreadPoolExecutor + queue，负责把耗时操作（行情抓取、回测）放到后台线程，
   结果/异常统一回到 Tk 主线程执行回调，避免「界面卡死」和无锁并发写控件；
2. ``GuiServices``：面向界面的瘦封装（惰性构造服务层，方法名与 Web API 语义一一对应），
   返回值都是纯 dict/list，便于界面直接渲染，也便于在没有 Tk 的环境下单元测试。
"""

import itertools
import queue
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_TIMEOUT_HINT = 30.0        # 超过这个秒数的任务会在状态栏提示（不中断，只是提示）


class TaskRunner:
    """把阻塞调用搬到后台线程，回调在主线程执行。"""

    def __init__(self, root, max_workers: int = 3, poll_ms: int = 40):
        self._root = root
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="qs-task")
        self._queue: "queue.Queue[Tuple[int, str, bool, Any]]" = queue.Queue()
        self._counter = itertools.count(1)
        self._busy = 0
        self._lock = threading.Lock()
        self._poll_ms = poll_ms
        self._poll_job: Optional[str] = None
        self._closed = False
        self._pending: Dict[int, Tuple[Optional[Callable], Optional[Callable]]] = {}
        self.on_busy_change: Optional[Callable[[int], None]] = None
        self.on_error: Optional[Callable[[BaseException], None]] = None   # 未提供 on_error 时的兜底
        # 注意：这里**不**启动轮询。只有真的有任务在飞时才挂 40ms 定时器，
        # 否则空闲时也会 25 次/秒唤醒主线程（且任何 `update()` 都会因定时器不断到期而永不返回）。

    # ------------------------------------------------------------------ 对外
    def run(self, fn: Callable[..., Any], *args, on_done: Optional[Callable[[Any], None]] = None,
            on_error: Optional[Callable[[BaseException], None]] = None, name: str = "", **kwargs) -> int:
        """提交后台任务，返回任务 id。``on_done``/``on_error`` 保证在主线程被调用。"""
        task_id = next(self._counter)
        with self._lock:
            self._busy += 1
        self._pending[task_id] = (on_done, on_error)     # 先登记：避免「任务已完成、回执先到」时丢回调
        self._notify_busy()
        self._schedule()

        def worker():
            started = time.time()
            try:
                result = fn(*args, **kwargs)
                ok, payload = True, result
            except BaseException as exc:                       # noqa: BLE001 - 后台异常必须带回主线程
                ok, payload = False, exc
                if not ok:
                    traceback.print_exc()
            self._queue.put((task_id, name or getattr(fn, "__name__", "task"), ok, payload,
                             time.time() - started))

        self._pool.submit(worker)
        return task_id

    # ------------------------------------------------------------------ 内部
    def _schedule(self) -> None:
        """只有还有在飞任务时保持轮询；空闲时彻底停止（不占用主线程）。"""
        if self._closed or self._poll_job is not None or not self._pending:
            return
        try:
            self._poll_job = self._root.after(self._poll_ms, self._drain)
        except Exception:  # noqa: BLE001 - 窗口销毁后不再调度
            self._closed = True

    def _drain(self) -> None:
        self._poll_job = None
        while True:
            try:
                task_id, name, ok, payload, elapsed = self._queue.get_nowait()
            except queue.Empty:
                break
            callbacks = self._pending.pop(task_id, (None, None))
            on_done, on_error = callbacks
            with self._lock:
                self._busy = max(0, self._busy - 1)
            try:
                if ok:
                    if on_done is not None:
                        on_done(payload)
                else:
                    if on_error is not None:
                        on_error(payload)
                    elif self.on_error is not None:
                        self.on_error(payload)
            except Exception:  # noqa: BLE001 - 回调异常不应中断队列处理
                traceback.print_exc()
        self._notify_busy()
        self._schedule()          # 仍有任务在飞则续挂定时器，否则不再调度

    def _notify_busy(self) -> None:
        if self.on_busy_change is not None:
            try:
                self.on_busy_change(self.busy)
            except Exception:  # noqa: BLE001
                pass

    @property
    def busy(self) -> int:
        with self._lock:
            return self._busy

    def shutdown(self) -> None:
        self._closed = True
        if self._poll_job is not None:
            try:
                self._root.after_cancel(self._poll_job)
            except Exception:  # noqa: BLE001
                pass
            self._poll_job = None
        self._pool.shutdown(wait=False)


class GuiServices:
    """界面用的服务封装：所有方法都是同步阻塞调用，请配合 ``TaskRunner`` 使用。"""

    def __init__(self, settings: Any = None):
        self._settings = settings
        self._services = None
        self.last_notes: List[str] = []

    # ------------------------------------------------------------------ 惰性构造
    @property
    def settings(self) -> Any:
        from quantstudio.config import get_settings

        return self._settings or get_settings()

    @property
    def services(self) -> Any:
        if self._services is None:
            from quantstudio.services import get_services

            self._services = get_services(self._settings) if self._settings is not None else get_services()
        return self._services

    # ------------------------------------------------------------------ 系统
    def system_status(self) -> Dict[str, Any]:
        return self.services.market.system_status()

    def provider_meta(self) -> Dict[str, Any]:
        try:
            return self.services.market.meta().to_dict()
        except Exception as exc:  # noqa: BLE001
            return {"source": "", "stale": False, "offline": True, "as_of": "", "notes": [str(exc)]}

    # ------------------------------------------------------------------ 行情
    def overview(self) -> Dict[str, Any]:
        return self.services.market.overview()

    def sectors(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        return self.services.market.sectors(limit)

    def quotes(self, codes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        return self.services.market.quotes(codes)

    def kline(self, code: str, days: int = 250, freq: str = "day", adjust: str = "qfq") -> List[Dict[str, Any]]:
        return self.services.market.kline(code, days=days, freq=freq, adjust=adjust)

    def watchlist(self) -> List[str]:
        return self.services.market.watchlist()

    def watchlist_quotes(self) -> Dict[str, Any]:
        return self.services.market.watchlist_quotes()

    def add_watchlist(self, code: str) -> List[str]:
        return self.services.market.add_to_watchlist(code)

    def remove_watchlist(self, code: str) -> List[str]:
        return self.services.market.remove_from_watchlist(code)

    # ------------------------------------------------------------------ 策略
    def strategies(self) -> List[Dict[str, Any]]:
        return self.services.strategies.list_strategies()

    def strategy(self, strategy_id: str) -> Dict[str, Any]:
        return self.services.strategies.get_strategy(strategy_id)

    def create_strategy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.services.strategies.create_strategy(payload)

    def delete_strategy(self, strategy_id: str) -> Dict[str, Any]:
        return self.services.strategies.delete_strategy(strategy_id)

    # ------------------------------------------------------------------ 回测
    def run_backtest(self, strategy_id: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.services.backtest.run(strategy_id, params or {})

    def backtest_cache_info(self) -> Dict[str, Any]:
        return self.services.backtest.cache_info()

    def clear_backtest_cache(self) -> Dict[str, Any]:
        return self.services.backtest.clear_cache()

    # ------------------------------------------------------------------ 账户与交易
    def portfolio_overview(self) -> Dict[str, Any]:
        return self.services.portfolio.overview()

    def holdings(self) -> List[Dict[str, Any]]:
        return self.services.portfolio.holdings()

    def equity(self, days: int = 90) -> List[Dict[str, Any]]:
        points, notes = self.services.portfolio.equity_curve(days=days)
        self.last_notes = list(notes or [])
        return points

    def portfolio_mode(self) -> str:
        return self.services.portfolio.mode()

    def set_portfolio_mode(self, mode: str) -> Dict[str, str]:
        return self.services.portfolio.set_mode(mode)

    def upsert_holding(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.services.portfolio.upsert_holding(payload)

    def delete_holding(self, code: str) -> Dict[str, Any]:
        return self.services.portfolio.delete_holding(code)

    def set_cash(self, amount: Any) -> Dict[str, Any]:
        return self.services.portfolio.set_cash(amount)

    def orders(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.services.portfolio.orders(limit=limit)

    def fills(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.services.portfolio.fills(limit=limit)

    def submit_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.services.portfolio.submit_order(payload)

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        return self.services.portfolio.cancel_order(order_id)

    def reset_paper(self, initial_cash: Any = None) -> Dict[str, Any]:
        return self.services.portfolio.reset(initial_cash)

    # ------------------------------------------------------------------ 便捷
    def data_source_summary(self) -> Dict[str, Any]:
        """给状态栏用的一句话摘要（不抛异常）。"""
        try:
            status = self.system_status()
        except Exception as exc:  # noqa: BLE001
            return {"text": "数据源不可用：%s" % exc, "mode": "error", "offline": True}
        mode = status.get("mode") or "unknown"
        provider = (status.get("provider") or {})
        sources = " / ".join(provider.get("sources") or [])
        if mode == "real":
            text = "真实行情 · %s" % (provider.get("active") or sources)
        elif mode == "cache":
            text = "缓存数据（可能滞后）"
        else:
            text = "离线 / 演示数据（不可用于交易决策）"
        return {
            "text": text,
            "mode": mode,
            "offline": bool(status.get("offline")),
            "as_of": status.get("as_of") or "",
            "sources": sources,
            "strategies": status.get("strategies") or {},
        }
