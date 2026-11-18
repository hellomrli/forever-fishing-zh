"""The fishing loop. One `step()` per iteration; every collaborator is injectable."""

from __future__ import annotations

import math
import queue
import random
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from wowfish.audio import AudioChunk
from wowfish.audio._threaded import ThreadedSource
from wowfish.capture import (
    Box,
    WindowRef,
    find_window,
    get_capturer,
    linux_hyprland,
    linux_x11,
    macos,
    refresh_window,
)
from wowfish.config import Config
from wowfish.engine import events as ev
from wowfish.engine.behavior import Behavior
from wowfish.engine.state import State
from wowfish.messages import msg
from wowfish.vision.bite import VisualBiteVerifier
from wowfish.vision.matcher import IconMatch
from wowfish.vision.region import Region
from wowfish.vision.search import IconSearch

UNFOCUSED = msg("unfocused")
THUMB_WIDTH = 480
AUDIO_BLOCK = 1024
AUDIO_MAX_S = 2.0  # never read more than this at once; the device buffer may drop older audio
STATS_EVERY_S = 10.0
LISTEN_EVERY_S = 2.0  # while waiting for the splash, say what is heard this often
SCORES_EVERY_S = 0.1  # ...and send each clip's peak score this often, for the GUI's graph
SLEEP_SLICE_S = 0.1
RESWEEP_AFTER_TIMEOUTS = 2  # icon timeouts in a row before the next cast measures the icon again
JUMP_LANDING_S = (1.0, 1.5)  # Fishing cannot be cast mid-air; wait this long after a jump
QUIT_WAIT_S = 30.0  # away from an inn, /quit counts down 20 s before the game closes
QUIT_TRIES = 3  # quit key presses before giving up on closing the game
# Casts in a row that heard a splash, held it back on the picture alone and never reeled in.
# After this many the picture check has cost more fish than it can save: reel in on sound.
VISUAL_MISSES_BEFORE_BYPASS = 2
BUSY = (State.CASTING, State.WAIT_BITE, State.INTERACT)  # a cast in hand


