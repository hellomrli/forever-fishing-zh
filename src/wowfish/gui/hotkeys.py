"""Windows hotkeys owned by a dedicated message thread, dispatched on the Tk thread."""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

from wowfish.input.keys import VK_CODES, normalize_key

WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
PM_REMOVE = 1
HOTKEY_PAUSE = 0x4F01
HOTKEY_STOP = 0x4F02


class HotkeyManager:
    def __init__(
        self, pause: Callable[[], None], stop: Callable[[], None], *, backend: Any = None
    ) -> None:
        self.callbacks = {HOTKEY_PAUSE: pause, HOTKEY_STOP: stop}
        self._backend = backend
        self._registered: set[int] = set()
        self._keys: tuple[str, str] | None = None
        self._commands: queue.Queue[Any] = queue.Queue()
        self._events: queue.Queue[int] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.thread_id = 0
        if backend is None and sys.platform == "win32":
            self._backend = ctypes.WinDLL("user32", use_last_error=True)
            self._backend.RegisterHotKey.argtypes = [
                wintypes.HWND,
                ctypes.c_int,
                wintypes.UINT,
                wintypes.UINT,
            ]
            self._backend.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            self._backend.PeekMessageW.argtypes = [
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
                wintypes.UINT,
            ]
        if self._backend is not None:
            self._thread = threading.Thread(
                target=self._run, name="foreverfish-hotkeys", daemon=True
            )
            self._thread.start()

    def sync(self, pause: str, stop: str) -> str:
        names = (normalize_key(pause), normalize_key(stop))
        if names == self._keys:
            return ""
        self._keys = names
        if self._backend is None:
            return ""
        ready, answer = threading.Event(), []
        self._commands.put((names, ready, answer))
        if not ready.wait(1.0):
            return "快捷键注册线程没有响应，请重新打开助手"
        return answer[0]

    def _register(self, names: tuple[str, str]) -> str:
        self._unregister()
        errors = []
        for ident, name in zip(self.callbacks, names, strict=True):
            if not name:
                continue
            if name not in {f"f{i}" for i in range(1, 13)}:
                errors.append(f"快捷键 {name!r} 无效，仅支持 F1–F12")
                continue
            if self._backend.RegisterHotKey(None, ident, MOD_NOREPEAT, VK_CODES[name]):
                self._registered.add(ident)
            else:
                errors.append(f"快捷键 {name.upper()} 已被其他程序占用；可在设置中更换")
        return "；".join(errors)

    def _run(self) -> None:
        if sys.platform == "win32":
            kernel32 = ctypes.WinDLL("kernel32")
            self.thread_id = int(kernel32.GetCurrentThreadId())
        try:
            while not self._stop.is_set():
                try:
                    names, ready, answer = self._commands.get(timeout=0.02)
                except queue.Empty:
                    pass
                else:
                    try:
                        answer.append(self._register(names))
                    except Exception as e:
                        answer.append(f"无法注册快捷键：{e}")
                    finally:
                        ready.set()
                message = wintypes.MSG()
                for _ in range(32):
                    if not self._backend.PeekMessageW(
                        ctypes.byref(message), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE
                    ):
                        break
                    ident = int(message.wParam)
                    if ident in self._registered:
                        self._events.put(ident)
        finally:
            self._unregister()

    def poll(self) -> None:
        # Tk's own message pump consumes thread messages. Keeping the native queue on our
        # worker avoids losing WM_HOTKEY; callbacks run here, on the caller's GUI thread.
        for _ in range(32):
            try:
                ident = self._events.get_nowait()
            except queue.Empty:
                break
            self.callbacks[ident]()

    def _unregister(self) -> None:
        for ident in self._registered:
            self._backend.UnregisterHotKey(None, ident)
        self._registered.clear()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.0)
        self._keys = None
