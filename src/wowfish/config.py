"""User configuration: one TOML file, defaults merged in, unknown keys warned about."""

from __future__ import annotations

import math
import os
import re
import tempfile
import threading
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import tomli_w
import tomlkit
import tomlkit.exceptions
from filelock import FileLock
from tomlkit.items import Comment, Key, Table, Whitespace

from wowfish import paths
from wowfish.assets import default_config_path
from wowfish.input.keys import is_supported_key

_LINE_RE = re.compile(r"\(at line (\d+), column \d+\)")
_WRITE_LOCK = threading.RLock()


class ConfigError(Exception):
    def __init__(self, path: Path, message: str, line: int | None = None) -> None:
        self.path = path
        self.message = message
        self.line = line
        super().__init__(str(self))

    def __str__(self) -> str:
        where = f"{self.path} line {self.line}" if self.line is not None else str(self.path)
        return f"Config error in {where}: {self.message}"


@dataclass(frozen=True)
class GameConfig:
    cast_key: str = "1"
    interact_key: str = "f"
    session_minutes: int = 0
    quit_key: str = ""  # an action bar /quit macro, pressed when session_minutes runs out
    # Keybind for a macro that puts a lure (e.g. a bauble) on the fishing pole. "" = no lure.
    lure_key: str = ""
    # How long the lure lasts; it is put on again once this has passed.
    lure_minutes: float = 10.0
    window_title: str = ""  # Empty: detect Chinese and English clients.
    pause_hotkey: str = "f8"
    stop_hotkey: str = "f9"


@dataclass(frozen=True)
class VisionConfig:
    icon_size_px: int = 0
    icon_native_size: int = 0
    icon_threshold: float = 0.75
    match_width: int = 1920
    confirm_bite: bool = True
    confirm_window_s: float = 0.6
    motion_threshold: float = 8.0
    search_left: float = 0.0
    search_top: float = 0.0
    search_right: float = 1.0
    search_bottom: float = 1.0


@dataclass(frozen=True)
class AudioConfig:
    capture: str = "game"  # "game": the game's own audio stream; "device": the loopback `device`
    device: str = ""
    bite_threshold: float = 0.66
    refractory_s: float = 3.0
    fallback_to_device: bool = True

    @property
    def selector(self) -> str:
        """What to open: "game", or the loopback device selector ("" = default output monitor)."""
        return "game" if self.capture == "game" else self.device


@dataclass(frozen=True)
class RunConfig:
    icon_timeout_s: float = 4.0
    cast_timeout_s: float = 30.0
    loot_timeout_s: float = 3.0
    pause_min_s: float = 0.5
    pause_max_s: float = 2.0
    key_hold_ms: int = 60
    lure_cast_s: float = 6.0
    tick_interval_s: float = 0.02
    loot_confirm_s: float = 0.4


@dataclass(frozen=True)
class BehaviorConfig:
    break_every_min_minutes: float = 0.0
    break_every_max_minutes: float = 0.0  # 0 = never take a break
    break_min_minutes: float = 0.0
    break_max_minutes: float = 0.0
    reaction_min_ms: int = 0
    reaction_max_ms: int = 0
    skip_bite_chance: float = 0.0
    key_hold_jitter_ms: int = 0
    jump_chance: float = 0.0
    jump_key: str = "space"


@dataclass(frozen=True)
class Config:
    game: GameConfig
    vision: VisionConfig
    audio: AudioConfig
    run: RunConfig = field(default_factory=RunConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)


_SECTIONS: dict[str, type] = {
    "game": GameConfig,
    "vision": VisionConfig,
    "audio": AudioConfig,
    "run": RunConfig,
    "behavior": BehaviorConfig,
}


@contextmanager
def config_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK, FileLock(str(path) + ".lock", timeout=5):
        yield


def _atomic_write(path: Path, text: str) -> None:
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as stream:
            tmp = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def ensure_config_file(path: Path | None = None) -> bool:
    path = path or paths.config_file()
    with config_lock(path):
        if path.exists():
            return False
        _atomic_write(path, default_config_path().read_text(encoding="utf-8"))
        return True


def add_missing_settings(path: Path | None = None) -> list[str]:
    path = path or paths.config_file()
    with config_lock(path):
        return _add_missing_settings(path)


