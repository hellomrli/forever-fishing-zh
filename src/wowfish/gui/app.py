"""The Chinese desktop window and its main-thread event loop."""

from __future__ import annotations

import gc
import sys
from typing import Any

from wowfish import APP_TITLE, __version__
from wowfish.gui.diagnostics_page import DiagnosticsPage
from wowfish.gui.fish_page import FishPage
from wowfish.gui.hotkeys import HotkeyManager
from wowfish.gui.presenter import Presenter
from wowfish.gui.setup_page import SetupPage
from wowfish.messages import error_text

POLL_MS = 50


def configure_fonts(root: Any) -> str:
    import tkinter.font as tkfont

    available = set(tkfont.families(root))
    family = next(
        (
            name
            for name in (
                "Microsoft YaHei UI",
                "Microsoft YaHei",
                "Noto Sans CJK SC",
                "WenQuanYi Micro Hei",
            )
            if name in available
        ),
        tkfont.nametofont("TkDefaultFont").actual("family"),
    )
    for name in (
        "TkDefaultFont",
        "TkTextFont",
        "TkMenuFont",
        "TkHeadingFont",
        "TkCaptionFont",
        "TkSmallCaptionFont",
        "TkIconFont",
        "TkTooltipFont",
    ):
        tkfont.nametofont(name).configure(family=family, size=10)
    root.tk.call(
        "font", "create", "ForeverHeading.TFont", "-family", family, "-size", 13, "-weight", "bold"
    )
    return family


class App:
    def __init__(self, presenter: Presenter | None = None) -> None:
        import ttkbootstrap as tb

        from wowfish.capture._win32 import set_dpi_aware

        set_dpi_aware()
        self.p = presenter or Presenter()
        self.root = tb.Window(
            themename="bootstrap-dark",
            title=f"{APP_TITLE} {__version__}",
            size=(1080, 780),
            minsize=(900, 640),
        )
        self.font_family = configure_fonts(self.root)
        self.root.report_callback_exception = self._callback_error
        self.notebook = tb.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)
        self.fish = FishPage(self.notebook, self.p)
        self.setup = SetupPage(self.notebook, self.p)
        self.diagnostics = DiagnosticsPage(self.notebook, self.p)
        self.notebook.add(self.fish.frame, text="钓鱼")
        self.notebook.add(self.setup.frame, text="设置")
        self.notebook.add(self.diagnostics.frame, text="校准与诊断")
        self.hotkeys = HotkeyManager(self.p.toggle_pause, self.p.stop)
        self._local_keys: tuple[str, str] | None = None
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._after = self.root.after(POLL_MS, self._tick)
        self.tick()

    def _callback_error(self, _kind: Any, error: Exception, _traceback: Any) -> None:
        from loguru import logger

        self.p.error = error_text(str(error))
        logger.opt(exception=(_kind, error, _traceback)).error("界面操作失败")

    def _sync_hotkeys(self) -> None:
        g = self.p.cfg.game
        problem = self.hotkeys.sync(g.pause_hotkey, g.stop_hotkey)
        if problem:
            self.p.error = problem
        if sys.platform != "win32":
            current = (g.pause_hotkey, g.stop_hotkey)
            if current != self._local_keys:
                for name in self._local_keys or ():
                    if name:
                        self.root.unbind(f"<{name.upper()}>")
                for name, command in zip(current, (self.p.toggle_pause, self.p.stop), strict=True):
                    if name:
                        self.root.bind(f"<{name.upper()}>", lambda _e, action=command: action())
                self._local_keys = current

    def tick(self) -> None:
        self._sync_hotkeys()
        self.hotkeys.poll()
        view = self.p.poll()
        self.fish.render(view)
        self.setup.render(view)
        self.diagnostics.render(view)

    def _tick(self) -> None:
        self._after = None
        try:
            self.tick()
        except Exception as e:
            self._callback_error(type(e), e, e.__traceback__)
        finally:
            if self.root is not None:
                self._after = self.root.after(POLL_MS, self._tick)

    def run(self) -> None:
        try:
            self.root.mainloop()
        finally:
            self.close()

    def close(self) -> None:
        """Release Tk references on its owning thread, after stopping background work."""
        if self.root is None:
            return
        if self._after is not None:
            self.root.after_cancel(self._after)
            self._after = None
        self.hotkeys.close()
        self.p.shutdown()
        self.root.destroy()
        self.setup = self.fish = self.diagnostics = self.notebook = None
        self.root = None
        gc.collect()


def run() -> None:
    App().run()
