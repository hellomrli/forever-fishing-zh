"""Exercise the real desktop window, also from a frozen Windows executable."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from wowfish import APP_TITLE, __version__


def _visible(widget: Any, container: Any) -> bool:
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    cx, cy = container.winfo_rootx(), container.winfo_rooty()
    return (
        widget.winfo_width() > 1
        and widget.winfo_height() > 1
        and cx <= x
        and cy <= y
        and x + widget.winfo_width() <= cx + container.winfo_width() + 2
        and y + widget.winfo_height() <= cy + container.winfo_height() + 2
    )


def _windows_checks(app: Any) -> dict[str, Any]:
    import ctypes
    from ctypes import wintypes

    import ttkbootstrap as tb

    from wowfish.capture import refresh_window
    from wowfish.capture._mss import MssCapturer
    from wowfish.capture._win32 import list_windows_win32
    from wowfish.gui.hotkeys import HOTKEY_PAUSE, HOTKEY_STOP, WM_HOTKEY

    seen: list[int] = []
    original = dict(app.hotkeys.callbacks)
    for ident in original:
        app.hotkeys.callbacks[ident] = lambda code=ident: seen.append(code)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    for ident in (HOTKEY_PAUSE, HOTKEY_STOP):
        assert user32.PostThreadMessageW(app.hotkeys.thread_id, WM_HOTKEY, ident, 0)
    deadline = time.monotonic() + 2.0
    while len(seen) < 2 and time.monotonic() < deadline:
        app.root.update()
        app.hotkeys.poll()
        time.sleep(0.02)
    app.hotkeys.callbacks = original
    assert seen == [HOTKEY_PAUSE, HOTKEY_STOP], f"全局快捷键消息丢失：{seen}"

    fixture = tb.Toplevel(master=app.root, title="魔兽世界-界面测试")
    fixture.geometry("240x180+50+50")
    tb.Label(fixture, text="中文窗口捕获测试").pack()
    app.root.update()
    window = next(w for w in list_windows_win32("魔兽世界-界面测试") if w.pid == os.getpid())
    cap = MssCapturer()
    try:
        cap.open(window)
        assert cap.grab().shape[:2] == (window.height, window.width)
        fixture.geometry("320x200+120+100")
        app.root.update()
        changed = refresh_window(window)
        assert changed is not None
        assert (changed.left, changed.top, changed.width, changed.height) != (
            window.left,
            window.top,
            window.width,
            window.height,
        )
        cap.update_window(changed)
        assert cap.grab().shape[:2] == (changed.height, changed.width)
        fixture.iconify()
        app.root.update()
        minimized = refresh_window(changed)
        assert minimized is not None and minimized.minimized
        fixture.deiconify()
        app.root.update()
        restored = refresh_window(minimized)
        assert restored is not None and not restored.minimized
        fixture.destroy()
        app.root.update()
        assert refresh_window(restored) is None
    finally:
        cap.close()
        if fixture.winfo_exists():
            fixture.destroy()
    audio_probe: dict[str, Any]
    from wowfish.audio._threaded import ThreadedSource
    from wowfish.audio._wasapi import ProcessLoopbackSource

    try:
        with ThreadedSource(
            ProcessLoopbackSource(os.getpid(), timeout_s=0.2, silence_on_timeout=True)
        ) as audio:
            assert audio.sample_rate == 22050 and audio.read(8).dtype == np.float32
            audio_probe = {"state": "opened", "name": audio.name}
    except Exception as e:
        # Hosted runners may have no Windows Audio service or output endpoint. Record the
        # real HRESULT for inspection; the runtime's fallback is tested separately.
        audio_probe = {"state": "unavailable", "reason": str(e)}
    return {
        "hotkeys": seen,
        "chinese_window": True,
        "capture_move_resize": True,
        "minimize_restore": True,
        "window_close": True,
        "process_audio": audio_probe,
    }


def run_smoke(report_path: Path) -> int:
    import tkinter.font as tkfont

    from loguru import logger

    from wowfish.cli import _setup_logging
    from wowfish.config import ensure_config_file, load_config
    from wowfish.gui.app import App
    from wowfish.gui.diagnostics_page import RegionPicker
    from wowfish.gui.presenter import Presenter
    from wowfish.gui.widgets import set_entry
    from wowfish.vision.imageio import read_image, write_image

    report_path = report_path.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "ok": False,
        "version": __version__,
        "platform": sys.platform,
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    previous = os.environ.get("FOREVERFISH_HOME")
    app = None
    try:
        with tempfile.TemporaryDirectory(prefix="foreverfish-smoke-") as directory:
            os.environ["FOREVERFISH_HOME"] = directory
            _setup_logging()
            ensure_config_file()
            app = App(Presenter())
            callback_errors: list[str] = []
            app.root.report_callback_exception = lambda _kind, exc, _tb: callback_errors.append(
                str(exc)
            )
            app.root.update()
            assert app.root.title() == f"{APP_TITLE} {__version__}"
            tabs = [app.notebook.tab(i, "text") for i in range(3)]
            assert tabs == ["钓鱼", "设置", "校准与诊断"]
            assert tkfont.nametofont("ForeverHeading.TFont").actual("weight") == "bold"
            report.update(title=app.root.title(), tabs=tabs, chinese_font=app.font_family)
            original_scaling = float(app.root.tk.call("tk", "scaling"))
            sizes = {
                name: tkfont.nametofont(name).actual("size")
                for name in app.root.tk.call("font", "names")
            }
            layouts = []
            screenshots = []
            for factor in (1.0, 1.25, 1.5):
                app.root.tk.call("tk", "scaling", original_scaling * factor)
                for name, size in sizes.items():
                    font = tkfont.nametofont(name)
                    font.configure(size=size + 1)
                    font.configure(size=size)
                app.root.geometry("900x640+20+20")
                for index, page in enumerate((app.fish, app.setup, app.diagnostics)):
                    app.notebook.select(index)
                    app.root.update()
                    app.tick()
                    app.root.update()
                    assert (app.root.winfo_width(), app.root.winfo_height()) == (900, 640)
                    if index == 0:
                        for control in (page.start, page.pause, page.stop, page.graph.canvas):
                            assert _visible(control, app.root), f"钓鱼页控件被裁切：{control}"
                        assert page.graph.canvas.winfo_height() >= 100
                    else:
                        page.scroll.canvas.yview_moveto(1.0)
                        app.root.update()
                        control = page.recover if index == 1 else page.export
                        assert _visible(control, page.scroll.canvas), (
                            f"底部按钮不可达：{tabs[index]}"
                        )
                        page.scroll.canvas.yview_moveto(0.0)
                        app.root.update()
                    layouts.append(
                        {
                            "scale": factor,
                            "page": tabs[index],
                            "size": [900, 640],
                            "font_height": tkfont.nametofont("TkDefaultFont").metrics("linespace"),
                        }
                    )
                    if factor == 1.0:
                        from PIL import ImageGrab

                        x, y = app.root.winfo_rootx(), app.root.winfo_rooty()
                        path = report_path.with_name(f"smoke-page-{index + 1}.png")
                        ImageGrab.grab(bbox=(x, y, x + 900, y + 640)).save(path)
                        screenshots.append(path.name)
            report.update(layouts=layouts, screenshots=screenshots)
            app.root.tk.call("tk", "scaling", original_scaling)
            for name, size in sizes.items():
                tkfont.nametofont(name).configure(size=size + 1)
                tkfont.nametofont(name).configure(size=size)
            app.notebook.select(1)
            set_entry(app.setup.cast, "not-a-key")
            app.setup.save_game.invoke()
            assert "保存失败" in app.setup.game_note.cget("text")
            assert load_config()[0].game.cast_key == "1"
            set_entry(app.setup.cast, "2")
            set_entry(app.setup.interact, "g")
            set_entry(app.setup.title, "魔兽世界")
            app.setup.save_game.invoke()
            assert load_config()[0].game.cast_key == "2"
            app.p.save_audio_settings(capture="device", device="扬声器（测试设备）")
            assert load_config()[0].audio.device == "扬声器（测试设备）"
            frame = np.full((180, 240, 3), (90, 60, 30), np.uint8)
            chinese_path = Path(directory) / "中文截图" / "浮漂.png"
            assert write_image(chinese_path, frame)
            assert np.array_equal(read_image(chinese_path), frame)
            from wowfish.gui.image import bgr_to_png_bytes

            picker = RegionPicker(app.root, bgr_to_png_bytes(frame), app.p, lambda: None)
            picker._begin(SimpleNamespace(x=24, y=18))
            picker._drag(SimpleNamespace(x=192, y=144))
            picker._save()
            assert load_config()[0].vision.search_left == 0.1
            assert load_config()[0].vision.search_bottom == 0.8
            app.fish.start.invoke()
            assert not app.p.running and "未找到" in app.p.error
            if sys.platform == "win32":
                report["windows"] = _windows_checks(app)
            logger.info("中文界面与配置冒烟验证完成")
            from tkinter import filedialog

            saved_dialog = filedialog.asksaveasfilename
            exported = Path(directory) / "中文日志.log"
            try:
                filedialog.asksaveasfilename = lambda **_kw: str(exported)
                app.diagnostics._export()
            finally:
                filedialog.asksaveasfilename = saved_dialog
            assert exported.exists() and "中文界面" in exported.read_text(encoding="utf-8")
            assert not callback_errors, callback_errors
            report.update(
                config_utf8=True,
                invalid_key_rejected=True,
                chinese_image_path=True,
                manual_region=True,
                log_export=True,
                callbacks_clean=True,
            )
            app.close()
            app = None
            logger.remove()
            report["ok"] = True
    except Exception as e:
        report.update(error=f"{type(e).__name__}: {e}", traceback=traceback.format_exc())
    finally:
        if app is not None:
            app.close()
        logger.remove()
        if previous is None:
            os.environ.pop("FOREVERFISH_HOME", None)
        else:
            os.environ["FOREVERFISH_HOME"] = previous
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0 if report["ok"] else 1
