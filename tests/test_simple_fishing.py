"""Exercise the presence loop with real gear templates and bundled bite sounds."""

from __future__ import annotations

import tomllib
from dataclasses import replace

import numpy as np
import pytest

from wowfish.assets import CLIP_NAMES
from wowfish.audio.clips import load_reference_clips
from wowfish.audio.detector import BiteDetector, BiteEvent
from wowfish.capture import WindowRef
from wowfish.config import (
    AudioConfig,
    BehaviorConfig,
    Config,
    GameConfig,
    RunConfig,
    VisionConfig,
    load_config,
)
from wowfish.engine.engine import FishingEngine
from wowfish.engine.state import State
from wowfish.vision.calibrate import Calibration
from wowfish.vision.matcher import IconMatcher
from wowfish.vision.template import load_icon_templates, template_for_size


class Pond:
    sample_rate = 22050

    def __init__(self):
        self.window = WindowRef("魔兽世界", 123, 0, 0, 320, 240, 456)
        self.t = 0.0
        self.focused = True
        self.pressed = []
        self.captured = []
        self.pending = np.zeros(0, np.float32)
        self.templates = load_icon_templates()
        self.water = np.full((240, 320, 3), (90, 60, 30), np.uint8)
        self.frame = self.water.copy()

    def icon(self, x=70, y=60):
        self.frame = self.water.copy()
        template = template_for_size(self.templates, 29)
        patch = self.frame[y : y + 29, x : x + 29]
        patch[template.mask > 0] = template.bgr[template.mask > 0]

    def grab(self, box=None):
        assert box is None, "Only gear presence is needed, without a splash crop"
        self.captured.append(self.t)
        return self.frame.copy()

    def is_game_focused(self, _window):
        return self.focused

    def press_key(self, key, _hold):
        self.pressed.append((self.t, key))

    def read(self, _count):
        samples, self.pending = self.pending, np.zeros(0, np.float32)
        return samples

    def clear(self):
        self.pending = np.zeros(0, np.float32)

    def reset(self):
        pass

    def feed(self, _chunk):
        return []

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def fishing(pond, detector=None, cfg=None):
    cfg = cfg or Config(
        GameConfig(),
        VisionConfig(icon_size_px=29),
        AudioConfig(),
        RunConfig(pause_min_s=0, pause_max_s=0),
    )

    def matcher(size):
        return IconMatcher(template_for_size(pond.templates, size))

    return FishingEngine(
        cfg,
        pond.window,
        capturer=pond,
        input_device=pond,
        audio_source=pond,
        detector=detector or pond,
        matcher=matcher(29),
        matcher_factory=matcher,
        calibrate=lambda *_args, **_kwargs: Calibration(29, 29, 0.0, 0, 0),
        clock=pond.clock,
        sleep=pond.sleep,
    )


def step_at(engine, pond, seconds):
    pond.t = seconds
    engine.step()


def test_missing_gear_casts_once_and_waits_for_landing():
    pond = Pond()
    engine = fishing(pond)
    engine.step()
    assert pond.pressed == [(0.0, "1")] and engine.state is State.CASTING
    pond.icon()
    step_at(engine, pond, 1.4)
    assert engine.state is State.CASTING and pond.captured == [0.0]
    step_at(engine, pond, 1.5)
    assert engine.state is State.WAIT_BITE and engine.stats.casts == 1


def test_existing_and_moving_gears_wait_until_the_no_sound_deadline():
    pond = Pond()
    pond.icon()
    engine = fishing(pond)
    engine.step()
    for at, x in [(5.0, 220), (15.0, 20), (29.0, 170)]:
        pond.icon(x, 140)
        step_at(engine, pond, at)
        assert engine.state is State.WAIT_BITE
    assert pond.pressed == [] and engine.stats.casts == engine.stats.misses == 0
    step_at(engine, pond, 30.0)
    assert pond.pressed == [(30.0, "1")], "A gear must not veto the no-sound recast"
    assert engine.state is State.CASTING and engine.stats.misses == engine.stats.casts == 1


