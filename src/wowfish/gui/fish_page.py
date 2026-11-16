"""Fishing controls, persistent feedback, bobber preview and bounded logs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from wowfish.gui import image
from wowfish.gui.presenter import Presenter, View
from wowfish.gui.score_graph import ScoreGraph
from wowfish.gui.widgets import wrapped_label
from wowfish.messages import STATE_NAMES, error_text

MAX_LOG_LINES = 1500


class FishPage:
    def __init__(self, parent: Any, presenter: Presenter) -> None:
        from tkinter.scrolledtext import ScrolledText

        import ttkbootstrap as tb

        self.p = presenter
        self.frame = f = tb.Frame(parent, padding=16)
        wrapped_label(
            f, "选择客户端 → 检查游戏设置 → 校准与试听 → 开始钓鱼", bootstyle="info"
        ).pack(fill="x", pady=(0, 10))
        clients = tb.Frame(f)
        clients.pack(fill="x")
        tb.Label(clients, text="游戏客户端").pack(side="left", padx=(0, 8))
        self.client = tb.Combobox(clients, state="readonly", width=48)
        self.client.pack(side="left", fill="x", expand=True)
        self.client.bind("<<ComboboxSelected>>", self._select_client)
        self.refresh = tb.Button(
            clients, text="刷新窗口", command=self.refresh_windows, bootstyle="secondary"
        )
        self.refresh.pack(side="left", padx=(8, 0))
        self.windows: list[Any] = []
        self._title_filter: str | None = None
        self.refresh_windows()

        controls = tb.Frame(f)
        controls.pack(fill="x", pady=(12, 8))
        self.start = tb.Button(
            controls, text="开始钓鱼", command=self._start, bootstyle="success", width=12
        )
        self.start.pack(side="left")
        self.pause = tb.Button(
            controls,
            text="暂停",
            command=self.p.toggle_pause,
            bootstyle="warning",
            width=10,
            state="disabled",
        )
        self.pause.pack(side="left", padx=8)
        self.stop = tb.Button(
            controls,
            text="停止",
            command=self.p.stop,
            bootstyle="danger",
            width=10,
            state="disabled",
        )
        self.stop.pack(side="left")
        self.state = tb.Label(controls, text="待机", bootstyle="info", padding=(12, 4))
        self.state.pack(side="left", padx=8)
        self.hotkeys = wrapped_label(f, bootstyle="secondary")
        self.hotkeys.pack(fill="x")
        self.error = wrapped_label(f, bootstyle="warning")
        self.error.pack(fill="x", pady=(4, 0))
        self.did = wrapped_label(f, font="ForeverHeading.TFont")
        self.did.pack(fill="x", pady=(10, 0))
        self.next = wrapped_label(f)
        self.next.pack(fill="x", pady=3)
        self.stats = wrapped_label(f, presenter.stats, bootstyle="info")
        self.stats.pack(fill="x", pady=(2, 4))
        self.source = wrapped_label(f, "音频来源：开始或试听后显示实际来源", bootstyle="secondary")
        self.source.pack(fill="x", pady=(0, 10))

        middle = tb.Frame(f)
        middle.pack(fill="both", expand=True)
        middle.columnconfigure(0, weight=2, minsize=260)
        middle.columnconfigure(1, weight=3, minsize=350)
        middle.rowconfigure(0, weight=1)
        preview = tb.Labelframe(middle, text="浮漂识别预览", padding=8)
        preview.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.preview = tb.Label(
            preview, text="找到浮漂后显示游戏截图", anchor="center", wraplength=240
        )
        self.preview.pack(fill="both", expand=True)
        self._photo = self._last_png = None
        self._preview_size = (0, 0)
        self.preview.bind("<Configure>", self._resize_preview)
        tabs = tb.Notebook(middle)
        tabs.grid(row=0, column=1, sticky="nsew")
        self.graph = ScoreGraph(tabs)
        self.log = ScrolledText(
            tabs, height=8, width=36, wrap="word", font="TkDefaultFont", state="disabled"
        )
        tabs.add(self.graph.canvas, text="水花得分")
        tabs.add(self.log, text="运行日志")

    def refresh_windows(self) -> None:
        if self.p.running:
            return
        old = (
            self.windows[self.client.current()]
            if 0 <= self.client.current() < len(self.windows)
            else None
        )
        self.windows = self.p.game_windows()
        self._title_filter = self.p.cfg.game.window_title
        values = [
            f"{w.title}  ·  PID {w.pid or '未知'}  ·  {w.width} × {w.height}" for w in self.windows
        ]
        self.client.configure(values=values or ["未找到客户端，请启动游戏后刷新"])
        pick = next(
            (
                i
                for i, w in enumerate(self.windows)
                if old and (w.pid, w.handle) == (old.pid, old.handle)
            ),
            0,
        )
        self.client.current(pick)
        if self.windows:
            self._select_client()

    def _select_client(self, _event: Any = None) -> None:
        index = self.client.current()
        if 0 <= index < len(self.windows):
            try:
                self.p.select_window(self.windows[index])
                self._title_filter = self.p.cfg.game.window_title
            except Exception as e:
                self.p.error = error_text(str(e))

    def _start(self) -> None:
        reasons = self.p.prerequisites(request=True)
        if reasons:
            self.p.error = "；".join(reasons)
            return
        self.p.start()

    def _resize_preview(self, event: Any) -> None:
        size = (max(1, event.width - 8), max(1, event.height - 8))
        if self._last_png is not None and size != self._preview_size:
            self._preview_size = size
            self._photo = image.photo_from_png(
                self._last_png, max_width=size[0], max_height=size[1]
            )
            self.preview.configure(image=self._photo, text="")

    def render(self, view: View) -> None:
        if not view.running and self._title_filter != self.p.cfg.game.window_title:
            self.refresh_windows()
        self.state.configure(text=STATE_NAMES.get(view.state, view.state))
        self.did.configure(text=view.did)
        self.next.configure(text=f"下一步：{view.next}")
        self.stats.configure(text=view.stats)
        self.error.configure(text=view.error[:320])
        self.source.configure(text=f"音频来源：{view.audio_name or '开始或试听后显示实际来源'}")
        g = self.p.cfg.game
        self.hotkeys.configure(
            text=f"暂停 / 继续：{g.pause_hotkey.upper() or '未设置'}    "
            f"停止：{g.stop_hotkey.upper() or '未设置'}"
            "    切出游戏会自动暂停，切回后重新抛竿"
        )
        self.start.configure(state="disabled" if view.running or view.working else "normal")
        self.pause.configure(
            state="normal" if view.running else "disabled",
            text="继续" if self.p.user_paused else "暂停",
        )
        self.stop.configure(state="normal" if view.running else "disabled")
        self.refresh.configure(state="disabled" if view.running or view.working else "normal")
        self.client.configure(state="disabled" if view.running or view.working else "readonly")
        if view.thumbnail_png is not None and view.thumbnail_png is not self._last_png:
            self._last_png = view.thumbnail_png
            self._preview_size = (0, 0)
            self._resize_preview(
                SimpleNamespace(
                    width=self.preview.winfo_width(), height=self.preview.winfo_height()
                )
            )
        self.graph.show(view.trace, self.p.cfg.run.cast_timeout_s)
        if view.new_lines:
            self.log.configure(state="normal")
            self.log.insert("end", "\n".join(view.new_lines) + "\n")
            count = int(self.log.index("end-1c").split(".")[0])
            if count > MAX_LOG_LINES:
                self.log.delete("1.0", f"{count - MAX_LOG_LINES + 1}.0")
            self.log.see("end")
            self.log.configure(state="disabled")
