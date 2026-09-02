#!/usr/bin/env python3
"""
Build the double-clickable Windows executable for intan_gui.py.

    python build_exe.py                 # single .exe, ffmpeg bundled if found
    python build_exe.py --onedir        # folder build: much faster to start
    python build_exe.py --no-ffmpeg     # smaller .exe, ffmpeg must be on PATH
    python build_exe.py --ffmpeg C:\\path\\to\\ffmpeg.exe

The result lands in dist/. Nothing is installed on the target machine: copy the
.exe anywhere and double-click it.

Trade-off worth knowing before you pick: --onefile gives you the single file you
probably want, but Windows unpacks ~200 MB to a temp folder on every launch, so
the window takes 10-20 s to appear the first time (faster afterwards, while the
files stay cached). --onedir starts in about a second but is a folder you must
keep together. Both need no installer.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY = HERE / "intan_gui.py"

# Pulled in dynamically, so PyInstaller's static analysis can miss them.
HIDDEN = ["scipy._lib.array_api_compat.numpy.fft", "scipy.special._special_ufuncs"]

# Nothing here is used by an Agg-only, tkinter-only app; excluding them keeps
# the executable to roughly a third of its otherwise-bloated size.
EXCLUDE = [
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "gtk", "IPython", "jupyter",
    "notebook", "pandas", "pytest", "sphinx", "docutils", "PIL.ImageQt",
    "matplotlib.backends.backend_qt5agg", "matplotlib.backends.backend_qtagg",
    "matplotlib.backends.backend_webagg", "matplotlib.backends.backend_gtk3agg",
    "sqlite3", "pydoc_data",
]


def find_ffmpeg(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    w = shutil.which("ffmpeg")
    if not w:
        return None
    # WinGet installs a shim; resolve it so we bundle the real binary.
    real = Path(os.path.realpath(w))
    return real if real.is_file() else Path(w)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--onedir", action="store_true",
                    help="folder build instead of a single file (starts faster)")
    ap.add_argument("--no-ffmpeg", action="store_true",
                    help="do not bundle ffmpeg")
    ap.add_argument("--ffmpeg", default=None, help="path to ffmpeg.exe to bundle")
    ap.add_argument("--name", default="IntanConverter", help="executable name")
    ap.add_argument("--console", action="store_true",
                    help="keep a console window (useful for debugging)")
    args = ap.parse_args()

    if not ENTRY.is_file():
        print(f"missing {ENTRY}", file=sys.stderr)
        return 1
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed. Install it with:\n"
              "    pip install pyinstaller", file=sys.stderr)
        return 1

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--name", args.name,
           "--onedir" if args.onedir else "--onefile",
           "--console" if args.console else "--windowed",
           "--distpath", str(HERE / "dist"),
           "--workpath", str(HERE / "build"),
           "--specpath", str(HERE)]

    for m in HIDDEN:
        cmd += ["--hidden-import", m]
    for m in EXCLUDE:
        cmd += ["--exclude-module", m]

    # The engine modules are imported by name, so add them explicitly rather
    # than relying on PyInstaller following the import graph from the GUI.
    for mod in ("dat_to_audio.py", "dat_to_video.py"):
        src = HERE / mod
        if not src.is_file():
            print(f"missing {src}", file=sys.stderr)
            return 1
        cmd += ["--add-data", f"{src}{os.pathsep}."]

    ff = None if args.no_ffmpeg else find_ffmpeg(args.ffmpeg)
    if ff:
        cmd += ["--add-binary", f"{ff}{os.pathsep}."]
        print(f"bundling ffmpeg from {ff} ({ff.stat().st_size / 1e6:.0f} MB)")
    elif not args.no_ffmpeg:
        print("ffmpeg not found - building without it. MP4 export will need "
              "ffmpeg.exe on PATH or beside the executable.")

    cmd.append(str(ENTRY))
    print("\n" + " ".join(cmd) + "\n")
    rc = subprocess.call(cmd)
    if rc != 0:
        return rc

    out = HERE / "dist" / (args.name if args.onedir else args.name + ".exe")
    if out.exists():
        size = (sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
                if out.is_dir() else out.stat().st_size)
        print(f"\nBuilt {out}  ({size / 1e6:.0f} MB)")
        print("Copy it anywhere and double-click. No installation needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
