# -*- mode: python ; coding: utf-8 -*-
"""Native single-file GUI and console builds. Run PyInstaller from the repository root."""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH).resolve().parent
source = root / "src"
datas = copy_metadata("forever-fishing-zh")
datas += collect_data_files("soundcard")
datas += collect_data_files("ttkbootstrap")
datas += [(str(source / "wowfish" / "assets"), "wowfish/assets")]
hidden = ["PIL._tkinter_finder"]
excludes = ["matplotlib", "torch", "torchvision", "pytest", "IPython"]
manifest = str(root / "packaging" / "windows.manifest") if sys.platform == "win32" else None
version_info = str(root / "packaging" / "windows_version.txt") if sys.platform == "win32" else None

cli = Analysis(
    [str(source / "wowfish" / "__main__.py")],
    pathex=[str(source)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
cli_pyz = PYZ(cli.pure)
cli_exe = EXE(
    cli_pyz,
    cli.scripts,
    cli.binaries,
    cli.datas,
    [],
    name="ForeverFishing-CLI",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    manifest=manifest,
)

gui = Analysis(
    [str(root / "packaging" / "gui_entry.py")],
    pathex=[str(source)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
gui_pyz = PYZ(gui.pure)
gui_exe = EXE(
    gui_pyz,
    gui.scripts,
    gui.binaries,
    gui.datas,
    [],
    name="ForeverFishing",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    manifest=manifest,
    version=version_info,
)
