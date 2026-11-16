"""Read-only environment checks with Chinese results and real audio capability probing."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from wowfish import APP_TITLE, __version__, paths
from wowfish.capture import WindowRef, find_window, linux_hyprland, macos
from wowfish.messages import error_text

Level = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class ProbeResult:
    name: str
    level: Level
    detail: str


Probe = Callable[[], ProbeResult]


def run_probes(probes: Sequence[Probe]) -> list[ProbeResult]:
    results = []
    for probe in probes:
        try:
            results.append(probe())
        except Exception as e:
            results.append(ProbeResult(probe.__name__, "fail", error_text(str(e))))
    return results


def render(results: Sequence[ProbeResult]) -> str:
    labels = {"ok": "正常", "warn": "提示", "fail": "失败"}
    return "\n".join(f"[{labels[r.level]}] {r.name}：{error_text(r.detail)}" for r in results)


def exit_code(results: Sequence[ProbeResult]) -> int:
    return 1 if any(r.level == "fail" for r in results) else 0


def _configured_window() -> WindowRef | None:
    from wowfish.config import load_config

    return find_window(load_config()[0].game.window_title)


def probe_system() -> ProbeResult:
    detail = (
        f"{platform.system()} {platform.machine()}，Python {platform.python_version()}，"
        f"{APP_TITLE} {__version__}，日志 {paths.log_file()}"
    )
    return ProbeResult("系统", "ok", detail)


def probe_display() -> ProbeResult:
    if macos():
        from wowfish.capture._quartz import screen_capture_available

        ok, detail = screen_capture_available()
        return ProbeResult("屏幕捕获", "ok" if ok else "warn", detail)
    if sys.platform == "win32":
        return ProbeResult("屏幕捕获", "ok", "Windows 桌面；已启用 DPI 感知")
    name = os.environ.get("DISPLAY")
    if not name:
        return ProbeResult(
            "屏幕捕获",
            "ok" if linux_hyprland() else "warn",
            "Hyprland" if linux_hyprland() else "未连接图形桌面，请在桌面会话中运行",
        )
    try:
        from Xlib import display

        display.Display().close()
    except Exception as e:
        return ProbeResult("屏幕捕获", "warn" if linux_hyprland() else "fail", str(e))
    return ProbeResult("屏幕捕获", "ok", f"{name}（X11 / Xwayland）")


def probe_opencv() -> ProbeResult:
    import cv2

    from wowfish.audio.clips import build_fingerprints
    from wowfish.vision.template import load_icon_templates

    return ProbeResult(
        "识别组件",
        "ok",
        f"OpenCV {cv2.__version__}；{len(load_icon_templates())} 张图标，"
        f"{len(build_fingerprints())} 段水花音频",
    )


def probe_audio() -> ProbeResult:
    from wowfish.audio.devices import list_loopback_devices

    try:
        found = list_loopback_devices()
    except Exception as e:
        return ProbeResult("回环设备", "warn", f"无法枚举声音设备：{e}")
    if not found:
        return ProbeResult("回环设备", "warn", "未找到声音输出设备；请连接扬声器或耳机")
    default = next((d.name for d in found if d.is_default), found[0].name)
    return ProbeResult("回环设备", "ok", f"{len(found)} 个设备，默认：{default}")


def probe_game_audio(window: WindowRef | None = None) -> ProbeResult:
    from wowfish.audio._threaded import ThreadedSource
    from wowfish.audio.devices import open_capture
    from wowfish.config import load_config

    cfg = load_config()[0]
    window = window or _configured_window()
    if cfg.audio.capture == "game" and window is None:
        return ProbeResult("实际音频来源", "warn", "请先启动并选择游戏客户端，再检查进程捕获能力")
    try:
        source = open_capture(
            cfg.audio.selector,
            pid=window.pid if window else None,
            title=window.title if window else cfg.game.window_title,
            fallback=cfg.audio.fallback_to_device,
        )
        with ThreadedSource(source) as opened:
            detail = opened.name
            fallback = opened.fallback_reason
            if fallback:
                return ProbeResult("实际音频来源", "warn", f"{detail}；{fallback}")
            return ProbeResult("实际音频来源", "ok", f"已实际打开：{detail}")
    except Exception as e:
        return ProbeResult("实际音频来源", "warn", str(e))


def probe_game_window(window: WindowRef | None = None) -> ProbeResult:
    window = window or _configured_window()
    if window is None:
        return ProbeResult("游戏窗口", "warn", "未找到游戏，请启动客户端后刷新窗口")
    detail = (
        f"{window.title}，PID {window.pid}，{window.width} × {window.height}，"
        f"位置 ({window.left}, {window.top})"
    )
    if window.minimized:
        return ProbeResult("游戏窗口", "warn", detail + "；已最小化，请恢复游戏窗口")
    if window.compositor and shutil.which("grim") is None:
        return ProbeResult("游戏窗口", "fail", detail + "；请安装 grim 以捕获 Wayland 窗口")
    return ProbeResult("游戏窗口", "ok", detail)


def probe_input() -> ProbeResult:
    if sys.platform == "win32":
        return ProbeResult("按键输入", "ok", "SendInput；只在选中的游戏获得焦点时发送按键")
    if macos():
        from wowfish.input._quartz import accessibility_available

        ok, detail = accessibility_available()
    elif linux_hyprland() and not os.environ.get("DISPLAY"):
        from wowfish.input._wayland import virtual_keyboard_available

        ok, detail = virtual_keyboard_available()
    elif os.environ.get("DISPLAY"):
        from wowfish.input._x11 import xtest_available

        ok, detail = xtest_available()
    else:
        return ProbeResult("按键输入", "warn", "请在图形桌面会话中运行")
    return ProbeResult("按键输入", "ok" if ok else "warn", detail)


def probe_calibration(window: WindowRef | None = None) -> ProbeResult:
    from wowfish.config import load_config
    from wowfish.vision.matcher import IconMatcher
    from wowfish.vision.template import load_icon_templates, template_for_size

    cfg = load_config()[0]
    size = cfg.vision.icon_size_px
    if size <= 0:
        return ProbeResult(
            "图标校准", "warn", "尚未校准；首次抛竿会自动测量，也可在“校准与诊断”页手动校准"
        )
    window = window or _configured_window()
    if window is None:
        return ProbeResult("图标校准", "ok", f"已保存 {size} 像素；游戏启动后重新核对尺寸")
    matcher = IconMatcher(template_for_size(load_icon_templates(), size), cfg.vision.match_width)
    return ProbeResult(
        "图标校准",
        "ok",
        f"图标 {size} 像素，缩图后保留 {matcher.template_px(window.width)} 像素细节",
    )


def probe_config() -> ProbeResult:
    from wowfish.config import load_config

    path = paths.config_file()
    _, warnings = load_config(path)
    return ProbeResult(
        "配置文件",
        "warn" if warnings else "ok",
        str(path) + ("；" + "；".join(warnings) if warnings else ""),
    )


def default_probes(window: WindowRef | None = None) -> list[Probe]:
    return [
        probe_system,
        probe_display,
        probe_opencv,
        probe_audio,
        lambda: probe_game_window(window),
        lambda: probe_game_audio(window),
        probe_input,
        lambda: probe_calibration(window),
        probe_config,
    ]
