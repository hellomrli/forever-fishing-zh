"""Preflight for release.yml. Fails fast before anything is built.

Usage: uv run python scripts/check_release.py
Prints `prerelease=true|false` on success.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from importlib.metadata import version as installed_version
from pathlib import Path

_PRERELEASE_RE = re.compile(r"^\d+(\.\d+)*(a|b|rc)\d+")


def is_prerelease(version: str) -> bool:
    return bool(_PRERELEASE_RE.match(version))


def check_assets_present(root: Path) -> None:
    assets = root / "src" / "wowfish" / "assets"
    expected = [assets / "interact_icon" / f"interact_gold_{n}.png" for n in (29, 42, 53, 79, 103)]
    clips = ("fishingbobber_1", "fishingbobber_2", "fishingbobber_3", "fishbite")
    expected += [assets / "clips" / f"{n}.wav" for n in clips]
    expected += [assets / "default_config"]
    missing = [str(p.relative_to(root)) for p in expected if not p.is_file()]
    if missing:
        print(f"error: missing package assets: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(1)


def check_versions(root: Path, version: str) -> None:
    """Read metadata as data; never execute the PyInstaller version resource."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    observed = {"pyproject.toml": project["version"]}
    packages = [p for p in lock["package"] if p["name"] == project["name"]]
    observed["uv.lock"] = packages[0]["version"] if len(packages) == 1 else None
    source = ast.parse((root / "src/wowfish/__init__.py").read_text(encoding="utf-8"))
    fallback = [
        ast.literal_eval(node.value)
        for node in ast.walk(source)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
        and isinstance(node.value, ast.Constant)
    ]
    observed["src/wowfish/__init__.py"] = fallback[0] if len(fallback) == 1 else None
    resource = ast.parse((root / "packaging/windows_version.txt").read_text(encoding="utf-8"))
    match = re.match(r"^\d+\.\d+\.\d+", version)
    if match is None:
        raise SystemExit(f"Invalid release version: {version!r}")
    numeric = (*(int(n) for n in match[0].split(".")), 0)
    expected = dict.fromkeys(observed, version)
    for node in ast.walk(resource):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "StringStruct" and len(node.args) == 2:
            name, value = map(ast.literal_eval, node.args)
            if name in ("FileVersion", "ProductVersion"):
                key = f"windows_version.txt:{name}"
                observed[key], expected[key] = value, version
        elif node.func.id == "FixedFileInfo":
            for keyword in node.keywords:
                if keyword.arg in ("filevers", "prodvers"):
                    key = f"windows_version.txt:{keyword.arg}"
                    observed[key], expected[key] = ast.literal_eval(keyword.value), numeric
    for name in ("FileVersion", "ProductVersion", "filevers", "prodvers"):
        key = f"windows_version.txt:{name}"
        if key not in observed:
            observed[key], expected[key] = None, version if "Version" in name else numeric
    manifest = ET.parse(root / "packaging/windows.manifest")
    identity = manifest.find("{urn:schemas-microsoft-com:asm.v1}assemblyIdentity")
    observed["windows.manifest"] = identity.get("version") if identity is not None else None
    expected["windows.manifest"] = ".".join(map(str, numeric))
    mismatches = [
        f"{path}: {actual!r} (expected {expected[path]!r})"
        for path, actual in observed.items()
        if actual != expected[path]
    ]
    if mismatches:
        raise SystemExit("Release version metadata mismatch:\n" + "\n".join(mismatches))
    notes = root / "docs/releases" / f"v{version}.md"
    if not notes.is_file() or not notes.read_text(encoding="utf-8").strip():
        raise SystemExit(f"Missing release notes: docs/releases/v{version}.md")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    version = installed_version("forever-fishing-zh")
    tag = os.environ.get("GITHUB_REF_NAME", "")
    if os.environ.get("GITHUB_REF", "").startswith("refs/tags/") and tag != f"v{version}":
        raise SystemExit(f"Release tag {tag!r} does not match package version {version!r}")
    check_versions(root, version)
    check_assets_present(root)
    print(f"prerelease={'true' if is_prerelease(version) else 'false'}")


if __name__ == "__main__":
    main()
