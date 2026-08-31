"""Locating the bundled ffmpeg and ffprobe.

Never assume these are on PATH: a packaged .app or AppImage runs with whatever
environment the desktop session happens to have, and a user's system ffmpeg may
be an old build without the filters we rely on. Resolution order:

1. ``SVE_FFMPEG`` / ``SVE_FFPROBE`` env vars (the nix devShell sets these).
2. Alongside the frozen executable - PyInstaller's ``sys._MEIPASS``, or a
   ``bin/`` directory next to the interpreter for an AppImage layout.
3. PATH, as an explicitly last-resort fallback.

If none resolve, raise rather than letting a subprocess fail later with a
confusing FileNotFoundError from deep inside an export.
"""

from __future__ import annotations

import os
import shutil
import sys
from functools import cache
from pathlib import Path

_EXE = ".exe" if sys.platform == "win32" else ""


class BinaryNotFound(RuntimeError):
    pass


def _bundle_roots() -> list[Path]:
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        root = Path(meipass)
        roots += [root, root / "bin"]
    exe_dir = Path(sys.executable).resolve().parent
    roots += [exe_dir, exe_dir / "bin", exe_dir.parent / "bin"]
    # macOS .app bundle: Contents/MacOS/<exe> -> Contents/Resources/bin
    roots.append(exe_dir.parent / "Resources" / "bin")
    return roots


@cache
def find_binary(name: str) -> str:
    env = os.environ.get(f"SVE_{name.upper()}")
    if env and Path(env).exists():
        return env

    for root in _bundle_roots():
        candidate = root / f"{name}{_EXE}"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    found = shutil.which(name)
    if found:
        return found

    raise BinaryNotFound(
        f"Could not find {name!r}. Set SVE_{name.upper()} to its full path, "
        f"or run inside `nix develop`, which provides it."
    )


def ffmpeg() -> str:
    return find_binary("ffmpeg")


def ffprobe() -> str:
    return find_binary("ffprobe")


__all__ = ["BinaryNotFound", "ffmpeg", "ffprobe", "find_binary"]
