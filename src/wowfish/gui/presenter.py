"""Tk-free application controller: localized events, atomic config and cancellable workers."""

from __future__ import annotations

import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger

from wowfish.audio import AudioChunk, AudioUnavailable
from wowfish.audio._threaded import ThreadedSource
from wowfish.config import (
    AudioConfig,
    Config,
    ConfigError,
    GameConfig,
    VisionConfig,
    load_config,
    recover_config,
    update_config,
)
from wowfish.engine import events as ev
from wowfish.engine.state import State
from wowfish.engine.thread import EngineThread
from wowfish.gui import image
from wowfish.input import keys
from wowfish.messages import clip_label, error_text, msg

BLOCK = 1024
CALIBRATE_WATCH_S = 30.0


def describe(event: ev.Event) -> str:
    match event:
        case ev.StateChanged(did=did, next=nxt):
            return f"{did}；{nxt}"
        case ev.IconFound(score=score, where=(x, y)):
            return f"找到浮漂 ({x}, {y})，图标得分 {score:.2f}"
        case ev.Calibrated(size=size, score=score):
            return f"图标已校准：{size} 像素，得分 {score:.2f}"
        case ev.IconLost():
            return msg("lost_icon")
        case ev.BiteHeard(clip=clip, score=score):
            return f"识别到{clip_label(clip)}，得分 {score:.2f}"
        case ev.BiteRejected(score=score):
            return f"水花得分 {score:.2f}，自己的浮漂没有明显变化，继续等待"
        case ev.AudioOpened(name=name, fallback=fallback):
            return f"音频来源：{name}" + (f"；{fallback}" if fallback else "")
        case ev.KeyPressed(key=key, why=why):
            action = {
                "cast": "抛竿",
                "bite": "收杆",
                "jump": "跳跃",
                "quit": "退出",
                "lure": "使用鱼饵",
            }
            return f"按键 {key}：{action.get(why, '交互')}"
        case ev.Caught():
            return msg("caught")
        case ev.Timeout(what=what, after_s=seconds, detail=detail):
            label = {"icon": "寻找图标", "bite": "等待咬钩", "loot": "收杆确认"}.get(what, what)
            return f"{label}超时（{seconds:g} 秒）" + (f"；{detail}" if detail else "")
        case ev.Listening(best_clip=clip, best_score=score, threshold=threshold):
            return (
                f"最高水花得分 {max(0, score):.2f}（{clip_label(clip) or '无'}），"
                f"阈值 {threshold:.2f}"
            )
        case ev.Paused(reason=reason):
            return f"已暂停：{reason}"
        case ev.Resumed():
            return "已恢复，将从新的一竿开始"
        case ev.Stats():
            return event.as_line()
        case ev.Error(message=message):
            return f"运行提示：{error_text(message)}"
        case ev.Stopped(reason=reason):
            label = {
                "user": "手动停止",
                "session limit": "达到会话时限",
                "error": "运行错误",
                "window closed": "游戏窗口已关闭",
            }
            return f"已停止：{label.get(reason, reason)}"
    return type(event).__name__


@dataclass(frozen=True)
class CalibrateDone:
    calibration: Any
    thumbnail_png: bytes


@dataclass(frozen=True)
class VerifyDone:
    report: Any


@dataclass(frozen=True)
class PreviewDone:
    thumbnail_png: bytes


@dataclass(frozen=True)
class DiagnosticsDone:
    results: list[Any]


@dataclass(frozen=True)
class WorkerFailed:
    what: str
    message: str


@dataclass
class ScoreTrace:
    t0: float = 0.0
    points: list[tuple[float, dict[str, float]]] = field(default_factory=list)
    threshold: float | None = None
    bite: tuple[float, str, float] | None = None
    timed_out: bool = False
    version: int = 0


@dataclass
class View:
    state: str
    did: str
    next: str
    stats: str
    thumbnail_png: bytes | None
    running: bool
    level_dbfs: float
    last_scores: dict[str, float]
    new_lines: list[str] = field(default_factory=list)
    log_events: list[object] = field(default_factory=list)
    trace: ScoreTrace = field(default_factory=ScoreTrace)
    listening: bool = False
    verify_status: str = ""
    calibrate_status: str = ""
    error: str = ""
    audio_name: str = ""
    working: bool = False
    calibration_running: bool = False
    verification_running: bool = False