def _add_missing_settings(path: Path) -> list[str]:
    """Add the settings a newer release introduced to an existing config file, each with its
    comment from default_config, and return what was added ("game.quit_key", "[behavior]").

    Only adds: values, comments and unknown keys already in the file stay as they are. Does not
    create a missing file, and leaves alone a file that does not parse (load_config reports it).
    """
    path = path or paths.config_file()
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"  # or the first added comment would land on the last line
    try:
        doc = tomlkit.parse(text)
        before = tomllib.loads(text)
    except (tomlkit.exceptions.ParseError, tomllib.TOMLDecodeError):
        return []
    template = tomlkit.parse(default_config_path().read_text(encoding="utf-8"))

    added: list[str] = []
    for section, template_table in template.items():
        if section not in doc:
            doc.append(section, template_table)
            added.append(f"[{section}]")
            continue
        table = doc[section]
        if not isinstance(table, Table):  # e.g. an inline table; load_config judges it
            continue
        missing = [
            (key, comments)
            for key, comments in _keys_with_comments(template_table)
            if key.key not in table
        ]
        if not missing:
            continue
        body = table.value.body
        blank_line_after = bool(body) and isinstance(body[-1][1], Whitespace)
        for key, comments in missing:
            for comment in comments:
                table.append(None, comment)
            table.append(key, template_table[key.key])
            added.append(f"{section}.{key.key}")
        if blank_line_after:  # appending lands after the section's blank line; put one back
            table.append(None, tomlkit.ws("\n"))

    if not added:
        return []
    merged = tomlkit.dumps(doc)
    after = tomllib.loads(merged)
    for section, values in before.items():  # never change what was there
        kept = after.get(section)
        if isinstance(values, dict) and isinstance(kept, dict):
            kept = {k: v for k, v in kept.items() if k in values}
        if kept != values:
            return []
    _atomic_write(path, merged)
    return added


def _keys_with_comments(table: Table) -> list[tuple[Key, list[Comment]]]:
    """Each key of a default_config table with the comment lines directly above it."""
    found: list[tuple[Key, list[Comment]]] = []
    comments: list[Comment] = []
    for key, item in table.value.body:
        if isinstance(item, Comment):
            comments.append(item)
        elif key is not None:
            found.append((key, comments))
            comments = []
        else:
            comments = []
    return found


def load_config(path: Path | None = None) -> tuple[Config, list[str]]:
    path = path or paths.config_file()
    if not path.exists():
        return Config(game=GameConfig(), vision=VisionConfig(), audio=AudioConfig()), []
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        m = _LINE_RE.search(str(e))
        raise ConfigError(path, str(e), int(m.group(1)) if m else None) from e
    except (UnicodeError, OSError) as e:
        raise ConfigError(path, f"无法读取 UTF-8 配置：{e}") from e

    warnings: list[str] = []
    built: dict[str, Any] = {}
    for section, cls in _SECTIONS.items():
        table = raw.pop(section, {})
        if not isinstance(table, dict):
            raise ConfigError(path, f"[{section}] must be a table")
        built[section] = _build_section(path, section, cls, table, warnings)
    for leftover in raw:
        warnings.append(f"unknown section [{leftover}] ignored")
    cfg = Config(**built)
    _validate(path, cfg)
    return cfg, warnings


def save_config(cfg: Config, path: Path | None = None) -> None:
    path = path or paths.config_file()
    with config_lock(path):
        _save_config(cfg, path)


def _save_config(cfg: Config, path: Path) -> None:
    _validate(path, cfg)
    text = (
        path.read_text(encoding="utf-8")
        if path.exists()
        else default_config_path().read_text(encoding="utf-8")
    )
    doc = tomlkit.parse(text)
    for section, values in asdict(cfg).items():
        if section not in doc:
            doc[section] = tomlkit.table()
        for key, value in values.items():
            doc[section][key] = value
    _atomic_write(path, tomlkit.dumps(doc))


def update_config(transform: Callable[[Config], Config], path: Path | None = None) -> Config:
    """Read-modify-write under one cross-process lock; never publish a stale snapshot."""
    path = path or paths.config_file()
    with config_lock(path):
        current, _ = load_config(path)
        updated = transform(current)
        _save_config(updated, path)
        return updated


def recover_config(path: Path | None = None) -> Path:
    """Explicitly back up a damaged configuration and restore defaults."""
    path = path or paths.config_file()
    with config_lock(path):
        backup = path.with_name(path.name + ".bak")
        index = 0
        while backup.exists():
            index += 1
            backup = path.with_name(f"{path.name}.bak.{index}")
        if path.exists():
            os.replace(path, backup)
        _atomic_write(path, default_config_path().read_text(encoding="utf-8"))
        return backup


