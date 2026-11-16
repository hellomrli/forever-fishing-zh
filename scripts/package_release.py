"""Create the Windows portable archive, provenance and release checksums."""

from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

from wowfish import __version__


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    dist = root / "dist"
    executables = [dist / "ForeverFishing.exe", dist / "ForeverFishing-CLI.exe"]
    for path in executables:
        if not path.is_file() or path.stat().st_size < 1_000_000:
            raise SystemExit(f"Missing or incomplete executable: {path}")
    info = (
        f"Forever Fishing {__version__}\nTarget: Windows x64\n"
        f"Commit: {os.environ.get('GITHUB_SHA', 'local')}\n"
    )
    archive = dist / f"ForeverFishing-v{__version__}-Windows-x64.zip"
    with zipfile.ZipFile(
        archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as package:
        for path in executables:
            package.write(path, "ForeverFishing/" + path.name)
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            package.write(root / name, "ForeverFishing/" + name)
        package.write(root / "docs" / "Windows使用说明.md", "ForeverFishing/中文使用说明.txt")
        package.writestr("ForeverFishing/BUILD_INFO.txt", info)
    (dist / "BUILD_INFO.txt").write_text(info, encoding="utf-8")
    assets = [*executables, archive, dist / "BUILD_INFO.txt"]
    checksums = []
    for path in assets:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        checksums.append(f"{digest}  {path.name}")
    (dist / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    print("\n".join(checksums))


if __name__ == "__main__":
    main()