@pytest.mark.parametrize("gear_lingers", [False, True])
@pytest.mark.parametrize("pause_s", [None, 60.0])
def test_reeling_continues_after_pickup_even_with_a_lingering_gear_or_old_pause(
    gear_lingers, pause_s
):
    pond = Pond()
    pond.icon()
    run = RunConfig() if pause_s is None else RunConfig(pause_min_s=pause_s, pause_max_s=pause_s)
    engine = fishing(
        pond, cfg=Config(GameConfig(), VisionConfig(icon_size_px=29), AudioConfig(), run)
    )
    engine.step()
    pond.t = 5.0
    engine._wait_bite(pond.t, [BiteEvent("fishbite", 0.9, 4.0)])
    if not gear_lingers:
        pond.frame = pond.water.copy()
    for at in (5.02, 5.25, 5.49):
        step_at(engine, pond, at)
        assert pond.pressed == [(5.0, "f")], "Allow the game to finish picking up the fish"
    step_at(engine, pond, 5.5)
    assert pond.pressed == [(5.0, "f"), (5.5, "1")]
    assert engine.state is State.CASTING
    assert engine.stats.catches == int(not gear_lingers) and engine.stats.misses == 0
    step_at(engine, pond, 5.52)
    assert engine.stats.casts == 1, "Complete each reel only once"


def test_pickup_wait_starts_after_the_interact_key_finishes_sending():
    pond = Pond()
    pond.icon()
    original_press = pond.press_key

    def delayed_press(key, hold):
        original_press(key, hold)
        pond.t += 0.4

    pond.press_key = delayed_press
    engine = fishing(pond)
    engine.step()
    pond.t = 5.0
    engine._wait_bite(pond.t, [BiteEvent("fishbite", 0.9, 4.0)])
    step_at(engine, pond, 5.89)
    assert pond.pressed == [(5.0, "f")]
    step_at(engine, pond, 5.91)
    assert pond.pressed == [(5.0, "f"), (5.91, "1")]
    assert engine.stats.misses == 0


def test_pickup_deadline_does_not_wait_for_another_screenshot():
    pond = Pond()
    pond.icon()
    engine = fishing(pond)
    engine.step()
    pond.t = 5.0
    engine._wait_bite(pond.t, [BiteEvent("fishbite", 0.9, 4.0)])

    def no_more_captures(*_args):
        pytest.fail("The pickup deadline must deliver the next cast without another scan")

    pond.grab = no_more_captures
    step_at(engine, pond, 5.5)
    assert pond.pressed == [(5.0, "f"), (5.5, "1")]
    assert engine.stats.misses == engine.stats.catches == 0


def test_duplicate_sounds_during_pickup_cannot_delay_the_next_cast():
    pond = Pond()
    pond.icon()
    engine = fishing(pond)
    engine.step()
    pond.t = 5.0
    sound = BiteEvent("fishbite", 0.9, 4.0)
    engine._wait_bite(pond.t, [sound])
    pond.feed = lambda _chunk: [sound]
    for at in (5.2, 5.4, 5.5):
        pond.pending = np.zeros(1024, np.float32)
        step_at(engine, pond, at)
    assert pond.pressed == [(5.0, "f"), (5.5, "1")]
    assert engine.stats.bites_heard == 1 and engine.stats.misses == 0


@pytest.mark.parametrize("interruption", ["pause", "focus", "stop"])
def test_pickup_wait_obeys_interruptions_and_resume_rechecks_presence(interruption):
    pond = Pond()
    pond.icon()
    engine = fishing(pond)
    engine.step()
    pond.t = 5.0
    engine._wait_bite(pond.t, [BiteEvent("fishbite", 0.9, 4.0)])
    if interruption == "pause":
        engine.pause()
    elif interruption == "focus":
        pond.focused = False
    else:
        engine.stop()
    step_at(engine, pond, 5.2)
    step_at(engine, pond, 5.6)
    assert pond.pressed == [(5.0, "f")]
    if interruption != "stop":
        pond.focused = True
        engine.resume()
        step_at(engine, pond, 6.0)
        assert engine.state is State.WAIT_BITE and pond.pressed == [(5.0, "f")]


def test_an_explicit_break_still_happens_between_pickup_and_the_next_cast():
    pond = Pond()
    pond.icon()
    cfg = Config(
        GameConfig(),
        VisionConfig(icon_size_px=29),
        AudioConfig(),
        behavior=BehaviorConfig(
            break_every_min_minutes=0.05,
            break_every_max_minutes=0.05,
            break_min_minutes=0.05,
            break_max_minutes=0.05,
        ),
    )
    engine = fishing(pond, cfg=cfg)
    engine.step()
    pond.t = 5.0
    engine._wait_bite(pond.t, [BiteEvent("fishbite", 0.9, 4.0)])
    step_at(engine, pond, 5.5)
    assert pond.pressed == [(5.0, "f"), (8.5, "1")]
    assert engine.state is State.CASTING and engine.stats.misses == 0