def _build_section(
    path: Path, section: str, cls: type, table: dict[str, Any], warnings: list[str]
) -> Any:
    known = {f.name: f for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in table.items():
        if key not in known:
            warnings.append(f"unknown key {section}.{key} ignored")
            continue
        expected = _expected_type(known[key].type)
        if expected is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise ConfigError(
                path, f"{section}.{key} must be {expected.__name__}, got {type(value).__name__}"
            )
        kwargs[key] = value
    return cls(**kwargs)


def _expected_type(annotation: Any) -> type:
    # Annotations are strings because of `from __future__ import annotations`.
    text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    base = text.replace(" | None", "").strip()
    return {"str": str, "int": int, "bool": bool, "float": float}[base]


def _validate(path: Path, cfg: Config) -> None:
    def check(ok: bool, key: str, rule: str) -> None:
        if not ok:
            raise ConfigError(path, f"{key} {rule}")

    for section, values in asdict(cfg).items():
        for key, value in values.items():
            if isinstance(value, float):
                check(math.isfinite(value), f"{section}.{key}", "必须为有限数值")
    for key in ("cast_key", "interact_key"):
        check(is_supported_key(getattr(cfg.game, key)), f"game.{key}", "必须为支持的非空键位")
    hotkeys = [k.lower() for k in (cfg.game.pause_hotkey, cfg.game.stop_hotkey) if k]
    check(
        all(k in {f"f{i}" for i in range(1, 13)} for k in hotkeys),
        "game.hotkeys",
        "仅支持 F1–F12 或留空",
    )
    check(len(hotkeys) == len(set(hotkeys)), "game.hotkeys", "暂停与停止快捷键不能相同")
    action_keys = (cfg.game.cast_key, cfg.game.interact_key, cfg.game.quit_key, cfg.game.lure_key)
    check(
        not set(hotkeys).intersection(k.lower() for k in action_keys),
        "game.hotkeys",
        "不能与游戏操作键冲突",
    )

    check(cfg.game.session_minutes >= 0, "game.session_minutes", "must be 0 (forever) or positive")
    q = cfg.game.quit_key
    check(not q or is_supported_key(q), "game.quit_key", "must be empty or a supported key name")
    lk = cfg.game.lure_key
    check(not lk or is_supported_key(lk), "game.lure_key", "must be empty or a supported key name")
    check(cfg.game.lure_minutes > 0, "game.lure_minutes", "must be positive")
    v, a = cfg.vision, cfg.audio
    check(0.0 < v.icon_threshold <= 1.0, "vision.icon_threshold", "must be in (0, 1]")
    check(v.match_width >= 320, "vision.match_width", "must be at least 320")
    check(v.icon_size_px >= 0, "vision.icon_size_px", "must be 0 or positive")
    check(v.icon_native_size >= 0, "vision.icon_native_size", "must be 0 or positive")
    check(0 <= v.search_left < v.search_right <= 1, "vision.search_region", "水平范围必须在 0–1 内")
    check(0 <= v.search_top < v.search_bottom <= 1, "vision.search_region", "垂直范围必须在 0–1 内")
    check(0 < v.confirm_window_s <= 2, "vision.confirm_window_s", "必须在 0–2 秒内")
    check(0 < v.motion_threshold <= 255, "vision.motion_threshold", "必须在 0–255 内")
    check(a.capture in ("game", "device"), "audio.capture", 'must be "game" or "device"')
    check(0.0 < a.bite_threshold <= 1.0, "audio.bite_threshold", "must be in (0, 1]")
    check(a.refractory_s >= 0.0, "audio.refractory_s", "must be 0 or positive")
    r = cfg.run
    check(r.icon_timeout_s > 0, "run.icon_timeout_s", "must be positive")
    check(r.cast_timeout_s > 0, "run.cast_timeout_s", "must be positive")
    check(r.loot_timeout_s > 0, "run.loot_timeout_s", "must be positive")
    check(r.pause_min_s >= 0, "run.pause_min_s", "must be 0 or positive")
    check(r.pause_min_s <= r.pause_max_s, "run.pause_min_s", "must not exceed run.pause_max_s")
    check(r.lure_cast_s >= 0, "run.lure_cast_s", "must be 0 or positive")
    check(0.005 <= r.tick_interval_s <= 0.2, "run.tick_interval_s", "必须在 0.005–0.2 秒内")
    check(0 < r.loot_confirm_s < r.loot_timeout_s, "run.loot_confirm_s", "必须小于拾取超时")
    check(10 <= r.key_hold_ms <= 500, "run.key_hold_ms", "must be between 10 and 500")
    b = cfg.behavior
    for lo, hi in (
        ("break_every_min_minutes", "break_every_max_minutes"),
        ("break_min_minutes", "break_max_minutes"),
        ("reaction_min_ms", "reaction_max_ms"),
    ):
        check(getattr(b, lo) >= 0, f"behavior.{lo}", "must be 0 or positive")
        check(getattr(b, lo) <= getattr(b, hi), f"behavior.{lo}", f"must not exceed behavior.{hi}")
    check(0.0 <= b.skip_bite_chance < 1.0, "behavior.skip_bite_chance", "must be in [0, 1)")
    check(0.0 <= b.jump_chance <= 1.0, "behavior.jump_chance", "must be in [0, 1]")
    check(
        b.key_hold_jitter_ms >= 0 and r.key_hold_ms - b.key_hold_jitter_ms >= 10,
        "behavior.key_hold_jitter_ms",
        "must be 0 or positive and leave run.key_hold_ms - jitter at 10 or more",
    )
    check(is_supported_key(b.jump_key), "behavior.jump_key", "must be a supported key name")


def to_toml(cfg: Config) -> str:
    return tomli_w.dumps(asdict(cfg))
