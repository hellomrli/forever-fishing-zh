"""Regressions for the audited failures; no real game or key injection is needed."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
import weakref
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from loguru import logger
from typer.testing import CliRunner

from wowfish import capture
from wowfish.audio import AudioUnavailable
from wowfish.audio._recording import RecordingSource
from wowfish.audio._threaded import ThreadedSource
from wowfish.audio._wasapi import ProcessLoopbackSource, to_mono_22050
from wowfish.audio.clips import build_fingerprints
from wowfish.audio.detector import BiteEvent
from wowfish.audio.features import DEFAULT_MEL
from wowfish.audio.verify import bite_report, clip_peaks, score_recording
from wowfish.audio.wav import read_wav
from wowfish.capture import WindowRef
from wowfish.capture._mss import MssCapturer
from wowfish.cli import app
from wowfish.config import (
    AudioConfig,
    Config,
    ConfigError,
    GameConfig,
    RunConfig,
    VisionConfig,
    add_missing_settings,
    ensure_config_file,
    load_config,
    recover_config,
    update_config,
)
from wowfish.engine import events as ev
from wowfish.engine.engine import FishingEngine
from wowfish.engine.state import State
from wowfish.gui.hotkeys import HOTKEY_PAUSE, HOTKEY_STOP, MOD_NOREPEAT, HotkeyManager
from wowfish.gui.presenter import CalibrateDone, Presenter, WorkerFailed
from wowfish.vision.bite import VisualBiteVerifier
from wowfish.vision.calibrate import Calibration, calibrate_icon_scale
from wowfish.vision.imageio import read_image, write_image
from wowfish.vision.matcher import IconMatch, IconMatcher
from wowfish.vision.region import Region
from wowfish.vision.search import IconSearch
from wowfish.vision.template import load_icon_templates, template_for_size


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREVERFISH_HOME", str(tmp_path / "数据"))
    yield
    logger.remove()


class FakeIO:
    sample_rate = 22050

    def __init__(self, window=None):
        self.window = window or WindowRef("魔兽世界", 123, 10, 20, 80, 60, 456)
        self.t = 0.0
        self.score = 0.9
        self.focused = True
        self.pressed = []
        self.sleeps = []
        self.resets = self.clears = 0
        self.frame = np.zeros((self.window.height, self.window.width, 3), np.uint8)

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds

    def open(self, window):
        self.window = window

    def update_window(self, window):
        self.window = window

    def close(self):
        pass

    def is_game_focused(self, window):
        return self.focused

    def press_key(self, key, hold_s):
        self.pressed.append(key)

    def read(self, count):
        return np.zeros(0, np.float32)

    def clear(self):
        self.clears += 1
        return 0

    def grab(self, box=None):
        if box is None:
            return self.frame.copy()
        x, y, w, h = box
        return self.frame[y : y + h, x : x + w].copy()

    def match(self, frame, region=None, full_width=None):
        return IconMatch(self.score, frame.shape[1] // 2, frame.shape[0] // 2, 18, 1.0)

    def reset(self):
        self.resets += 1

    def feed(self, chunk):
        return []


def engine(io, **kwargs):
    cfg = kwargs.pop(
        "cfg",
        Config(
            GameConfig(),
            VisionConfig(confirm_bite=False),
            AudioConfig(),
            run=RunConfig(pause_min_s=0, pause_max_s=0),
        ),
    )
    return FishingEngine(
        cfg,
        io.window,
        capturer=kwargs.pop("capturer", io),
        input_device=io,
        audio_source=io,
        detector=io,
        matcher=io,
        clock=io.clock,
        sleep=io.sleep,
        **kwargs,
    )


def test_auto_detects_chinese_and_english_clients(monkeypatch):
    small = WindowRef("魔兽世界", 10, 0, 0, 800, 600, 1)
    large = WindowRef("World of Warcraft", 20, 0, 0, 1600, 900, 2)
    monkeypatch.setattr(
        capture, "_find_window", lambda title: {small.title: small, large.title: large}.get(title)
    )
    assert capture.find_window() == large
    assert capture.find_window("魔兽世界") == small
    assert capture.title_matches("魔兽世界 - 角色甲")
    assert capture.title_matches("world of warcraft")
    assert not capture.title_matches("其他游戏")


def test_title_override_reaches_window_audio_and_refresh(monkeypatch):
    import wowfish.engine.engine as module
    from wowfish.audio import devices
    from wowfish.input import get_input_device

    del get_input_device
    requested, opened, refreshed = [], [], []
    window = WindowRef("Forever 角色甲", 987, 0, 0, 80, 60, 123)
    io = FakeIO(window)
    monkeypatch.setattr(module, "unsupported_platform", lambda: "")
    monkeypatch.setattr(module, "missing_permissions", lambda **_: [])
    monkeypatch.setattr(module, "find_window", lambda title: requested.append(title) or window)
    monkeypatch.setattr(module, "get_capturer", lambda _: io)
    monkeypatch.setattr("wowfish.input.get_input_device", lambda _: io)
    monkeypatch.setattr(module, "refresh_window", lambda target: refreshed.append(target) or target)
    monkeypatch.setattr(devices, "open_capture", lambda *args, **kw: opened.append(kw) or io)
    cfg = Config(GameConfig(window_title="魔兽世界"), VisionConfig(), AudioConfig())
    built = module.build_engine(cfg, title="Forever 角色甲")
    assert requested == ["Forever 角色甲"]
    assert opened[0]["pid"] == 987 and opened[0]["title"] == window.title
    assert built._find_window("ignored title") == window
    assert refreshed == [window]


def test_cli_title_is_forwarded(monkeypatch):
    from wowfish.commands import run

    observed = []

    def stop_after_build(cfg, **kwargs):
        observed.append(kwargs)
        raise RuntimeError("验证窗口参数")

    monkeypatch.setattr(run, "build_engine", stop_after_build)
    result = CliRunner().invoke(app, ["run", "--title", "魔兽世界", "--minutes", "1"])
    assert result.exit_code == 1
    assert observed[0]["title"] == "魔兽世界" and observed[0]["session_s"] == 60


def test_presenter_binds_selected_pid_and_does_not_change_title_filter():
    first = WindowRef("魔兽世界", 10, 0, 0, 800, 600, 1)
    second = replace(first, pid=20, handle=2)
    opened = []
    p = Presenter(
        list_devices=lambda: [],
        find_window=lambda _: first,
        refresh_window=lambda w: replace(w, left=100),
        list_windows=lambda _: [first, second],
        open_capture=lambda *args, **kw: opened.append(kw),
    )
    p.select_window(second)
    p._open_audio("game")
    assert opened[0]["pid"] == 20 and opened[0]["title"] == "魔兽世界"
    assert p.cfg.game.window_title == ""
    p._refresh_window = lambda _: replace(second, pid=99)
    assert p.resolve_window() is None
    p.shutdown()


@pytest.mark.parametrize("change", [{"left": 110, "top": 120}, {"width": 100, "height": 90}])
def test_engine_updates_actual_mss_bounds(change, monkeypatch):
    io = FakeIO()
    updated = replace(io.window, **change)
    areas = []
    shot = SimpleNamespace(
        grab=lambda area: (
            areas.append(area) or np.zeros((area["height"], area["width"], 4), np.uint8)
        ),
        close=lambda: None,
    )
    monkeypatch.setattr("wowfish.capture._mss._mss_instance", lambda: shot)
    cap = MssCapturer()
    cap.open(io.window)
    e = engine(io, capturer=cap, find_window=lambda _: updated)
    e.step()
    frame = cap.grab()
    assert e.window == updated
    assert areas[-1] == {
        "left": updated.left,
        "top": updated.top,
        "width": updated.width,
        "height": updated.height,
    }
    assert frame.shape[:2] == (updated.height, updated.width)
    cap.close()


def test_one_missing_frame_is_not_a_catch_and_recovery_resets_confirmation():
    io = FakeIO()
    e = engine(io)
    e.state, e._icon_xy = State.INTERACT, (40, 30)
    io.score = 0.0
    e.step()
    assert e.stats.catches == 0 and e.state is State.INTERACT
    io.t += 0.25
    io.score = 0.9
    e.step()
    assert e._misses == 0 and e._disappeared_since is None
    io.score = 0.0
    for _ in range(2):
        io.t += 0.26
        e.step()
        assert e.stats.catches == 0
    io.t += 0.26
    e.step()
    assert e.stats.catches == 1 and e.state is State.IDLE


def test_saved_audio_and_manual_calibration_preserve_fresh_config():
    p = Presenter(list_devices=lambda: [], find_window=lambda _: None)
    update_config(
        lambda cfg: replace(cfg, vision=replace(cfg.vision, icon_size_px=53, icon_native_size=53))
    )
    p.save_audio_device("扬声器")
    assert load_config()[0].vision.icon_size_px == 53
    p.last_calibration = CalibrateDone(Calibration(42, 42, 0.9, 10, 10, 0.3, 0.4), b"")
    update_config(lambda cfg: replace(cfg, audio=replace(cfg.audio, device="耳机")))
    p.save_calibration()
    cfg = load_config()[0]
    assert cfg.audio.device == "耳机" and cfg.vision.icon_size_px == 42
    p.shutdown()


def test_utf8_comments_unknown_fields_and_atomic_failure(tmp_path, monkeypatch):
    path = tmp_path / "中文配置.toml"
    path.write_text(
        '# 我的注释\n[game]\ncast_key = "1" # 自定义按键\n'
        'custom = "保留"\n[extra]\nnote = "额外分组"\n',
        encoding="utf-8",
    )
    update_config(lambda cfg: replace(cfg, audio=replace(cfg.audio, device="扬声器（中文）")), path)
    add_missing_settings(path)
    text = path.read_text(encoding="utf-8")
    assert "# 我的注释" in text and "# 自定义按键" in text
    assert 'custom = "保留"' in text and 'note = "额外分组"' in text
    assert load_config(path)[0].audio.device == "扬声器（中文）"

    def cannot_replace(*_args):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr("wowfish.config.os.replace", cannot_replace)
    with pytest.raises(OSError):
        update_config(lambda cfg: replace(cfg, game=replace(cfg.game, cast_key="2")), path)
    assert path.read_text(encoding="utf-8") == text
    assert not list(tmp_path.glob("*.tmp"))


def test_concurrent_updates_do_not_lose_changes(tmp_path):
    path = tmp_path / "config.toml"

    def increment(_):
        update_config(
            lambda cfg: replace(
                cfg, game=replace(cfg.game, session_minutes=cfg.game.session_minutes + 1)
            ),
            path,
        )

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(increment, range(20)))
    assert load_config(path)[0].game.session_minutes == 20
    code = (
        "import sys; from pathlib import Path; from dataclasses import replace; "
        "from wowfish.config import update_config; "
        "[update_config(lambda c: replace(c, game=replace(c.game, "
        "session_minutes=c.game.session_minutes+1)), Path(sys.argv[1])) for _ in range(8)]"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for _ in range(2)
    ]
    for proc in processes:
        _out, err = proc.communicate(timeout=20)
        assert proc.returncode == 0, err.decode("utf-8", "replace")
    assert load_config(path)[0].game.session_minutes == 36


@pytest.mark.parametrize(
    "key,value",
    [
        ("cast_key", ""),
        ("cast_key", "not-a-key"),
        ("interact_key", ""),
        ("interact_key", "ctrl+f"),
        ("pause_hotkey", "f"),
        ("cast_key", "f8"),
        ("stop_hotkey", "f8"),
    ],
)
def test_invalid_keys_rejected_before_running(tmp_path, key, value):
    path = tmp_path / "config.toml"
    path.write_text(f'[game]\n{key} = "{value}"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_nonfinite_threshold_is_rejected(tmp_path, bad):
    path = tmp_path / "config.toml"
    path.write_text(f"[audio]\nbite_threshold = {bad}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_corrupted_config_requires_explicit_backup_before_recovery():
    from wowfish import paths

    path = paths.config_file()
    path.write_bytes(b"\xff\xfeinvalid")
    p = Presenter(list_devices=lambda: [], find_window=lambda _: None)
    assert p.config_error and p.error
    assert path.read_bytes() == b"\xff\xfeinvalid"
    backup = recover_config()
    assert backup.read_bytes() == b"\xff\xfeinvalid"
    assert load_config()[0].game.cast_key == "1"
    p.shutdown()


def test_loop_waits_even_without_new_audio_and_when_paused():
    io = FakeIO()
    e = engine(io)
    e.state, e._icon_xy = State.WAIT_BITE, (40, 30)
    for _ in range(100):
        e.step()
    assert io.t >= 1.99 and len(io.sleeps) == 100
    e.pause()
    e.step()
    before = io.t
    e.step()
    assert e.state is State.PAUSED and io.t > before


def test_pause_and_resize_resume_with_a_new_cast_and_never_switch_pid():
    io = FakeIO()
    e = engine(io, find_window=lambda _: io.window)
    e.state, e._icon_xy = State.WAIT_BITE, (40, 30)
    io.focused = False
    e.step()
    assert e.state is State.PAUSED and io.pressed == []
    io.focused = True
    e.step()
    assert e.state is State.CASTING and io.pressed == ["1"] and io.clears == 1
    io.window = replace(io.window, minimized=True)
    io.t += 0.3
    e.step()
    assert e.state is State.PAUSED
    io.window = replace(io.window, minimized=False, width=100)
    io.t += 0.3
    e.step()
    assert e.state is State.CASTING and io.pressed == ["1", "1"]
    io.window = replace(io.window, pid=999)
    io.t += 0.3
    e.step()
    assert e.state is State.STOPPED and io.pressed == ["1", "1"]


def test_wasapi_timeout_counts_packet_wait_and_silence_can_continue():
    clock = SimpleNamespace(t=0.0)

    class Idle:
        def read_packet(self):
            clock.t += 0.1
            return None

    source = ProcessLoopbackSource(123, backend=Idle(), timeout_s=5.0, clock=lambda: clock.t)
    with pytest.raises(RuntimeError, match="5 秒"):
        source.read(1)
    assert 5.0 <= clock.t <= 5.11
    quiet = ProcessLoopbackSource(
        123, backend=Idle(), timeout_s=0.2, clock=lambda: clock.t, silence_on_timeout=True
    )
    assert np.array_equal(quiet.read(4), np.zeros(4, np.float32))
    quiet.cancel()
    assert len(quiet.read(4)) == 0


def test_wasapi_keeps_partial_stereo_packets():
    pcm = np.asarray([[16384, 16384], [16384, 16384]], dtype="<i2").tobytes()
    packets = deque([pcm[:4], pcm[4:]])
    backend = SimpleNamespace(read_packet=lambda: packets.popleft())
    source = ProcessLoopbackSource(123, backend=backend)
    assert source.read(1).tolist() == [0.5]
    assert to_mono_22050(pcm).tolist() == [0.5]


class BlockingSource:
    sample_rate = 22050
    name = "线程测试设备"

    def __init__(self):
        self.entered, self.cancelled, self.closed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        self.owners = []

    def __enter__(self):
        self.owners.append(threading.get_ident())
        self.entered.set()
        return self

    def read(self, _count):
        self.owners.append(threading.get_ident())
        self.cancelled.wait(3.0)
        return np.zeros(0, np.float32)

    def cancel(self):
        self.cancelled.set()

    def __exit__(self, *exc):
        self.owners.append(threading.get_ident())
        self.closed.set()


def test_audio_open_read_close_share_one_thread_and_read_is_nonblocking():
    inner = BlockingSource()
    source = ThreadedSource(inner)
    with source:
        assert inner.entered.is_set()
        started = time.monotonic()
        assert len(source.read(1024)) == 0
        assert time.monotonic() - started < 0.1
    assert inner.closed.is_set()
    assert len(set(inner.owners)) == 1 and inner.owners[0] != threading.get_ident()
    assert not source._thread.is_alive()


def test_threaded_audio_buffer_is_bounded_and_eof_waits_for_draining():
    class Burst:
        sample_rate = 10
        exhausted = False

        def __enter__(self):
            return self

        def read(self, _count):
            self.exhausted = True
            return np.arange(100, dtype=np.float32)

        def __exit__(self, *exc):
            pass

    source = ThreadedSource(Burst(), max_s=2)
    with source:
        source._thread.join(1)
        assert not source.exhausted
        assert source.read(20).tolist() == list(range(80, 100))
        assert source.exhausted


def test_meter_failure_is_reported_without_unhandled_thread_error(monkeypatch):
    failures = []
    monkeypatch.setattr(threading, "excepthook", lambda args: failures.append(args.exc_value))

    class Failed(BlockingSource):
        def __enter__(self):
            raise AudioUnavailable("模拟设备打开失败")

    p = Presenter(
        list_devices=lambda: [],
        find_window=lambda _: None,
        open_capture=lambda *args, **kw: Failed(),
    )
    p.start_meter("test")
    p._meter.thread.join(2)
    view = p.poll()
    assert not view.listening and "模拟设备打开失败" in view.error
    assert any(isinstance(item, WorkerFailed) for item in view.log_events)
    assert not failures
    p.shutdown()


def test_audio_fallback_reports_actual_device_and_closes_failed_primary():
    from wowfish.audio.devices import FallbackSource

    calls = []

    class Source:
        sample_rate = 22050

        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        def __enter__(self):
            calls.append((self.name, "open", threading.get_ident()))
            if self.fail:
                raise AudioUnavailable("此系统不支持进程捕获")
            return self

        def __exit__(self, *exc):
            calls.append((self.name, "close", threading.get_ident()))

        def read(self, count):
            return np.zeros(0, np.float32)

    fallback = FallbackSource(lambda: Source("进程", True), lambda: Source("扬声器"))
    with ThreadedSource(fallback) as opened:
        assert opened.name == "扬声器"
        assert "此系统不支持" in opened.fallback_reason
    assert [entry[:2] for entry in calls] == [
        ("进程", "open"),
        ("进程", "close"),
        ("扬声器", "open"),
        ("扬声器", "close"),
    ]
    assert len({owner for _, _, owner in calls}) == 1


def test_cancel_verification_closes_its_blocking_reader_promptly():
    inner = BlockingSource()
    p = Presenter(
        list_devices=lambda: [], find_window=lambda _: None, open_capture=lambda *args, **kw: inner
    )
    p.record_and_verify(60.0, "test")
    assert inner.entered.wait(2)
    p.cancel_workers(wait=True)
    assert inner.closed.wait(1) and not p.working
    assert not any(isinstance(x, WorkerFailed) for x in p.poll().log_events)
    p.shutdown()


def test_cancellation_prevents_overlapping_calibration_tasks():
    started = threading.Event()
    p = Presenter(list_devices=lambda: [], find_window=lambda _: None)

    def wait_for_cancel(task):
        started.set()
        task.stop.wait(3)

    p._launch("calibrate", wait_for_cancel)
    assert started.wait(1)
    p._launch("preview", wait_for_cancel)
    assert any(isinstance(item, WorkerFailed) for item in p.poll().log_events)
    p.cancel_workers(wait=True)
    assert not p.working
    p.shutdown()


def test_reconnection_budget_is_not_reset_by_empty_reads():
    class Flapping(FakeIO):
        def __init__(self):
            super().__init__()
            self.reads = self.restarts = 0

        def read(self, count):
            self.reads += 1
            if self.reads % 2:
                raise RuntimeError("设备断开")
            return np.zeros(0, np.float32)

        def restart(self):
            self.restarts += 1

    io = Flapping()
    e = engine(io)
    e.state, e._icon_xy = State.WAIT_BITE, (40, 30)
    for _ in range(7):
        e.step()
    assert io.restarts == 3 and e.state is State.STOPPED


def test_streaming_recording_does_not_accumulate_samples(tmp_path):
    samples = np.asarray([0.5, -0.5, 0.25, -0.25], np.float32)
    references = []

    class Repeating:
        sample_rate = 22050
        name = "测试音频"

        def __enter__(self):
            return self

        def read(self, count):
            chunk = np.tile(samples, 1024)
            references.append(weakref.ref(chunk))
            return chunk

        def __exit__(self, *exc):
            pass

    path = tmp_path / "中文录音.wav"
    with RecordingSource(Repeating(), path) as source:
        for _ in range(256):
            source.read(4096)
        assert path.stat().st_size > 1_000_000
        assert all(reference() is None for reference in references)
    saved, rate = read_wav(path)
    assert rate == 22050 and len(saved) == 256 * 4096
    np.testing.assert_allclose(saved[:4], samples, atol=1 / 32767)


@pytest.mark.parametrize("prefer_new", [True, False])
def test_legacy_home_fallback_and_preferred_home(tmp_path, monkeypatch, prefer_new):
    from wowfish import paths

    legacy, current = tmp_path / "旧目录", tmp_path / "新目录"
    monkeypatch.setenv("WOWFISH_HOME", str(legacy))
    if prefer_new:
        monkeypatch.setenv("FOREVERFISH_HOME", str(current))
    else:
        monkeypatch.delenv("FOREVERFISH_HOME")
    wanted = current if prefer_new else legacy
    assert paths.config_file() == wanted / "config" / "config.toml"
    assert ensure_config_file()


def test_visual_confirmation_rejects_other_sound_before_interacting():
    io = FakeIO()
    verifier = SimpleNamespace(confirmed=lambda _: False)
    e = engine(io, bite_verifier=verifier)
    e.state, e._icon_xy = State.WAIT_BITE, (40, 30)
    event = BiteEvent("fishbite", 0.9, 0.0)
    e._wait_bite(0.0, [event])
    assert io.pressed == [] and e.state is State.WAIT_BITE
    e._wait_bite(0.7, [])
    assert any(isinstance(item, ev.BiteRejected) for item in e.events.queue)
    assert io.pressed == []
    verifier.confirmed = lambda _: True
    e._wait_bite(0.8, [event])
    assert io.pressed == ["f"] and e.state is State.INTERACT


def test_visual_sampling_ignores_changes_outside_own_bobber_crop():
    io = FakeIO(WindowRef("魔兽世界", 123, 0, 0, 200, 160, 456))
    verifier = VisualBiteVerifier(8)
    e = engine(io, bite_verifier=verifier)
    e._icon_xy, e._icon_size = (80, 40), 18
    e._observe_bobber(0)
    io.frame[20:50, 150:180] = 200
    e._observe_bobber(0.2)
    assert not verifier.confirmed(0.2)
    io.frame[50:80, 60:90] = 200
    e._observe_bobber(0.4)
    assert verifier.confirmed(0.4)
    assert not verifier.confirmed(2.1)


@pytest.mark.parametrize("size", [12, 14, 16, 18])
def test_small_icons_keep_detail_on_wide_windows(size):
    templates = load_icon_templates()
    template = template_for_size(templates, size)
    frame = np.full((180, 3840, 3), (90, 60, 30), np.uint8)
    x, y = 1200, 80
    frame[y : y + size, x : x + size][template.mask > 0] = template.bgr[template.mask > 0]
    matcher = IconMatcher(template, match_width=1920)
    match = matcher.match(frame)
    assert matcher.template_px(3840) >= 12
    assert match.accepted(0.75), match
    assert abs(match.x - (x + size / 2)) <= 2


def test_manual_search_region_excludes_other_icons_and_persists_tracking():
    template = template_for_size(load_icon_templates(), 29)
    frame = np.full((240, 640, 3), (90, 60, 30), np.uint8)
    for x in (80, 450):
        frame[100:129, x : x + 29][template.mask > 0] = template.bgr[template.mask > 0]
    region = Region(0.5, 0.1, 0.9, 0.9)
    search = IconSearch(IconMatcher(template), 0.75, allowed_region=region)
    for _ in range(2):
        _phase, found = search.step(frame)
        assert found.accepted(0.75) and found.x > 400
        assert search.region.left >= region.left and search.region.right <= region.right


def test_calibration_can_cancel_between_matches():
    frame = np.zeros((100, 100, 3), np.uint8)
    with pytest.raises(InterruptedError):
        calibrate_icon_scale(frame, load_icon_templates(), cancel=lambda: True)


def test_short_recording_returns_no_bites_instead_of_crashing():
    fingerprints = build_fingerprints()
    scores = score_recording(np.zeros(10, np.float32), 22050, fingerprints)
    report = bite_report(scores, fingerprints, DEFAULT_MEL)
    assert not report.stands_out
    assert all(peak.score == -1 for peak in clip_peaks(scores, fingerprints, DEFAULT_MEL))


def test_chinese_image_paths_roundtrip(tmp_path):
    path = tmp_path / "中文目录" / "浮漂截图.png"
    frame = np.arange(12 * 15 * 3, dtype=np.uint8).reshape(12, 15, 3)
    assert write_image(path, frame)
    assert np.array_equal(read_image(path), frame)


def test_windows_loopback_mixes_channels_instead_of_requesting_mono(monkeypatch):
    import wowfish.audio._soundcard as module

    requested = []

    class Recorder:
        def __enter__(self):
            return self

        def record(self, numframes):
            return np.asarray([[0.5, -0.5], [0.8, 0.2]], np.float32)

        def __exit__(self, *exc):
            pass

    mic = SimpleNamespace(name="扬声器", recorder=lambda **kw: requested.append(kw) or Recorder())
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module, "ComApartment", nullcontext)
    with module.SoundcardSource(mic) as source:
        np.testing.assert_allclose(source.read(2), [0, 0.5])
    assert requested[0]["channels"] is None


def test_hotkeys_stay_on_owner_thread_and_dispatch_on_caller():
    messages, registrations, unregistered = queue.Queue(), [], []

    class Backend:
        def RegisterHotKey(self, hwnd, ident, flags, vk):
            registrations.append((ident, flags, threading.get_ident()))
            return True

        def UnregisterHotKey(self, hwnd, ident):
            unregistered.append((ident, threading.get_ident()))

        def PeekMessageW(self, pointer, hwnd, lo, hi, flags):
            try:
                pointer._obj.wParam = messages.get_nowait()
            except queue.Empty:
                return False
            return True

    delivered = []
    manager = HotkeyManager(
        lambda: delivered.append((HOTKEY_PAUSE, threading.get_ident())),
        lambda: delivered.append((HOTKEY_STOP, threading.get_ident())),
        backend=Backend(),
    )
    try:
        assert manager.sync("f8", "f9") == ""
        messages.put(HOTKEY_PAUSE)
        messages.put(HOTKEY_STOP)
        deadline = time.monotonic() + 1
        while len(delivered) < 2 and time.monotonic() < deadline:
            manager.poll()
            time.sleep(0.01)
        assert [ident for ident, _ in delivered] == [HOTKEY_PAUSE, HOTKEY_STOP]
        assert all(owner == threading.get_ident() for _, owner in delivered)
        assert all(flags == MOD_NOREPEAT for _, flags, _ in registrations)
    finally:
        manager.close()
    assert len(unregistered) == 2
    assert {owner for _, owner in unregistered} == {owner for _, _, owner in registrations}
    assert next(iter({owner for _, _, owner in registrations})) != threading.get_ident()


def test_hotkey_conflict_is_visible_and_retry_uses_new_keys():
    backend = SimpleNamespace(
        RegisterHotKey=lambda *args: False,
        UnregisterHotKey=lambda *args: None,
        PeekMessageW=lambda *args: False,
    )
    manager = HotkeyManager(lambda: None, lambda: None, backend=backend)
    try:
        assert "F8" in manager.sync("f8", "")
        assert "F10" in manager.sync("f10", "")
    finally:
        manager.close()