class FishingEngine:
    def __init__(
        self,
        cfg: Config,
        window: WindowRef,
        *,
        capturer: Any,
        input_device: Any,
        audio_source: Any,
        detector: Any,
        matcher: Any = None,
        matcher_factory: Callable[[int], Any] | None = None,
        calibrate: Callable[..., Any] | None = None,
        on_calibrated: Callable[[Any], None] | None = None,
        find_window: Callable[..., WindowRef | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        session_s: float | None = None,
        box_interval_s: float = 0.25,
        lost_after: int = 3,
        bite_verifier: Any = None,
    ) -> None:
        self.cfg = cfg
        self.window = window
        self.capturer = capturer
        self.input = input_device
        self.audio = audio_source
        self.detector = detector
        self._matcher_factory = matcher_factory or (lambda size: matcher)
        self._calibrate = calibrate or (
            lambda frame, **kw: _real_calibrate(frame, cancel=self._stop_event.is_set, **kw)
        )
        self._on_calibrated = on_calibrated or (lambda cal: None)
        self._find_window = find_window
        self.threshold = cfg.vision.icon_threshold
        # The saved size (if any) is only a first guess: every run measures the icon on its
        # first cast, since resolution and UI scale change the icon's size on screen.
        self.matcher = matcher if matcher is not None else None
        if self.matcher is None and cfg.vision.icon_size_px > 0:
            self.matcher = self._matcher_factory(cfg.vision.icon_size_px)
        self.search = IconSearch(self.matcher, self.threshold) if self.matcher else None
        self._sweeping = True
        self._icon_timeouts = 0
        self._clock = clock
        self._sleep = sleep
        self._real_sleep = sleep is time.sleep
        self._stop_event = threading.Event()
        self._user_paused = threading.Event()
        self._rng = rng or random.Random()
        self.behavior = Behavior(cfg.behavior, cfg.run.key_hold_ms, self._rng)
        self.session_s = cfg.game.session_minutes * 60.0 if session_s is None else session_s
        self.box_interval_s = box_interval_s
        self.lost_after = lost_after

        self.events: queue.Queue[ev.Event] = queue.Queue()
        self.state = State.IDLE
        self.stats = ev.Stats(0.0)
        self.started: float | None = None
        self._stop = False
        self._resume_state = State.IDLE
        self._t_cast = 0.0
        self._t_interact = 0.0
        self._icon_xy: tuple[int, int] | None = None
        self._icon_size = 0
        self._misses = 0
        self._last_box_grab = -math.inf
        self._last_audio: float | None = None
        self._audio_frames = 0
        self._last_stats = 0.0
        self._last_listen = 0.0
        self._last_scores_sent = 0.0
        self._best_heard: tuple[str, float] = ("", -1.0)  # best clip score since the icon was found
        self._lure_at: float | None = None  # when the lure last finished applying
        self._last_window_refresh = -math.inf
        self._disappeared_since: float | None = None
        self._last_visual = -math.inf
        self._candidate: tuple[Any, float] | None = None
        self.verifier = bite_verifier or (
            VisualBiteVerifier(cfg.vision.motion_threshold) if cfg.vision.confirm_bite else None
        )
        self._bite_rejected = False  # this cast heard a splash the picture did not confirm
        self._visual_misses = 0  # such casts in a row that ended without reeling in
        self._audio_retries = 0
        self._audio_stable_since: float | None = None
        self._last_acquire = -math.inf

    # -- public ------------------------------------------------------------------------------

    def stop(self) -> None:
        self._stop = True
        self._stop_event.set()
        cancel = getattr(self.audio, "cancel", None)
        if cancel is not None:
            cancel()

    def pause(self) -> None:
        self._user_paused.set()

    def resume(self) -> None:
        self._user_paused.clear()

    def toggle_pause(self) -> None:
        if self._user_paused.is_set():
            self.resume()
        else:
            self.pause()

    def run(self) -> None:
        try:
            if self._stop:
                self._finish("user")
                return
            self.capturer.open(self.window)
            with self.audio:
                self._emit(
                    ev.AudioOpened(
                        self._t(),
                        getattr(self.audio, "name", "音频设备"),
                        getattr(self.audio, "fallback_reason", ""),
                    )
                )
                while self.state is not State.STOPPED:
                    self.step()
        finally:
            self.capturer.close()
            close_input = getattr(self.input, "close", None)  # e.g. a Wayland virtual keyboard
            if close_input is not None:
                close_input()

    def step(self) -> None:
        if self.state is State.STOPPED:
            return
        now = self._clock()
        if self.started is None:
            self.started = now
            self._last_audio = now
            self.behavior.start(now)
        if self._stop:
            self._finish("user")
            return
        if self._time_up() and not (self.cfg.game.quit_key and self.state in BUSY):
            # with a quit key, the cast in hand finishes first (no new one starts): the quit
            # comes between casts, not mid-cast or while looting
            self._quit_game()
            self._finish("user" if self._stop else "session limit")
            return
        try:
            self._refresh_window(now)
            if self.state is State.STOPPED:
                return
            self._step(self._clock())
        except Exception as e:  # a collaborator broke; say so and stop rather than spin
            if self._stop:
                self._finish("user")
                return
            self._emit(ev.Error(self._t(), f"{type(e).__name__}: {e}"))
            self._finish("error")
        if self.state is not State.STOPPED and not self._stop:
            self._wait(max(0.0, self.cfg.run.tick_interval_s - (self._clock() - now)))

    # -- the loop body -----------------------------------------------------------------------

    def _pause_if_needed(self) -> bool:
        focused = self.input.is_game_focused(self.window)
        if focused is False or self.window.minimized or self._user_paused.is_set():
            if self.state is not State.PAUSED:
                self._resume_state = self.state
                self.state = State.PAUSED
                reason = msg("user_pause") if self._user_paused.is_set() else UNFOCUSED
                self._emit(ev.Paused(self._t(), reason))
            return True
        return False

    def _step(self, now: float) -> None:
        if self._pause_if_needed():
            self._drain_audio(now)
            return
        if self.state is State.PAUSED:
            if self._resume_state in BUSY:
                self.stats = replace(self.stats, misses=self.stats.misses + 1)
            self._icon_xy = None
            self._candidate = None
            self.detector.reset()
            self.state = State.IDLE
            self._emit(ev.Resumed(self._t(), self.state))

        chunks = self._drain_audio(now)
        bites = []
        if self.state is State.WAIT_BITE:
            self._observe_bobber(now)
            for chunk in chunks:
                bites.extend(self.detector.feed(chunk))
            self._note_scores()
            self._send_scores(now, flush=bool(bites))  # flush so the graph gets the bite's peak

        if self.state is State.IDLE:
            self._cast(now)
        elif self.state is State.CASTING:
            self._acquire(now)
        elif self.state is State.WAIT_BITE:
            self._wait_bite(now, bites)
        elif self.state is State.INTERACT:
            self._after_interact(now)
        self._maybe_stats(now)

    def _cast(self, now: float) -> None:
        if self._lure_due(now):
            self._apply_lure()
            return  # still IDLE: the next step casts
        if self.behavior.jump():
            self._press(self.cfg.behavior.jump_key, "jump")
            if not self._wait(self._rng.uniform(*JUMP_LANDING_S), watch_focus=True):
                return
            now = self._clock()
        key = self.cfg.game.cast_key
        clear = getattr(self.audio, "clear", None)
        if clear is not None:  # a pause, break, or jump left audio queued; hear this cast live
            clear()
        if not self._press(key, "cast"):
            return
        self.stats = ev.Stats(
            self._t(), self.stats.casts + 1, self.stats.catches, self.stats.misses,
            self.stats.bites_heard, self._elapsed(),
        )  # fmt: skip
        self._t_cast = self._clock()
        self._last_acquire = -math.inf
        if self.search is not None:
            self.search.reset()
        limit = self.cfg.run.icon_timeout_s
        self._change(
            State.CASTING,
            msg("cast", key=key),
            msg("seek", seconds=limit),
        )

    def _acquire(self, now: float) -> None:
        if now - self._last_acquire < 0.1:
            return
        self._last_acquire = now
        frame = self.capturer.grab()
        v = self.cfg.vision
        region = Region(v.search_left, v.search_top, v.search_right, v.search_bottom)
        if self.search is not None:  # fast path: the saved or last measured size
            m = self.matcher.match(frame, region)
            if m.accepted(self.threshold):
                self._icon_timeouts = 0
                self._sweeping = False
                self._found(frame, m)
                return
        if self._sweeping or self.search is None:
            x0, y0, x1, y1 = region.to_box(frame.shape[1], frame.shape[0])
            cal = self._calibrate(frame[y0:y1, x0:x1], match_width=self.cfg.vision.match_width)
            cal = replace(cal, x=cal.x + x0, y=cal.y + y0)
            if cal.valid:
                self._use_calibration(cal)
                self._icon_timeouts = 0
                self._found(frame, IconMatch(cal.score, cal.x, cal.y, cal.icon_size_px, cal.gold))
                return
        if now - self._t_cast > self.cfg.run.icon_timeout_s:
            self._emit(ev.Timeout(self._t(), "icon", self.cfg.run.icon_timeout_s))
            self._icon_timeouts += 1
            if self._icon_timeouts >= RESWEEP_AFTER_TIMEOUTS:
                self._sweeping = True
            self._miss(msg("no_icon"))

    def _use_calibration(self, cal: Any) -> None:
        self.matcher = self._matcher_factory(cal.icon_size_px)
        self.threshold = cal.suggested_threshold()
        self.search = IconSearch(self.matcher, self.threshold)
        self._sweeping = False
        self._emit(ev.Calibrated(self._t(), int(cal.icon_size_px), float(cal.score)))
        self._on_calibrated(cal)

    def _found(self, frame: np.ndarray, m: Any) -> None:
        self._icon_xy = (m.x, m.y)
        self._icon_size = m.size
        self._misses = 0
        self._disappeared_since = None
        self._candidate = None
        self._bite_rejected = False
        if self.verifier is not None:
            self.verifier.reset()
        self._last_visual = -math.inf
        self._last_box_grab = self._clock()  # the full frame just confirmed it; look again later
        thumb, box = _thumbnail(frame, m.x, m.y, m.size)
        self._emit(ev.IconFound(self._t(), thumb, box, float(m.score), int(m.size), (m.x, m.y)))
        self.detector.reset()
        self._best_heard = ("", -1.0)
        self._last_listen = self._last_scores_sent = self._clock()
        self._change(
            State.WAIT_BITE,
            msg("found", x=m.x, y=m.y),
            msg("wait_bite", key=self.cfg.game.interact_key),
        )

    def _wait_bite(self, now: float, bites: list[Any]) -> None:
        if bites:
            bite = bites[0]
            self.stats = ev.Stats(
                self._t(), self.stats.casts, self.stats.catches, self.stats.misses,
                self.stats.bites_heard + 1, self._elapsed(),
            )  # fmt: skip
            self._emit(ev.BiteHeard(self._t(), bite.clip, float(bite.score)))
            self._candidate = (bite, now + self.cfg.vision.confirm_window_s)
        if self._candidate is not None:
            bite, deadline = self._candidate
            if self.verifier is not None and not self.verifier.confirmed(now):
                if now >= deadline:
                    self._candidate = None
                    self._bite_rejected = True
                    strongest = getattr(self.verifier, "strongest", None)
                    change, needed = (strongest(now) if strongest else None) or (0.0, 0.0)
                    self._emit(
                        ev.BiteRejected(self._t(), float(bite.score), float(change), float(needed))
                    )
                # Keep checking icon loss and the cast timeout while awaiting confirmation.
            else:
                self._candidate = None
                if self.verifier is not None:
                    self._visual_misses = 0  # the picture check passed a bite: it works here
                self._interact_bite(bite)
                return
        seen = self._track(now)
        if seen is False and self._misses >= self.lost_after:
            self._emit(ev.IconLost(self._t()))
            self._note_visual_miss()
            self._miss(msg("lost_icon"))
            return
        if now - self._t_cast > self.cfg.run.cast_timeout_s:
            clip, score = self._best_heard
            detail = f"最高得分 {score:.2f}（{clip}）" if clip else "没有声音匹配"
            self._emit(ev.Timeout(self._t(), "bite", self.cfg.run.cast_timeout_s, detail))
            self._note_visual_miss()
            self._miss(msg("no_bite"))

    def _note_visual_miss(self) -> None:
        """The cast ended without reeling in. If the picture check held back a splash on it,
        count that, and stop letting the check veto once it has done so several casts running:
        a bobber that goes unanswered every cast is worse than answering a neighbour's splash."""
        if self.verifier is None or not self._bite_rejected:
            return
        self._visual_misses += 1
        if self._visual_misses >= VISUAL_MISSES_BEFORE_BYPASS:
            self.verifier = None
            self._emit(ev.Error(self._t(), msg("visual_off", casts=self._visual_misses)))

    def _interact_bite(self, bite: Any) -> None:
        if self.behavior.skip_bite():
            self._miss(msg("skip"))
            return
        if not self._wait(self.behavior.reaction_s(), watch_focus=True):
            return
        key = self.cfg.game.interact_key
        if not self._press(key, "bite"):
            return
        self._t_interact = self._clock()
        self._last_box_grab = -math.inf
        self._misses = 0
        self._disappeared_since = None
        self._change(
            State.INTERACT,
            msg("interact", key=key, score=bite.score),
            msg("loot"),
        )

    def _after_interact(self, now: float) -> None:
        seen = self._track(now)
        if seen is True:
            self._disappeared_since = None
        elif seen is False and self._disappeared_since is None:
            self._disappeared_since = now
        if (
            seen is False
            and self._misses >= self.lost_after
            and self._disappeared_since is not None
            and now - self._disappeared_since >= self.cfg.run.loot_confirm_s
        ):
            self.stats = ev.Stats(
                self._t(), self.stats.casts, self.stats.catches + 1, self.stats.misses,
                self.stats.bites_heard, self._elapsed(),
            )  # fmt: skip
            self._emit(ev.Caught(self._t()))
            self._pause_then_idle(msg("caught"))
            return
        if now - self._t_interact > self.cfg.run.loot_timeout_s:
            self._emit(ev.Timeout(self._t(), "loot", self.cfg.run.loot_timeout_s))
            self._miss(msg("no_loot"))

    # -- lure --------------------------------------------------------------------------------

    def _lure_due(self, now: float) -> bool:
        if not self.cfg.game.lure_key:
            return False
        return self._lure_at is None or now - self._lure_at >= self.cfg.game.lure_minutes * 60.0

    def _apply_lure(self) -> None:
        """Press the lure macro and wait out its cast. The bot cannot see the bags or the buff,
        so the macro does nothing when the lure is missing and this just refreshes on a timer."""
        key, wait = self.cfg.game.lure_key, self.cfg.run.lure_cast_s
        if not self._press(key, "lure"):
            return
        self._change(
            State.IDLE,
            msg("lure", key=key),
            msg("lure_wait", seconds=wait, minutes=self.cfg.game.lure_minutes),
        )
        if self._wait(wait, watch_focus=True):
            self._lure_at = self._clock()

    # -- helpers -----------------------------------------------------------------------------

    def _refresh_window(self, now: float) -> None:
        if self._find_window is None or now - self._last_window_refresh < 0.25:
            return
        self._last_window_refresh = now
        found = self._find_window(self.window.title)
        if found is None or (found.handle, found.pid) != (self.window.handle, self.window.pid):
            self._finish("window closed")
            return
        if found == self.window:
            return
        resized = (found.width, found.height, found.scale) != (
            self.window.width,
            self.window.height,
            self.window.scale,
        )
        self.window = found
        update = getattr(self.capturer, "update_window", None)
        if update is not None:
            update(found)
        else:
            self.capturer.close()
            self.capturer.open(found)
        if resized:
            self._sweeping = True
            if self.state in BUSY:
                self._miss(msg("resized"))

    def _observe_bobber(self, now: float) -> None:
        if self.verifier is None or self._icon_xy is None or now - self._last_visual < 0.1:
            return
        self._last_visual = now
        x, y = self._icon_xy
        side = max(16, self._icon_size)
        x0, y0 = max(0, x - side * 2), max(0, y - side)
        x1 = min(self.window.width, x + side * 2)
        y1 = min(self.window.height, y + side * 4)
        self.verifier.observe(self.capturer.grab((x0, y0, x1 - x0, y1 - y0)), now)

    def _track(self, now: float) -> bool | None:
        """Grab the box around the icon (at most every box_interval_s) and look for it there.

        True = seen, False = not seen (misses counted), None = not time to look yet.
        """
        if now - self._last_box_grab < self.box_interval_s or self._icon_xy is None:
            return None
        self._last_box_grab = now
        x, y = self._icon_xy
        w, h = self.window.width, self.window.height
        x0, y0, x1, y1 = Region.around(x, y, w, h, 0.10).to_box(w, h)
        v = self.cfg.vision
        rx0, ry0, rx1, ry1 = Region(
            v.search_left, v.search_top, v.search_right, v.search_bottom
        ).to_box(w, h)
        x0, y0, x1, y1 = max(x0, rx0), max(y0, ry0), min(x1, rx1), min(y1, ry1)
        if x1 <= x0 or y1 <= y0:
            self._misses += 1
            return False
        box: Box = (x0, y0, x1 - x0, y1 - y0)
        crop = self.capturer.grab(box)
        m = self.matcher.match(crop, full_width=w)
        if m.accepted(self.threshold):
            self._icon_xy = (x0 + m.x, y0 + m.y)
            self._misses = 0
            return True
        self._misses += 1
        return False

    def _time_up(self) -> bool:
        return self.session_s != 0 and self._elapsed() >= self.session_s

    def _quit_game(self) -> None:
        """Press the key of the player's /quit macro, if one is set, and wait for the game
        window to close; press it again while the window stays, QUIT_TRIES times in all."""
        key, find = self.cfg.game.quit_key, self._find_window
        if not key:
            return
        try:
            for _ in range(QUIT_TRIES):
                if self.input.is_game_focused(self.window) is False:
                    self._emit(ev.Error(self._t(), "会话时间已到，游戏不在前台，已保留游戏窗口"))
                    return
                if not self._press(key, "quit"):
                    return
                if find is None:
                    return  # nothing to watch the window with
                self._change(
                    State.IDLE,
                    msg("quit", key=key),
                    msg("quit_wait", seconds=QUIT_WAIT_S),
                )
                if self._wait_for_game_to_close(find):
                    self._change(State.IDLE, msg("closed"), msg("stopping"))
                    return
                if self._stop:
                    return
            self._emit(
                ev.Error(
                    self._t(),
                    f"已按 {key} 共 {QUIT_TRIES} 次，游戏仍未关闭；请检查 /quit 宏",
                )
            )
        except Exception as e:
            self._emit(ev.Error(self._t(), f"退出游戏失败：{type(e).__name__}: {e}"))

    def _wait_for_game_to_close(self, find: Callable[..., WindowRef | None]) -> bool:
        """Look for the game window every second for up to QUIT_WAIT_S; True once it is gone."""
        deadline = self._clock() + QUIT_WAIT_S
        while not self._stop and not self._user_paused.is_set():
            found = find(self.window.title)
            if found is None or (found.handle, found.pid) != (self.window.handle, self.window.pid):
                return True  # nothing left, or only another window with the title
            if self._clock() >= deadline:
                return False
            self._wait(1.0)
        return False

    def _press(self, key: str, why: str) -> bool:
        if (
            self._stop
            or self._user_paused.is_set()
            or self.input.is_game_focused(self.window) is False
        ):
            return False
        self.input.press_key(key, self.behavior.hold_s())
        self._emit(ev.KeyPressed(self._t(), key, why))
        return True

    def _miss(self, did: str) -> None:
        self.stats = ev.Stats(
            self._t(), self.stats.casts, self.stats.catches, self.stats.misses + 1,
            self.stats.bites_heard, self._elapsed(),
        )  # fmt: skip
        self._pause_then_idle(did)

    def _pause_then_idle(self, did: str) -> None:
        pause = self._rng.uniform(self.cfg.run.pause_min_s, self.cfg.run.pause_max_s)
        rest = self.behavior.break_s(self._clock())
        if rest > 0 and self.session_s != 0:  # a break never outlasts the session
            rest = min(rest, max(0.0, self.session_s - self._elapsed()))
        if self._time_up():
            nxt = msg("time_quit") if self.cfg.game.quit_key else msg("time_up")
        elif rest > 0:
            nxt = msg("rest", minutes=rest / 60)
        else:
            nxt = msg("recast", seconds=pause)
        self._change(State.IDLE, did, nxt)
        self._icon_xy = None
        self._wait(rest + pause, watch_focus=True)

    def _wait(self, seconds: float, *, watch_focus: bool = False) -> bool:
        """Return whether the wait completed; gameplay delays also pause on focus loss."""
        deadline = self._clock() + seconds
        while not self._stop:
            if watch_focus and self._pause_if_needed():
                return False
            if self._user_paused.is_set() and self.state is not State.PAUSED:
                return False
            remaining = deadline - self._clock()
            if remaining <= 0:
                return True
            step = min(SLEEP_SLICE_S, remaining)
            if self._real_sleep:
                self._stop_event.wait(step)
            else:
                self._sleep(step)
        return False

    def _drain_audio(self, now: float) -> list[AudioChunk]:
        rate = self.audio.sample_rate
        last = self._last_audio if self._last_audio is not None else now
        n = int((now - last) * rate)
        n = max(AUDIO_BLOCK, min(n, int(AUDIO_MAX_S * rate)))
        self._last_audio = now
        try:
            samples = self.audio.read(n)
        except RuntimeError:
            restart = getattr(self.audio, "restart", None)
            if restart is None or self._audio_retries >= 3 or self._stop:
                raise
            self._audio_retries += 1
            self._audio_stable_since = None
            self._emit(ev.Error(self._t(), f"音频中断，正在重新连接（{self._audio_retries}/3）"))
            self._wait(1.0)
            if self._stop:
                return []
            try:
                restart()
            except Exception as e:
                self._emit(ev.Error(self._t(), f"音频重新连接失败：{e}"))
                if self._audio_retries >= 3:
                    raise
                return []
            self._emit(
                ev.AudioOpened(
                    self._t(),
                    getattr(self.audio, "name", "音频设备"),
                    getattr(self.audio, "fallback_reason", ""),
                )
            )
            self._candidate = None
            self.detector.reset()
            return []
        if not len(samples):
            return []
        if self._audio_stable_since is None:
            self._audio_stable_since = now
        elif now - self._audio_stable_since >= 5.0:
            self._audio_retries = 0
        self._audio_frames += len(samples)
        return [AudioChunk(samples, rate, self._audio_frames / rate)]

    def _change(self, state: State, did: str, nxt: str) -> None:
        self.state = state
        self._emit(ev.StateChanged(self._t(), state, did, nxt))
        self._emit_stats()

    def _note_scores(self) -> None:
        best = getattr(self.detector, "best_heard", None)
        if best is not None and best.score > self._best_heard[1]:
            self._best_heard = (best.name, float(best.score))

    def _bite_threshold(self) -> float:
        return float(getattr(self.detector, "threshold", self.cfg.audio.bite_threshold))

    def _send_scores(self, now: float, *, flush: bool) -> None:
        take = getattr(self.detector, "take_peaks", None)
        if take is None or not (flush or now - self._last_scores_sent >= SCORES_EVERY_S):
            return
        self._last_scores_sent = now
        self._emit(ev.Scores(self._t(), take(), self._bite_threshold()))

    def _maybe_stats(self, now: float) -> None:
        if self.state is not State.WAIT_BITE:
            return
        if now - self._last_listen >= LISTEN_EVERY_S:
            self._last_listen = now
            clip, score = self._best_heard
            self._emit(ev.Listening(self._t(), clip, score, self._bite_threshold()))
        if now - self._last_stats >= STATS_EVERY_S:
            self._emit_stats()

    def _emit_stats(self) -> None:
        self._last_stats = self._clock()
        self.stats = ev.Stats(
            self._t(), self.stats.casts, self.stats.catches, self.stats.misses,
            self.stats.bites_heard, self._elapsed(),
        )  # fmt: skip
        self._emit(self.stats)

    def _finish(self, reason: str) -> None:
        if self.state is State.STOPPED:
            return
        self._emit_stats()
        self.state = State.STOPPED
        self._emit(ev.Stopped(self._t(), reason))

    def _emit(self, event: ev.Event) -> None:
        self.events.put(event)

    def _elapsed(self) -> float:
        return 0.0 if self.started is None else self._clock() - self.started

    def _t(self) -> float:
        return self._elapsed()


def _thumbnail(frame: np.ndarray, x: int, y: int, size: int) -> tuple[np.ndarray, Box]:
    h, w = frame.shape[:2]
    scale = min(1.0, THUMB_WIDTH / w)
    if scale < 1.0:
        thumb = cv2.resize(
            frame, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA
        )
    else:
        thumb = frame.copy()
    side = max(2, round(size * scale))
    bx = max(0, round(x * scale - side / 2))
    by = max(0, round(y * scale - side / 2))
    return thumb, (bx, by, side, side)


def unsupported_platform() -> str:
    """Why the bot cannot run here, or "" when it can (Linux X11/Xwayland/Hyprland, Windows,
    macOS)."""
    import os
    import sys

    if sys.platform == "win32" or macos() or linux_x11() or linux_hyprland():
        return ""
    if sys.platform == "linux" and not os.environ.get("DISPLAY"):
        return (
            "DISPLAY is not set; the bot needs X11, Xwayland, or Hyprland "
            "(log into a session with one)"
        )
    return (
        "the bot is not supported yet on this platform "
        "(Linux X11/Xwayland/Hyprland, Windows, and macOS only)"
    )


def missing_permissions(request: bool = False) -> list[str]:
    """The macOS privacy permissions the bot still lacks, in plain words; empty elsewhere.

    `request` also asks macOS for them, which shows its prompt the first time.
    """
    if not macos():
        return []
    from wowfish.capture._quartz import screen_capture_available
    from wowfish.errors import NotSupported
    from wowfish.input._quartz import accessibility_available

    missing: list[str] = []
    for check in (screen_capture_available, accessibility_available):
        try:
            ok, detail = check(request)
        except NotSupported as e:
            ok, detail = False, str(e)
        if not ok:
            missing.append(detail)
    return missing


def build_engine(
    cfg: Config,
    *,
    session_s: float | None = None,
    title: str | None = None,
    window: WindowRef | None = None,
    record_audio: Path | None = None,
) -> FishingEngine:
    """Wire the real capturer, input device, audio source, detector, and matcher for `cfg`.

    `record_audio` saves everything the engine hears to that WAV when it stops (for debugging
    missed splashes offline). Raises RuntimeError with a one-line hint when a prerequisite is
    missing.
    """
    from wowfish.audio._recording import RecordingSource
    from wowfish.audio.detector import detector_for
    from wowfish.audio.devices import open_capture
    from wowfish.input import get_input_device
    from wowfish.vision.matcher import IconMatcher
    from wowfish.vision.template import load_icon_templates, template_for_size

    platform_problem = unsupported_platform()
    if platform_problem:
        raise RuntimeError(platform_problem)
    missing = missing_permissions(request=True)
    if missing:
        raise RuntimeError("; ".join(missing))
    title = cfg.game.window_title if title is None else title
    window = window or find_window(title)
    if window is None:
        raise RuntimeError("未找到游戏窗口，请启动客户端并在界面选择它")
    templates = load_icon_templates()

    def matcher_factory(size: int) -> IconMatcher:
        return IconMatcher(template_for_size(templates, size), cfg.vision.match_width)

    def on_calibrated(cal: Any) -> None:
        from wowfish.config import update_config

        update_config(
            lambda current: replace(
                current,
                vision=replace(
                    current.vision,
                    icon_size_px=cal.icon_size_px,
                    icon_native_size=cal.native_size,
                    icon_threshold=cal.suggested_threshold(),
                ),
            )
        )

    detector = detector_for(cfg.audio)

    def audio_factory() -> Any:
        return open_capture(
            cfg.audio.selector,
            pid=window.pid,
            title=window.title,
            fallback=cfg.audio.fallback_to_device,
        )

    audio: Any = ThreadedSource(audio_factory(), factory=audio_factory)
    if record_audio is not None:
        audio = RecordingSource(audio, record_audio)
    return FishingEngine(
        cfg,
        window,
        capturer=get_capturer(window),
        input_device=get_input_device(window),
        audio_source=audio,
        detector=detector,
        matcher_factory=matcher_factory,
        on_calibrated=on_calibrated,
        find_window=lambda _: refresh_window(window),
        session_s=session_s,
    )


def _real_calibrate(
    frame: np.ndarray, *, match_width: int = 1920, cancel: Callable[[], bool] | None = None
) -> Any:
    from wowfish.vision.calibrate import calibrate_icon_scale
    from wowfish.vision.template import load_icon_templates

    return calibrate_icon_scale(
        frame, load_icon_templates(), match_width=match_width, cancel=cancel
    )
