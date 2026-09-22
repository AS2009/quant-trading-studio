# -*- coding: utf-8 -*-
"""轻提示：在指定宿主容器里堆叠显示消息，自动消失（``timeout=0`` 表示常驻，需手动关闭）。"""

import itertools
import tkinter as tk
from typing import Optional

from .. import theme

ICONS = {"ok": "✓", "error": "✕", "warn": "!", "info": "i"}


class Toast:
    """提示管理器（线程约束：只在主线程调用 ``show``）。"""

    def __init__(self, master: tk.Misc, host: Optional[tk.Misc] = None, max_items: int = 3):
        self.master = master
        self.host = host or master
        self.max_items = max_items
        self._items = []          # [(frame, after_id)]
        self._seq = itertools.count(1)

    def show(self, text: str, kind: str = "info", timeout: int = 4200) -> tk.Frame:
        frame = tk.Frame(self.host, bg=theme.COLORS["panel"], padx=1, pady=1,
                         highlightthickness=1, highlightbackground=theme.COLORS["line"])
        color = {"ok": theme.COLORS["down"], "error": theme.COLORS["up"],
                 "warn": theme.COLORS["warn"]}.get(kind, theme.COLORS["brand"])
        left = tk.Frame(frame, bg=color, width=3)
        left.pack(side="left", fill="y")
        body = tk.Frame(frame, bg=theme.COLORS["panel"])
        body.pack(side="left", fill="both", expand=True)
        tk.Label(body, text="%s  %s" % (ICONS.get(kind, "i"), text), bg=theme.COLORS["panel"],
                 fg=theme.COLORS["text"], font=theme.font(9), justify="left",
                 wraplength=420, padx=8, pady=5).pack(side="left", fill="x", expand=True)
        if timeout <= 0:
            close = tk.Label(body, text="✕", bg=theme.COLORS["panel"], fg=theme.COLORS["text2"],
                             font=theme.font(8), padx=6, cursor="hand2")
            close.pack(side="right")
            close.bind("<Button-1>", lambda _e, f=frame: self._dismiss(f))
        frame.pack(side="bottom", anchor="e", pady=(2, 0))
        after_id = None
        if timeout > 0:
            after_id = self.master.after(timeout, lambda f=frame: self._dismiss(f))
        self._items.append((frame, after_id))
        while len(self._items) > self.max_items:
            old_frame, old_after = self._items.pop(0)
            if old_after:
                try:
                    self.master.after_cancel(old_after)
                except Exception:  # noqa: BLE001
                    pass
            old_frame.destroy()
        return frame

    def _dismiss(self, frame: tk.Frame) -> None:
        for index, (item, after_id) in enumerate(list(self._items)):
            if item is frame:
                if after_id:
                    try:
                        self.master.after_cancel(after_id)
                    except Exception:  # noqa: BLE001
                        pass
                self._items.pop(index)
                break
        try:
            frame.destroy()
        except Exception:  # noqa: BLE001
            pass

    def clear(self) -> None:
        for frame, after_id in list(self._items):
            if after_id:
                try:
                    self.master.after_cancel(after_id)
                except Exception:  # noqa: BLE001
                    pass
            try:
                frame.destroy()
            except Exception:  # noqa: BLE001
                pass
        self._items = []