@dataclass
class Task:
    stop: threading.Event
    thread: threading.Thread
    source: Any = None


class _Meter:
    def __init__(
        self,
        source: Any,
        detector: Any,
        on_error: Callable[[Exception], None],
        on_open: Callable[[str, str], None],
    ) -> None:
        self.source = ThreadedSource(source)
        self.detector = detector
        self.on_error = on_error
        self.on_open = on_open
        self.level_dbfs = -120.0
        self.scores: dict[str, float] = {}
        self.name = "正在打开音频设备"
        self.fallback_reason = ""
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="foreverfish-meter", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self, wait: bool = False) -> None:
        self._stop.set()
        self.source.cancel()
        if wait:
            self.thread.join(2.0)

    def _run(self) -> None:
        frames = 0
        try:
            if self._stop.is_set():
                return
            with self.source:
                self.name = self.source.name
                self.fallback_reason = self.source.fallback_reason
                self.on_open(self.name, self.fallback_reason)
                while not self._stop.is_set():
                    samples = self.source.read(BLOCK)
                    if len(samples):
                        frames += len(samples)
                        self.detector.feed(
                            AudioChunk(
                                samples, self.source.sample_rate, frames / self.source.sample_rate
                            )
                        )
                        self.level_dbfs = self.detector.level_dbfs
                        self.scores = {s.name: s.score for s in self.detector.last_scores}
                    if self.source.exhausted:
                        break
                    self._stop.wait(0.02)
        except Exception as e:
            if not self._stop.is_set():
                self.on_error(e)


