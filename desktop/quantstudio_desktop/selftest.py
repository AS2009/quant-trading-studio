# -*- coding: utf-8 -*-
"""自检模式：给 CI 与用户排障用（不依赖显示器，除 ``run_gui_selftest`` 外）。

- ``--selftest``：核心链路（数据源 → 策略 → 回测 → 报告）在真实环境下跑一遍；
- ``--selftest-gui``：构建主窗口与全部页面后立即销毁，验证 UI 代码与打包产物可运行。
"""

import os
import sys
import time
import traceback
from typing import Any, Dict, List, Tuple


def _line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def report_path() -> str:
    """自检报告文件路径（无控制台的 GUI 程序靠它把结果交给 CI/用户）。"""
    import tempfile

    return os.path.join(tempfile.gettempdir(), "quantstudio_selftest.txt")


class _ReportTee:
    """把 stdout 同时写到报告文件：``--windowed`` 构建下 stdout 可能是 devnull。"""

    def __init__(self, stream, path: str):
        self._stream = stream
        self._path = path
        try:
            self._fh = open(path, "w", encoding="utf-8")
        except OSError:
            self._fh = None

    def write(self, text: str) -> int:
        if self._stream is not None:
            try:
                self._stream.write(text)
            except Exception:  # noqa: BLE001
                pass
        if self._fh is not None:
            try:
                self._fh.write(text)
                self._fh.flush()
            except Exception:  # noqa: BLE001
                pass
        return len(text)

    def flush(self) -> None:
        for handle in (self._stream, self._fh):
            try:
                if handle is not None:
                    handle.flush()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass


def _run_with_report(target) -> int:
    """执行自检函数并把输出同时写进报告文件。"""
    original = sys.stdout
    tee = _ReportTee(original, report_path())
    sys.stdout = tee                      # type: ignore[assignment]
    try:
        code = target()
    finally:
        sys.stdout = original             # type: ignore[assignment]
        tee.write("\n[报告文件] %s\n" % report_path())
        tee.close()
    return code


_PUMP_SECONDS = 1.5        # 单页最多“消化事件”的秒数：让界面稳定下来，又绝不让 CI 挂死


def _pump(widget, seconds: float = _PUMP_SECONDS):
    """有界事件泵：在 ``seconds`` 秒内把待处理事件 **尽数处理**，然后一定返回。

    为什么不用 ``tkinter.Misc.update()``：只要还有定时器到期它就继续处理，
    遇到自我重排的定时器（后台任务轮询、ttk 进度条动画都是这类）会**永不返回**。
    这里用 ``dooneevent(DONT_WAIT)``（不阻塞）＋时间上限。

    返回 ``(处理事件数, 用时秒, 队列是否仍未排空)``；第三个为真才值得怀疑自激循环。
    """
    import time as _time

    try:
        import tkinter as tk

        flags = tk._tkinter.DONT_WAIT | tk._tkinter.ALL_EVENTS       # type: ignore[attr-defined]
    except Exception:                                                # noqa: BLE001
        flags = 6                                                    # Tcl: DONT_WAIT|ALL_EVENTS
    done = 0
    started = _time.time()
    while _time.time() - started < seconds:
        try:
            if not widget.tk.dooneevent(flags):
                break
        except Exception:                                            # noqa: BLE001 - 窗口已销毁
            break
        done += 1
    elapsed = _time.time() - started
    return done, elapsed, elapsed >= seconds


def run_selftest(symbol: str = "600519.SH") -> int:
    return _run_with_report(lambda: _run_selftest(symbol))


