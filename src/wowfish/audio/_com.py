"""Balance Windows COM initialization on each thread that uses audio devices."""

from __future__ import annotations

import ctypes
import sys
from typing import Any

from wowfish.audio import AudioUnavailable


class ComApartment:
    def __init__(self) -> None:
        self._ole32: Any = None
        self._initialized = False

    def __enter__(self) -> ComApartment:
        if sys.platform == "win32":
            self._ole32 = ctypes.WinDLL("ole32")
            self._ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            self._ole32.CoInitializeEx.restype = ctypes.c_int32
            hr = self._ole32.CoInitializeEx(None, 0)
            self._initialized = hr in (0, 1)
            if not self._initialized and (hr & 0xFFFFFFFF) != 0x80010106:
                raise AudioUnavailable(f"无法初始化音频线程（HRESULT 0x{hr & 0xFFFFFFFF:08X}）")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._initialized:
            self._ole32.CoUninitialize()
            self._initialized = False