def test_a_due_lure_is_applied_after_pickup_without_waiting_for_the_old_gear():
    pond = Pond()
    pond.icon()
    cfg = Config(GameConfig(lure_key="2"), VisionConfig(icon_size_px=29), AudioConfig())
    engine = fishing(pond, cfg=cfg)
    engine.step()
    pond.t = 5.0
    engine._wait_bite(pond.t, [BiteEvent("fishbite", 0.9, 4.0)])
    step_at(engine, pond, 5.5)
    assert pond.pressed == [(5.0, "f"), (5.5, "2")]
    step_at(engine, pond, 11.52)
    assert pond.pressed[-1] == (11.52, "1")
    assert engine.state is State.CASTING and engine.stats.misses == 0


@pytest.mark.parametrize("clip", CLIP_NAMES)
def test_bundled_sound_reels_on_the_detection_tick_with_an_unchanged_picture(clip):
    from wowfish.audio.clips import build_fingerprints

    pond = Pond()
    pond.icon()
    heard = []

    class Detector(BiteDetector):
        def feed(self, chunk):
            bites = super().feed(chunk)
            heard.extend((pond.t, bite) for bite in bites)
            return bites

    detector = Detector(build_fingerprints())
    engine = fishing(pond, detector)
    engine.step()
    samples = load_reference_clips()[clip]
    signal = np.concatenate([np.zeros(4312, np.float32), samples, np.zeros(4 * pond.sample_rate)])
    for offset in range(0, len(signal), 1024):
        pond.pending = signal[offset : offset + 1024]
        step_at(engine, pond, 5.0 + (offset + len(pond.pending)) / pond.sample_rate)
    assert len(heard) == 1 and heard[0][1].clip == clip
    assert pond.pressed[0] == (heard[0][0], "f"), "The sound must press the key immediately"
    assert [key for _, key in pond.pressed] == ["f", "1"]
    assert 0.5 <= pond.pressed[1][0] - pond.pressed[0][0] < 0.5 + 1024 / pond.sample_rate
    assert heard[0][0] not in pond.captured, "The detection tick must skip image work"
    assert engine.state is State.WAIT_BITE and engine.stats.catches == engine.stats.misses == 0


def test_the_next_cast_starts_its_own_no_sound_timer_after_pickup():
    pond = Pond()
    pond.icon()
    engine = fishing(pond)
    engine.step()
    pond.t = 5.0
    engine._wait_bite(pond.t, [BiteEvent("fishbite", 0.9, 4.0)])
    step_at(engine, pond, 5.5)
    step_at(engine, pond, 7.0)
    deadline = 35.5
    assert [key for _, key in pond.pressed] == ["f", "1"]
    assert engine.stats.catches == engine.stats.misses == 0
    step_at(engine, pond, 35.49)
    assert [key for _, key in pond.pressed] == ["f", "1"]
    step_at(engine, pond, deadline)
    assert pond.pressed[-1] == (deadline, "1") and engine.stats.misses == 1


def test_sustained_absence_before_a_bite_ends_a_cast_but_brief_flicker_does_not():
    pond = Pond()
    pond.icon()
    engine = fishing(pond)
    engine.step()
    pond.frame = pond.water.copy()
    for at in (2.0, 2.25, 2.5):
        step_at(engine, pond, at)
    assert engine.stats.catches == engine.stats.misses == 0
    pond.icon()
    step_at(engine, pond, 2.75)
    pond.frame = pond.water.copy()
    for at in (3.0, 3.25, 3.5, 3.75, 4.0):
        step_at(engine, pond, at)
    assert engine.state is State.IDLE
    assert engine.stats.catches == 0 and engine.stats.misses == 1
    step_at(engine, pond, 4.25)
    assert pond.pressed[-1] == (4.25, "1") and engine.stats.casts == 1


def test_sound_takes_priority_when_the_gear_is_disappearing():
    pond = Pond()
    pond.icon()
    engine = fishing(pond)
    engine.step()
    pond.frame = pond.water.copy()
    for at in (2.0, 2.25, 2.5):
        step_at(engine, pond, at)
    step_at(engine, pond, 2.75)
    pond.t = 3.0
    engine._wait_bite(pond.t, [BiteEvent("fishbite", 0.9, 2.0)])
    assert pond.pressed == [(3.0, "f")] and engine.state is State.INTERACT
    assert engine.stats.misses == engine.stats.catches == 0


@pytest.mark.parametrize("present", [False, True])
def test_resume_checks_presence_before_sending_any_cast(present):
    pond = Pond()
    pond.icon()
    engine = fishing(pond)
    engine.step()
    engine.pause()
    step_at(engine, pond, 2.0)
    assert engine.state is State.PAUSED
    if not present:
        pond.frame = pond.water.copy()
    engine.resume()
    step_at(engine, pond, 3.0)
    assert pond.pressed == ([] if present else [(3.0, "1")])
    assert engine.stats.misses == 0


