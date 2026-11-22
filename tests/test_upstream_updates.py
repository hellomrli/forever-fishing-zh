"""Regressions for the upstream landing delay and process audio selection fixes."""

from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pytest

from wowfish.audio._pipewire import AppStream, list_app_streams, pick_stream
from wowfish.capture import WindowRef
from wowfish.config import AudioConfig, Config, GameConfig, VisionConfig
from wowfish.engine.engine import FishingEngine
from wowfish.engine.state import State
from wowfish.vision.calibrate import Calibration
from wowfish.vision.matcher import IconMatcher
from wowfish.vision.template import load_icon_templates, template_for_size


class CastScene:
    sample_rate = 22050

    def __init__(self):
        self.window = WindowRef("魔兽世界", 123, 0, 0, 320, 240, 456)
        self.t = 0.0
        self.pressed = []
        self.on_grab = lambda: None
        self.water = np.full((240, 320, 3), (90, 60, 30), np.uint8)
        self.frame = self.water.copy()
        self.templates = load_icon_templates()

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def is_game_focused(self, window):
        return True

    def press_key(self, key, hold_s):
        self.pressed.append(key)

    def grab(self, box=None):
        self.on_grab()
        if box is None:
            return self.frame.copy()
        x, y, width, height = box
        return self.frame[y : y + height, x : x + width].copy()

    def put_icon(self, x, y, size=29):
        template = template_for_size(self.templates, size)
        self.frame[y : y + size, x : x + size][template.mask > 0] = template.bgr[template.mask > 0]

    def read(self, count):
        return np.zeros(0, np.float32)

    def reset(self):
        pass


def fishing(scene, calibrate=None):
    cfg = Config(GameConfig(), VisionConfig(), AudioConfig())
    cfg = replace(cfg, vision=replace(cfg.vision, icon_size_px=29, confirm_bite=False))

    def matcher(size):
        return IconMatcher(template_for_size(scene.templates, size))

    return FishingEngine(
        cfg,
        scene.window,
        capturer=scene,
        input_device=scene,
        audio_source=scene,
        detector=scene,
        matcher=matcher(29),
        matcher_factory=matcher,
        calibrate=calibrate,
        clock=scene.clock,
        sleep=scene.sleep,
    )


def test_saved_size_waits_for_landing_and_rejects_a_gear_present_before_cast():
    scene = CastScene()
    scene.put_icon(70, 60)
    engine = fishing(scene)
    engine._sweeping = False  # A previously measured size takes the fast matching path.
    engine._cast(scene.t)
    scene.t = 0.2
    engine._acquire(scene.t)
    assert engine.state is State.CASTING
    scene.t = 1.5
    engine._acquire(scene.t)
    assert engine.state is State.CASTING, "An unchanged UI gear must not become the bobber"

    scene.frame = scene.water.copy()
    scene.put_icon(220, 140)
    scene.t = 1.7
    engine._acquire(scene.t)
    assert engine.state is State.WAIT_BITE
    assert abs(engine._icon_xy[0] - 234.5) <= 1
    assert engine.stats.casts == 1 and scene.pressed == ["1"]


def test_calibration_waits_for_landing_and_releases_the_precast_frame():
    scene = CastScene()
    calls = []

    def calibrate(frame, **kwargs):
        calls.append(scene.t)
        return Calibration(29, 29, 0.95, 120, 100, 0.5, 0.4)

    engine = fishing(scene, calibrate)
    engine._cast(scene.t)
    scene.t = 1.4
    engine._acquire(scene.t)
    assert calls == [] and engine.state is State.CASTING
    scene.t = 1.5
    engine._acquire(scene.t)
    assert calls == [1.5] and engine.state is State.WAIT_BITE
    assert engine._before_cast is None


def test_a_short_icon_timeout_still_expires_during_the_landing_wait():
    scene = CastScene()
    engine = fishing(scene, lambda *args, **kwargs: pytest.fail("Calibration ran before landing"))
    engine.cfg = replace(engine.cfg, run=replace(engine.cfg.run, icon_timeout_s=0.5))
    engine._cast(scene.t)
    scene.t = 0.6
    engine._acquire(scene.t)
    assert engine.state is State.IDLE and engine.stats.misses == 1
    assert engine._before_cast is None


def test_pausing_during_the_precast_screenshot_prevents_the_cast():
    scene = CastScene()
    engine = fishing(scene)
    scene.on_grab = engine.pause
    engine._cast(scene.t)
    assert scene.pressed == [] and engine.stats.casts == 0
    assert engine._before_cast is None


def test_native_pipewire_nodes_inherit_the_client_pid():
    objects = [
        {
            "id": 7,
            "type": "PipeWire:Interface:Client",
            "info": {"props": {"pipewire.sec.pid": 321}},
        },
        {
            "info": {
                "props": {
                    "client.id": "7",
                    "object.serial": 10,
                    "media.class": "Stream/Output/Audio",
                    "application.name": "魔兽世界",
                }
            }
        },
        {
            "info": {
                "props": {
                    "client.id": 7,
                    "object.serial": 11,
                    "media.class": "Stream/Output/Audio",
                    "application.process.id": "654",
                }
            }
        },
    ]
    streams = list_app_streams(dump=lambda: json.dumps(objects))
    assert [(stream.serial, stream.pid) for stream in streams] == [(10, 321), (11, 654)]


def test_pipewire_prefers_the_selected_pid_then_its_children():
    child = AppStream(1, "魔兽世界", 43, "wow.exe")
    direct = AppStream(2, "魔兽世界", 42, "wow.exe")
    unrelated = AppStream(3, "魔兽世界", 99, "wow.exe")
    parents = {43: 44, 44: 42, 99: 1, 42: 1}
    assert pick_stream([child, direct], pid=42, name="魔兽世界", parent_of=parents.get) == direct
    assert pick_stream([unrelated, child], pid=42, name="魔兽世界", parent_of=parents.get) == child
    assert pick_stream([unrelated], pid=42, name="魔兽世界", parent_of=parents.get) is None


def test_pipewire_parent_cycles_do_not_hang_or_select_another_client():
    stream = AppStream(1, "魔兽世界", 43, "wow.exe")
    parents = {43: 44, 44: 43}
    assert pick_stream([stream], pid=42, name="魔兽世界", parent_of=parents.get) is None
