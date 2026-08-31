"""On-disk cache locations.

Normalized intermediates and thumbnails are derived data: expensive to make,
cheap to remake, and never worth putting in the project file. They live under
the platform cache directory so that clearing them is an ordinary thing a user
or a cleanup tool can do without breaking a project.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "simple-video-editor"


def _root() -> Path:
    override = os.environ.get("SVE_CACHE_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP_NAME
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / APP_NAME


def cache_dir(*parts: str) -> Path:
    path = _root().joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_size_bytes() -> int:
    root = _root()
    if not root.exists():
        return 0
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


def clear_cache() -> None:
    root = _root()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


__all__ = ["APP_NAME", "cache_dir", "cache_size_bytes", "clear_cache"]