def test_legacy_visual_timing_and_skip_settings_cannot_delay_reeling(tmp_path):
    path = tmp_path / "旧配置.toml"
    legacy = (
        '# 保留我的设置\n[game]\ninteract_key="g"\n'
        "[vision]\nconfirm_bite=true\nconfirm_window_s=2.0\nmotion_threshold=255.0\n"
        "[run]\ncast_timeout_s=30.0\nloot_timeout_s=0.1\nloot_confirm_s=10.0\n"
        "[behavior]\nreaction_min_ms=10000\nreaction_max_ms=10000\nskip_bite_chance=1.0\n"
    )
    path.write_text(legacy, encoding="utf-8")
    cfg, warnings = load_config(path)
    assert warnings == []
    pond = Pond()
    pond.icon()
    engine = fishing(pond, cfg=cfg)
    engine.step()
    step_at(engine, pond, 29.0)
    before = pond.t
    engine._wait_bite(before, [BiteEvent("fishbite", 0.9, 29.0)])
    assert pond.pressed == [(before, "g")] and pond.t == before

    from wowfish.config import update_config

    update_config(
        lambda current: replace(current, audio=replace(current.audio, device="耳机")), path
    )
    text = path.read_text(encoding="utf-8")
    assert "# 保留我的设置" in text
    raw = tomllib.loads(text)
    for section, values in tomllib.loads(legacy).items():
        assert all(raw[section][key] == value for key, value in values.items())
    assert load_config(path)[0].audio.device == "耳机"


def test_no_sound_deadline_applies_while_still_seeking_the_first_gear():
    pond = Pond()
    cfg = Config(
        GameConfig(),
        VisionConfig(icon_size_px=29),
        AudioConfig(),
        RunConfig(icon_timeout_s=60.0, pause_min_s=2.0, pause_max_s=2.0),
    )
    engine = fishing(pond, cfg=cfg)
    engine.step()
    for at in [1.5, 20.0, 29.99]:
        step_at(engine, pond, at)
        assert engine.state is State.CASTING
    captures = list(pond.captured)
    step_at(engine, pond, 30.0)
    assert pond.pressed == [(0.0, "1"), (30.0, "1")]
    assert pond.captured == captures, "The no-sound deadline must bypass icon acquisition"
    assert engine.stats.casts == 2 and engine.stats.misses == 1


def test_no_sound_deadline_uses_actual_cast_delivery_not_icon_acquisition():
    pond = Pond()
    original_press = pond.press_key

    def delayed_press(key, hold):
        original_press(key, hold)
        pond.t += 0.4

    pond.press_key = delayed_press
    engine = fishing(pond)
    engine.step()
    pond.icon()
    step_at(engine, pond, 1.8)
    assert engine.state is State.CASTING
    step_at(engine, pond, 2.0)
    step_at(engine, pond, 30.39)
    assert pond.pressed == [(0.0, "1")]
    step_at(engine, pond, 30.41)
    assert pond.pressed == [(0.0, "1"), (30.41, "1")]
    assert engine.stats.casts == 2 and engine.stats.misses == 1


def test_a_bite_on_the_no_sound_deadline_reels_instead_of_recasting():
    pond = Pond()
    pond.icon()
    engine = fishing(pond)
    engine.step()
    pond.t = 30.0
    engine._wait_bite(pond.t, [BiteEvent("fishbite", 0.9, 29.0)])
    assert pond.pressed == [(30.0, "f")] and engine.stats.misses == 0
    step_at(engine, pond, 30.49)
    assert len(pond.pressed) == 1
    step_at(engine, pond, 30.5)
    assert pond.pressed[-1] == (30.5, "1") and engine.stats.misses == 0


@pytest.mark.parametrize("clip", CLIP_NAMES)
def test_a_disappearing_gear_does_not_recast_before_the_real_sound_is_recognized(clip):
    from wowfish.audio.clips import build_fingerprints

    pond = Pond()
    pond.icon()
    detector = BiteDetector(build_fingerprints())
    engine = fishing(pond, detector)
    engine.step()
    pond.frame = pond.water.copy()
    engine._last_box_grab = 4.75
    signal = np.concatenate([load_reference_clips()[clip], np.zeros(2 * pond.sample_rate)])
    for offset in range(0, len(signal), 441):  # Realistic 20 ms audio batches.
        pond.pending = signal[offset : offset + 441]
        step_at(engine, pond, 5.0 + (offset + len(pond.pending)) / pond.sample_rate)
    assert pond.pressed[0][1] == "f", "Icon loss must not interrupt the 0.8 s sound fingerprint"
    assert [key for _, key in pond.pressed].count("f") == 1
    assert engine.stats.bites_heard == engine.stats.catches == 1