class Presenter:
    def __init__(
        self,
        *,
        build_engine: Callable[..., Any] | None = None,
        list_devices: Callable[[], list[Any]] | None = None,
        open_capture: Callable[..., Any] | None = None,
        find_window: Callable[..., Any] | None = None,
        get_capturer: Callable[..., Any] | None = None,
        list_windows: Callable[..., list[Any]] | None = None,
        refresh_window: Callable[..., Any] | None = None,
    ) -> None:
        from wowfish.audio.devices import GAME, GAME_CHOICE, LoopbackDevice, list_loopback_devices
        from wowfish.audio.devices import open_capture as real_open
        from wowfish.capture import find_window as real_find
        from wowfish.capture import get_capturer as real_capturer
        from wowfish.capture import list_windows as real_windows
        from wowfish.capture import refresh_window as real_refresh
        from wowfish.engine.engine import build_engine as real_build

        self._build_engine = build_engine or real_build
        self._list_devices = list_devices or list_loopback_devices
        self._open_capture = open_capture or real_open
        self._find_window = find_window or real_find
        self._refresh_window = refresh_window or (
            (lambda window: find_window(window.title)) if find_window else real_refresh
        )
        self._get_capturer = get_capturer or real_capturer
        self._list_windows = list_windows or real_windows
        self._game, self._game_choice, self._device_type = GAME, GAME_CHOICE, LoopbackDevice
        self._selected_window: Any = None
        self.config_error = ""
        try:
            self.cfg = load_config()[0]
        except ConfigError as e:
            self.config_error = error_text(str(e))
            self.cfg = Config(game=GameConfig(), vision=VisionConfig(), audio=AudioConfig())
        self._results: queue.Queue[object] = queue.Queue()
        self._thread: EngineThread | None = None
        self._meter: _Meter | None = None
        self._retired_meters: list[_Meter] = []
        self._workers: dict[str, Task] = {}
        self._worker_lock = threading.Lock()
        self.state, self.did, self.next = (
            State.IDLE.name,
            "准备就绪",
            "选择游戏客户端，检查设置后点击开始",
        )
        self.stats = ev.Stats(0.0).as_line()
        self.thumbnail_png: bytes | None = None
        self.last_calibration: CalibrateDone | None = None
        self.trace = ScoreTrace()
        self.verify_status = self.calibrate_status = self.audio_name = ""
        self.error = self.config_error
        self._record_level: float | None = None

    def reload(self) -> None:
        self.cfg = load_config()[0]
        self.config_error = ""

    def _update(self, section: str, **values: Any) -> None:
        self.cfg = update_config(
            lambda current: replace(
                current,
                **{
                    section: replace(getattr(current, section), **values),
                },
            )
        )
        self.config_error = ""

    def restore_config(self) -> Path:
        backup = recover_config()
        self.reload()
        self.error = ""
        return backup

    def save_keys(
        self, cast_key: str, interact_key: str, quit_key: str = "", lure_key: str = ""
    ) -> None:
        normalized = [keys.normalize_key(k) for k in (cast_key, interact_key, quit_key, lure_key)]
        for name in (*normalized[:2], *(k for k in normalized[2:] if k)):
            if not keys.is_supported_key(name):
                raise ValueError(f"不支持键位 {name!r}；可用字母、数字、F1–F12 和空格")
        self._update(
            "game",
            cast_key=normalized[0],
            interact_key=normalized[1],
            quit_key=normalized[2],
            lure_key=normalized[3],
        )

    def save_game_settings(self, **values: Any) -> None:
        self._update("game", **values)

    def clear_selected_window(self) -> None:
        self._selected_window = None

    def save_session_minutes(self, minutes: int) -> None:
        if minutes < 0:
            raise ValueError("时限必须大于或等于 0；0 表示不限时")
        self._update("game", session_minutes=minutes)

    def game_windows(self) -> list[Any]:
        try:
            return self._list_windows(self.cfg.game.window_title)
        except Exception as e:
            self._results.put(WorkerFailed("刷新游戏窗口", str(e)))
            return []

    def select_window(self, window: Any) -> None:
        if self.running:
            raise ValueError("请先停止钓鱼，再更换客户端")
        self._selected_window = window

    def resolve_window(self) -> Any:
        if self._selected_window is not None:
            found = self._refresh_window(self._selected_window)
            if found is None or (found.handle, found.pid) != (
                self._selected_window.handle,
                self._selected_window.pid,
            ):
                return None
            self._selected_window = found
            return found
        return self._find_window(self.cfg.game.window_title)

    def audio_devices(self) -> list[Any]:
        try:
            monitors = self._list_devices()
        except AudioUnavailable:
            monitors = []
        if sys.platform == "darwin":
            return list(monitors)
        return [self._game_choice] + [self._device_type(d.id, d.name, False) for d in monitors]

    def default_device(self) -> Any | None:
        found = self.audio_devices()
        return next((d for d in found if d.is_default), found[0] if found else None)

    def save_audio_device(self, selector: str) -> None:
        if selector == self._game:
            self._update("audio", capture="game")
        else:
            self._update("audio", capture="device", device=selector)

    def save_audio_settings(self, **values: Any) -> None:
        self._update("audio", **values)

    def _open_audio(self, selector: str | None) -> Any:
        selector = selector if selector is not None else self.cfg.audio.selector
        window = self.resolve_window() if selector == self._game else None
        if selector == self._game and window is None:
            raise RuntimeError("未找到选中的游戏窗口，请启动客户端并重新选择")
        return self._open_capture(
            selector,
            pid=window.pid if window else None,
            title=window.title if window else self.cfg.game.window_title,
            fallback=self.cfg.audio.fallback_to_device,
        )

    def save_calibration(self) -> None:
        if self.last_calibration is None or not self.last_calibration.calibration.valid:
            raise ValueError("请先成功校准图标")
        cal = self.last_calibration.calibration
        self._update(
            "vision",
            icon_size_px=cal.icon_size_px,
            icon_native_size=cal.native_size,
            icon_threshold=cal.suggested_threshold(),
        )

    def save_search_region(self, left: float, top: float, right: float, bottom: float) -> None:
        self._update(
            "vision", search_left=left, search_top=top, search_right=right, search_bottom=bottom
        )

    def save_vision_settings(self, **values: Any) -> None:
        self._update("vision", **values)

    def save_bite_threshold(self, value: str) -> float:
        try:
            threshold = float(value)
        except ValueError:
            raise ValueError("请输入有效的数字") from None
        if not 0.0 < threshold <= 1.0:
            raise ValueError("阈值必须大于 0 且不超过 1")
        self._update("audio", bite_threshold=threshold)
        return threshold

    def prerequisites(self, request: bool = False) -> list[str]:
        from wowfish.audio.devices import MAC_NO_GAME_AUDIO
        from wowfish.engine.engine import missing_permissions, unsupported_platform

        reasons = [self.config_error] if self.config_error else []
        problem = unsupported_platform()
        if problem:
            reasons.append(error_text(problem))
        reasons += missing_permissions(request)
        if sys.platform == "darwin" and self.cfg.audio.capture == self._game:
            reasons.append(MAC_NO_GAME_AUDIO)
        try:
            window = self.resolve_window()
        except Exception as e:
            window = None
            reasons.append(error_text(str(e)))
        if window is None and not problem:
            reasons.append("未找到选中的游戏窗口，请启动客户端或重新选择")
        found = self.audio_devices()
        if not found:
            reasons.append("未找到音频输入设备；macOS 请安装 BlackHole")
        if (
            self.cfg.audio.capture == "device"
            and self.cfg.audio.device
            and not any(self.cfg.audio.device in (d.id, d.name) for d in found)
        ):
            reasons.append("选定的音频设备已断开，请刷新设备并重新选择")
        return reasons

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def user_paused(self) -> bool:
        return self._thread is not None and self._thread.engine._user_paused.is_set()

    def start(self, minutes: float | None = None) -> None:
        if self.running:
            return
        self.stop_meter(wait=True)
        self.cancel_workers(wait=True)
        if self.working:
            self._results.put(WorkerFailed("开始", "旧的校准任务仍在结束，请稍后再开始"))
            return
        try:
            self.reload()
            window = self.resolve_window()
            if window is None:
                raise RuntimeError("未找到选中的游戏窗口")
            engine = self._build_engine(
                self.cfg, session_s=None if minutes is None else minutes * 60, window=window
            )
        except Exception as e:
            self._results.put(WorkerFailed("开始", str(e)))
            return
        self.error = self.audio_name = ""
        self.stats = ev.Stats(0.0).as_line()
        self.trace = ScoreTrace(version=self.trace.version + 1)
        self._thread = EngineThread(engine)
        self._thread.start()
        self.state, self.did, self.next = State.IDLE.name, "已启动", "请切回选中的游戏客户端"

    def stop(self) -> None:
        if self._thread is not None:
            self._thread.engine.stop()
        self.cancel_workers()
        self.stop_meter()

    def toggle_pause(self) -> None:
        if self._thread is not None:
            self._thread.engine.toggle_pause()

    def shutdown(self) -> None:
        if self._thread is not None:
            self._thread.engine.stop()
        self.stop_meter(wait=True)
        deadline = time.monotonic() + 2.0
        for meter in self._retired_meters:
            meter.stop()
            meter.thread.join(max(0.0, deadline - time.monotonic()))
        self._retired_meters.clear()
        self.cancel_workers(wait=True)
        if self._thread is not None:
            self._thread.stop(timeout=3.0)
            self._thread = None

    def poll(self) -> View:
        lines: list[str] = []
        items: list[object] = []
        if self._thread is not None:
            q = self._thread.events
            for _ in range(1000):
                try:
                    event = q.get_nowait()
                except queue.Empty:
                    break
                if isinstance(event, ev.Scores):
                    self.trace.points.append((event.t, event.peaks))
                    self.trace.points = self.trace.points[-600:]
                    self.trace.threshold = event.threshold
                    self.trace.version += 1
                    continue
                items.append(event)
                lines.append(describe(event))
                logger.debug(lines[-1])
                self._apply(event)
            if not self._thread.is_alive() and q.empty():
                self._thread = None
        while True:
            try:
                item = self._results.get_nowait()
            except queue.Empty:
                break
            items.append(item)
            line = self._describe_result(item)
            lines.append(line)
            logger.debug(line)
        return View(
            self.state,
            self.did,
            self.next,
            self.stats,
            self.thumbnail_png,
            self.running,
            self.level_dbfs,
            self.last_scores,
            lines,
            items,
            self.trace,
            listening=self.meter_running or self._record_level is not None or self.running,
            verify_status=self.verify_status,
            calibrate_status=self.calibrate_status,
            error=self.error,
            audio_name=self.audio_name or (self._meter.name if self._meter else ""),
            working=self.working,
            calibration_running=self.task_running("calibrate"),
            verification_running=self.task_running("verify"),
        )

    def _apply(self, event: ev.Event) -> None:
        match event:
            case ev.StateChanged(state=state, did=did, next=nxt):
                self.state, self.did, self.next = state.name, did, nxt
            case ev.IconFound(thumbnail=thumb, box=box):
                self.thumbnail_png = image.bgr_to_png_bytes(image.with_box(thumb, box))
                self.trace = ScoreTrace(
                    t0=event.t, threshold=self.trace.threshold, version=self.trace.version + 1
                )
            case ev.BiteHeard(clip=clip, score=score):
                self.trace.bite = (event.t, clip, score)
                self.trace.version += 1
            case ev.Timeout(what="bite"):
                self.trace.timed_out = True
                self.trace.version += 1
            case ev.Stats():
                self.stats = event.as_line()
            case ev.Paused(reason=reason):
                self.state, self.did, self.next = State.PAUSED.name, reason, "恢复后从新的一竿开始"
            case ev.Resumed(state=state):
                self.state, self.did, self.next = state.name, "已恢复", "准备重新抛竿"
            case ev.AudioOpened(name=name, fallback=fallback):
                self.audio_name = name
                if fallback:
                    self.error = error_text(fallback)
            case ev.Stopped():
                self.state, self.did, self.next = (
                    State.STOPPED.name,
                    describe(event),
                    "可以修改设置后重新开始",
                )
            case ev.Error(message=message):
                self.error = error_text(message)

    def _describe_result(self, item: object) -> str:
        match item:
            case ev.AudioOpened():
                self._apply(item)
                return describe(item)
            case CalibrateDone(calibration=cal):
                self.last_calibration = item
                if cal.valid:
                    return f"校准成功：{cal.icon_size_px} 像素，得分 {cal.score:.2f}；点击保存校准"
                return "未找到金色齿轮；请打开交互图标、靠近浮漂，或手动框选水面范围"
            case VerifyDone(report=r):
                if r.stands_out:
                    return (
                        f"录音检测到 {len(r.bites)} 次水花，建议阈值 {r.threshold:.2f}；保存后生效"
                    )
                return "录音中没有清晰的咬钩声音；请检查音效和实际音频来源后重试"
            case WorkerFailed(what=what, message=message):
                self.error = error_text(message)
                return f"{what}失败：{self.error}"
            case PreviewDone():
                return "游戏截图已获取，请框选需要识别的水面区域"
            case DiagnosticsDone():
                return "环境诊断完成"
        return str(item)

    def _launch(self, name: str, target: Callable[[Task], None]) -> None:
        if self.running:
            self._results.put(WorkerFailed("校准或诊断", "请先停止钓鱼"))
            return
        with self._worker_lock:
            if any(task.thread.is_alive() for task in self._workers.values()):
                self._results.put(WorkerFailed("启动任务", "请等待当前任务完成，或先取消"))
                return
            stop = threading.Event()
            task = Task(stop, threading.Thread())

            def guarded() -> None:
                try:
                    target(task)
                except Exception as e:
                    if not task.stop.is_set():
                        self._results.put(WorkerFailed("后台任务", str(e)))

            task.thread = threading.Thread(target=guarded, name=f"foreverfish-{name}", daemon=True)
            self._workers[name] = task
            task.thread.start()

    def task_running(self, name: str) -> bool:
        task = self._workers.get(name)
        return task is not None and task.thread.is_alive()

    @property
    def working(self) -> bool:
        return any(task.thread.is_alive() for task in self._workers.values())

    def cancel_workers(self, wait: bool = False) -> None:
        with self._worker_lock:
            tasks = list(self._workers.values())
        for task in tasks:
            task.stop.set()
            cancel = getattr(task.source, "cancel", None)
            if cancel is not None:
                cancel()
        if wait:
            deadline = time.monotonic() + 1.0
            for task in tasks:
                task.thread.join(max(0.0, deadline - time.monotonic()))

    def grab_and_calibrate(self) -> None:
        self._launch("calibrate", self._calibrate_worker)

    def _calibrate_worker(self, task: Task) -> None:
        from wowfish.vision.calibrate import calibrate_icon_scale
        from wowfish.vision.region import Region
        from wowfish.vision.template import load_icon_templates

        cap = None
        try:
            self.calibrate_status = "正在查找选中的游戏窗口；请切回游戏并抛竿"
            window = self.resolve_window()
            if window is None:
                raise RuntimeError("未找到选中的游戏窗口")
            cap = self._get_capturer(window)
            cap.open(window)
            templates = load_icon_templates()
            v = self.cfg.vision
            region = Region(v.search_left, v.search_top, v.search_right, v.search_bottom)
            deadline = time.monotonic() + CALIBRATE_WATCH_S
            while not task.stop.is_set():
                found = self._refresh_window(window)
                if found is None:
                    raise RuntimeError("校准过程中游戏窗口已关闭")
                if (found.handle, found.pid) != (window.handle, window.pid):
                    raise RuntimeError("校准客户端已关闭，请重新选择游戏窗口")
                if found.minimized:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("校准超时：请恢复游戏窗口后重试")
                    self.calibrate_status = "游戏已最小化，请恢复窗口继续校准"
                    task.stop.wait(0.1)
                    continue
                update = getattr(cap, "update_window", None)
                if update:
                    update(found)
                self.calibrate_status = (
                    f"寻找金色齿轮，剩余 {max(0, deadline - time.monotonic()):.0f} 秒；"
                    "请保持游戏可见"
                )
                frame = cap.grab()
                x0, y0, x1, y1 = region.to_box(frame.shape[1], frame.shape[0])
                cal = calibrate_icon_scale(
                    frame[y0:y1, x0:x1],
                    templates,
                    match_width=v.match_width,
                    cancel=task.stop.is_set,
                )
                cal = replace(cal, x=cal.x + x0, y=cal.y + y0)
                if cal.valid or time.monotonic() >= deadline:
                    thumb, scale = image.thumbnail(frame)
                    side = max(2, round(cal.icon_size_px * scale))
                    box = (
                        round(cal.x * scale - side / 2),
                        round(cal.y * scale - side / 2),
                        side,
                        side,
                    )
                    self._results.put(
                        CalibrateDone(
                            cal,
                            image.bgr_to_png_bytes(
                                image.with_box(thumb, box) if cal.valid else thumb
                            ),
                        )
                    )
                    return
                task.stop.wait(0.1)
        except Exception as e:
            if not task.stop.is_set():
                self._results.put(WorkerFailed("图标校准", str(e)))
        finally:
            if cap is not None:
                cap.close()
            self.calibrate_status = ""

    def grab_preview(self) -> None:
        self._launch("preview", self._preview_worker)

    def _preview_worker(self, task: Task) -> None:
        cap = None
        try:
            self.calibrate_status = "3 秒后截图，请切回游戏并保持水面可见"
            if task.stop.wait(3.0):
                return
            window = self.resolve_window()
            if window is None:
                raise RuntimeError("未找到选中的游戏窗口")
            cap = self._get_capturer(window)
            cap.open(window)
            thumb, _ = image.thumbnail(cap.grab(), width=800)
            self._results.put(PreviewDone(image.bgr_to_png_bytes(thumb)))
        except Exception as e:
            if not task.stop.is_set():
                self._results.put(WorkerFailed("获取游戏截图", str(e)))
        finally:
            if cap is not None:
                cap.close()
            self.calibrate_status = ""

    def record_and_verify(self, seconds: float = 60.0, selector: str | None = None) -> None:
        self.stop_meter(wait=True)
        self._launch("verify", lambda task: self._verify_worker(task, seconds, selector))

    def _verify_worker(self, task: Task, seconds: float, selector: str | None) -> None:
        from wowfish.audio.clips import build_fingerprints
        from wowfish.audio.features import DEFAULT_MEL
        from wowfish.audio.verify import bite_report, score_recording

        try:
            self.verify_status = "正在打开音频设备"
            source = ThreadedSource(self._open_audio(selector), max_s=1.0)
            task.source = source
            if task.stop.is_set():
                source.cancel()
                return
            chunks: list[np.ndarray] = []
            frames = 0
            with source:
                self.audio_name = source.name
                deadline = time.monotonic() + min(seconds, 120.0)
                while not task.stop.is_set() and time.monotonic() < deadline:
                    samples = source.read(BLOCK)
                    if len(samples):
                        chunks.append(samples)
                        frames += len(samples)
                        rms = float(np.sqrt(np.mean(np.square(samples))))
                        self._record_level = 20.0 * np.log10(rms + 1e-9)
                    self.verify_status = (
                        f"录音剩余 {max(0, deadline - time.monotonic()):.0f} 秒；"
                        "请在游戏里手动钓到一次咬钩"
                    )
                    if frames >= seconds * source.sample_rate or source.exhausted:
                        break
                    task.stop.wait(0.02)
            if task.stop.is_set():
                return
            self.verify_status = "正在分析水花录音"
            y = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
            if len(y) < DEFAULT_MEL.n_fft:
                raise RuntimeError("没有采集到有效音频，请检查输出设备和音效")
            fps = build_fingerprints()
            scores = score_recording(y, source.sample_rate, fps, DEFAULT_MEL)
            report = bite_report(scores, fps, DEFAULT_MEL)
            if not task.stop.is_set():
                self._results.put(VerifyDone(report))
        except Exception as e:
            if not task.stop.is_set():
                self._results.put(WorkerFailed("录音验证", str(e)))
        finally:
            self._record_level = None
            self.verify_status = ""

    def run_diagnostics(self) -> None:
        def worker(task: Task) -> None:
            from wowfish.doctor import default_probes, run_probes

            results = run_probes(default_probes(window=self.resolve_window()))
            if not task.stop.is_set():
                self._results.put(DiagnosticsDone(results))

        self._launch("diagnostics", worker)

    @property
    def level_dbfs(self) -> float:
        if self._record_level is not None:
            return self._record_level
        if self._thread is not None:
            return float(getattr(self._thread.engine.detector, "level_dbfs", -120))
        return self._meter.level_dbfs if self._meter else -120.0

    @property
    def last_scores(self) -> dict[str, float]:
        if self._thread is not None:
            return {
                s.name: s.score for s in getattr(self._thread.engine.detector, "last_scores", ())
            }
        return dict(self._meter.scores) if self._meter else {}

    @property
    def meter_running(self) -> bool:
        return self._meter is not None and self._meter.thread.is_alive()

    def start_meter(self, selector: str | None = None) -> None:
        from wowfish.audio.detector import detector_for

        self.stop_meter()
        if self.running or self.working:
            self._results.put(WorkerFailed("试听", "请先停止当前钓鱼或校准任务"))
            return
        try:
            source = self._open_audio(selector)
            detector = detector_for(self.cfg.audio)
            self._meter = _Meter(
                source,
                detector,
                lambda e: self._results.put(WorkerFailed("试听", str(e))),
                lambda name, reason: self._results.put(ev.AudioOpened(0.0, name, reason)),
            )
            self._meter.start()
        except Exception as e:
            self._results.put(WorkerFailed("试听", str(e)))

    def stop_meter(self, wait: bool = False) -> None:
        if self._meter is not None:
            self._meter.stop(wait)
            if self._meter.thread.is_alive():
                self._retired_meters.append(self._meter)
            self._meter = None
        self._retired_meters = [meter for meter in self._retired_meters if meter.thread.is_alive()]
