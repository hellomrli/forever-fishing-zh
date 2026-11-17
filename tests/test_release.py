"""Publication must reject stale metadata before producing an EXE."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
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
        "docs/releases/v1.0.1.md",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    return tmp_path


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        ("pyproject.toml", 'version = "1.0.1"', 'version = "1.0.0"'),
        ("uv.lock", 'version = "1.0.1"', 'version = "1.0.0"'),
        ("src/wowfish/__init__.py", '"1.0.1"', '"1.0.0"'),
        ("packaging/windows_version.txt", "'FileVersion', '1.0.1'", "'FileVersion', '1.0.0'"),
        ("packaging/windows_version.txt", "filevers=(1, 0, 1, 0)", "filevers=(1, 0, 0, 0)"),
        ("packaging/windows.manifest", 'version="1.0.1.0"', 'version="1.0.0.0"'),
    ],
)
def test_rejects_stale_metadata(metadata, path, old, new):
    target = metadata / path
    text = target.read_text(encoding="utf-8")
    assert old in text
    target.write_text(text.replace(old, new), encoding="utf-8")
    with pytest.raises(SystemExit, match="metadata mismatch"):
        release.check_versions(metadata, "1.0.1")


def test_rejects_missing_notes(metadata):
    (metadata / "docs/releases/v1.0.1.md").unlink()
    with pytest.raises(SystemExit, match="Missing release notes"):
        release.check_versions(metadata, "1.0.1")
