"""Screen capture boundary: find the game window and grab its pixels as BGR arrays."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

from wowfish.errors import NotSupported

Box = tuple[int, int, int, int]  # x, y, width, height in window pixels
GAME_TITLES = ("魔兽世界", "World of Warcraft")


def title_matches(title: str, pattern: str = "") -> bool:
    return any(t.lower() in title.lower() for t in ((pattern,) if pattern else GAME_TITLES))


@dataclass(frozen=True)
class WindowRef:
    title: str
    pid: int | None
    left: int  # screen position; logical (unscaled) pixels for a Wayland window
    top: int
    width: int  # size of a grab, in pixels
    height: int
    handle: int | None = None  # native window id where the platform has one
    compositor: str | None = None  # "hyprland" for a native Wayland window, else None
    scale: float = 1.0  # the Wayland output's scale: grab pixels per logical pixel
    minimized: bool = False


@runtime_checkable
class Capturer(Protocol):
    def open(self, window: WindowRef) -> None: ...

    def update_window(self, window: WindowRef) -> None: ...

    def grab(self, box: Box | None = None) -> np.ndarray:
        """Return the window (or `box` inside it) as HxWx3 uint8 BGR."""
        ...

    def close(self) -> None: ...


def linux_x11() -> bool:
    return sys.platform == "linux" and bool(os.environ.get("DISPLAY"))


def linux_hyprland() -> bool:
    return sys.platform == "linux" and bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


def macos() -> bool:
    return sys.platform == "darwin"


def find_window(title_substring: str = "") -> WindowRef | None:
    if not title_substring:
        found = [w for title in GAME_TITLES if (w := _find_window(title)) is not None]
        return max(found, key=lambda w: w.width * w.height, default=None)
    return _find_window(title_substring)


def _find_window(title_substring: str) -> WindowRef | None:
    """The largest viewable window whose title contains the text.

    On Hyprland the compositor picks it from all its windows, native Wayland and Xwayland alike;
    an Xwayland winner is then looked up through X11 by its pid. Elsewhere X11, then Windows,
    then macOS (in points, which mss grabs as is); else None.
    """
    pid: int | None = None
    if linux_hyprland():
        from wowfish.capture._wayland import find_window_hyprland

        try:
            found = find_window_hyprland(title_substring)
        except (NotSupported, OSError, ValueError):
            if not linux_x11():
                raise
            # Hyprland's IPC did not answer; X11 alone still finds an Xwayland game
        else:
            if found is None or found.compositor:
                return found
            pid = found.pid
    if linux_x11():
        from wowfish.capture._x11 import find_window_x11

        return find_window_x11(title_substring, pid)
    if sys.platform == "win32":
        from wowfish.capture import _win32

        return _win32.find_window_win32(title_substring)
    if macos():
        from wowfish.capture._quartz import find_window_quartz

        return find_window_quartz(title_substring)
    return None


def list_windows(title: str = "") -> list[WindowRef]:
    """Enumerate candidates so users can explicitly choose among multiple clients."""
    if sys.platform == "win32":
        from wowfish.capture._win32 import list_windows_win32

        return list_windows_win32(title)
    if linux_hyprland():
        from wowfish.capture._wayland import hyprland_request, pick_hyprland_window

        monitors = hyprland_request("monitors")
        result = []
        for client in hyprland_request("clients"):
            if title_matches(str(client.get("title", "")), title):
                ref = pick_hyprland_window([client], monitors, str(client["title"]))
                if ref is not None:
                    if ref.compositor is None and linux_x11():
                        from wowfish.capture._x11 import find_window_x11

                        ref = find_window_x11(ref.title, ref.pid)
                    if ref is not None:
                        result.append(ref)
        return result
    if linux_x11():
        from wowfish.capture._x11 import list_windows_x11

        return list_windows_x11(title)
    if macos():
        from wowfish.capture._quartz import pick_quartz_window, window_infos

        return [
            ref
            for info in window_infos()
            if title_matches(str(info.get("name") or info.get("owner") or ""), title)
            and (
                ref := pick_quartz_window(
                    [info], title or str(info.get("name") or info.get("owner"))
                )
            )
            is not None
        ]
    return []


def refresh_window(window: WindowRef) -> WindowRef | None:
    """Refresh the bound client only; never switch to another same-title process."""
    if sys.platform == "win32":
        from wowfish.capture._win32 import refresh_window_win32

        return refresh_window_win32(window)
    return next(
        (w for w in list_windows(window.title) if (w.handle, w.pid) == (window.handle, window.pid)),
        None,
    )


def get_capturer(window: WindowRef | None = None) -> Capturer:
    if window is not None and window.compositor:
        from wowfish.capture._wayland import GrimCapturer

        return GrimCapturer()
    if linux_x11():
        from wowfish.capture._x11 import X11WindowCapturer

        return X11WindowCapturer()
    try:
        import mss  # noqa: F401
    except Exception:
        from wowfish.capture._stub import StubCapturer

        return StubCapturer()
    from wowfish.capture._mss import MssCapturer

    return MssCapturer()


__all__ = [
    "Box",
    "Capturer",
    "WindowRef",
    "find_window",
    "get_capturer",
    "linux_hyprland",
    "linux_x11",
    "list_windows",
    "macos",
    "refresh_window",
    "title_matches",
]
