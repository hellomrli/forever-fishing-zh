"""Publication must reject stale metadata before producing an EXE."""

from __future__ import annotations

import importlib.util
import shutil
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
NUMERIC_VERSION = (*(int(part) for part in VERSION.split(".")[:3]), 0)
spec = importlib.util.spec_from_file_location("check_release", ROOT / "scripts/check_release.py")
assert spec is not None and spec.loader is not None
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def metadata(tmp_path):
    for relative in (
        "pyproject.toml",
        "uv.lock",
        "src/wowfish/__init__.py",
        "packaging/windows_version.txt",
        "packaging/windows.manifest",
        f"docs/releases/v{VERSION}.md",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    return tmp_path


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        ("pyproject.toml", f'version = "{VERSION}"', 'version = "0.0.0"'),
        ("uv.lock", f'version = "{VERSION}"', 'version = "0.0.0"'),
        ("src/wowfish/__init__.py", f'"{VERSION}"', '"0.0.0"'),
        (
            "packaging/windows_version.txt",
            f"'FileVersion', '{VERSION}'",
            "'FileVersion', '0.0.0'",
        ),
        (
            "packaging/windows_version.txt",
            f"filevers={NUMERIC_VERSION}",
            "filevers=(0, 0, 0, 0)",
        ),
        (
            "packaging/windows.manifest",
            f'version="{".".join(map(str, NUMERIC_VERSION))}"',
            'version="0.0.0.0"',
        ),
    ],
)
def test_rejects_stale_metadata(metadata, path, old, new):
    target = metadata / path
    text = target.read_text(encoding="utf-8")
    assert old in text
    target.write_text(text.replace(old, new), encoding="utf-8")
    with pytest.raises(SystemExit, match="metadata mismatch"):
        release.check_versions(metadata, VERSION)


def test_rejects_missing_notes(metadata):
    (metadata / f"docs/releases/v{VERSION}.md").unlink()
    with pytest.raises(SystemExit, match="Missing release notes"):
        release.check_versions(metadata, VERSION)