def _run_selftest(symbol: str = "600519.SH") -> int:
    from . import boot

    info = boot.prepare()
    failures: List[str] = []
    warnings: List[str] = []

    _line("QuantTrading Studio 桌面版 · 自检")
    print("打包运行   :", "是" if info["frozen"] else "否（源码运行）")
    print("Python     :", info["python"])
    print("核心包目录 :", info["core_dir"])
    print("数据目录   :", info["data_dir"])

    try:
        import quantstudio

        print("核心版本   :", quantstudio.__version__)
    except Exception as exc:  # noqa: BLE001
        failures.append("无法导入核心包 quantstudio：%s" % exc)

    _line("1) 数据源")
    provider = None
    try:
        from quantstudio.data import get_provider

        started = time.time()
        provider = get_provider()
        print("Provider   :", provider.name)
        quotes = provider.latest_quotes([symbol])
        for quote in quotes:
            print("  快照 %-11s %-8s %10.2f  %+6.2f%%  来源=%s" % (
                quote.code, quote.name, quote.price, quote.change_pct, quote.source))
        bars = provider.kline(symbol, days=30)
        print("  K线 %d 根，末根 %s 收 %.2f（来源 %s）" % (
            len(bars), bars[-1].date, bars[-1].close, getattr(provider.last_meta, "source", "?")))
        if bars[-1].close <= 0:
            failures.append("K 线收盘价异常")
        if getattr(provider.last_meta, "offline", False):
            warnings.append("当前为离线/演示数据（%s）" % getattr(provider.last_meta, "source", "?"))
        print("  用时 %.2fs" % (time.time() - started))
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        failures.append("数据源访问失败：%s" % exc)

    _line("2) 策略库")
    strategy_id = "st_ma_cross"
    try:
        from quantstudio.strategies import list_specs, local_status

        specs = list_specs()
        for spec in specs:
            print("  %-20s %-16s origin=%s" % (spec.id, spec.name, spec.origin))
        local = local_status()
        print("本地策略目录:", local.get("dir"))
        print("已加载      :", ", ".join(local.get("loaded") or []) or "（无）")
        for item in local.get("errors") or []:
            failures.append("本地策略加载失败 %s：%s" % (item.get("file"), item.get("error")))
        if not specs:
            failures.append("策略库为空")
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        failures.append("策略库不可用：%s" % exc)

    _line("3) 回测引擎")
    try:
        from quantstudio.backtest.engine import BacktestEngine
        from quantstudio.core.models import BacktestRequest
        from quantstudio.strategies import create

        request = BacktestRequest(strategy_id=strategy_id, symbols=[symbol],
                                  start="2024-01-01", end="", initial_cash=1_000_000.0)
        started = time.time()
        result = BacktestEngine(provider=provider).run(request, create(strategy_id, symbols=[symbol]))
        metrics = result.metrics
        print("策略        :", strategy_id, "| 交易日", metrics.trading_days)
        print("累计收益    : %+.2f%%  年化 %+.2f%%  最大回撤 %.2f%%" % (
            metrics.total_return_pct, metrics.annual_return_pct, metrics.max_drawdown_pct))
        print("夏普        : %.2f  交易 %d 次  费用 %.2f 元" % (
            metrics.sharpe, metrics.trade_count, metrics.total_fee))
        print("净值点      :", len(result.nav), "| 用时 %.2fs" % (time.time() - started))
        if metrics.trading_days <= 0:
            failures.append("回测没有产生任何交易日")
        if not result.nav:
            failures.append("回测净值为空")
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        failures.append("回测失败：%s" % exc)

    _line("4) 界面依赖")
    try:
        import tkinter  # noqa: F401
        from tkinter import ttk

        print("tkinter     :", "可用")
        try:
            root = tkinter.Tk()
            root.withdraw()
            print("Tk 版本     :", root.tk.call("info", "patchlevel"))
            print("ttk 主题    :", ", ".join(ttk.Style(root).theme_names()))
            root.destroy()
        except Exception as exc:  # noqa: BLE001
            print("Tk 初始化失败（无显示器环境属正常）:", exc)
    except Exception as exc:  # noqa: BLE001
        failures.append("缺少 tkinter：%s" % exc)

    _line("自检结果")
    for item in warnings:
        print("  警告:", item)
    for item in failures:
        print("  失败:", item)
    if failures:
        print("\n自检未通过（%d 项失败）" % len(failures))
        return 1
    print("\n自检通过 ✅" + ("（含警告 %d 条）" % len(warnings) if warnings else ""))
    return 0


def run_gui_selftest() -> int:
    return _run_with_report(_run_gui_selftest)


def _run_gui_selftest() -> int:
    """构建主窗口 + 全部页面，确认 UI 代码与打包产物可用。"""
    from . import boot

    boot.prepare()
    try:
        import tkinter as tk
    except Exception as exc:  # noqa: BLE001
        print("缺少 tkinter：%s" % exc)
        return 1

    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001
        if sys.platform == "win32":
            print("Windows 上 Tk 初始化失败：%s" % exc)
            return 1
        print("跳过 GUI 自检（无显示器）：%s" % exc)
        return 0

    from .app import DesktopApp
    from .views import VIEW_SPECS

    failures: List[str] = []
    warnings: List[str] = []
    notes: List[str] = []
    app = None
    try:
        app = DesktopApp()
        app.withdraw()                       # 不弹窗，仅构建
        for key, title, _module, _class in VIEW_SPECS:
            try:
                app.select_view(key)
                app.update_idletasks()
                events, elapsed, still_busy = _pump(app)
                print("  页面 OK  : %-12s %s（事件 %d 个 / %.2fs）" % (key, title, events, elapsed))
                if still_busy:
                    notes.append("页面 %s：%.1fs 内仍有事件未处理完（%d 个，通常是数据渲染中）"
                                 % (key, _PUMP_SECONDS, events))
            except Exception as exc:         # noqa: BLE001
                traceback.print_exc()
                failures.append("页面 %s（%s）构建失败：%s" % (key, title, exc))
        app.update_idletasks()
        print("  窗口尺寸  :", app.winfo_reqwidth(), "x", app.winfo_reqheight())
    except Exception as exc:                 # noqa: BLE001
        traceback.print_exc()
        failures.append("主窗口构建失败：%s" % exc)
    finally:
        try:
            if app is not None:
                app.on_close()
        except Exception:                    # noqa: BLE001
            pass

    for item in warnings:
        print("  警告:", item)
    for item in notes:
        print("  备注:", item)
    if failures:
        print("\nGUI 自检未通过：")
        for item in failures:
            print("  -", item)
        return 1
    print("\nGUI 自检通过 ✅" + ("（含警告 %d 条）" % len(warnings) if warnings else ""))
    return 0
