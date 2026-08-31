"""Clip thumbnails, extracted with ffmpeg on a worker thread.

Thumbnails are cosmetic, so every failure here is swallowed into "no
thumbnail" rather than surfaced as an error. They are cached on disk keyed by
source path, mtime and the requested timestamp, so reopening a project does
not re-decode anything.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QPixmap

from .binaries import ffmpeg
from .cache import cache_dir

# Sized so a thumbnail plus a two-line label fits the timeline panel's default
# width without the label being elided into uselessness.
THUMB_WIDTH = 112
THUMB_HEIGHT = 63


def _key(source: Path, at: float) -> str:
    try:
        mtime = source.stat().st_mtime_ns
    except OSError:
        mtime = 0
    raw = f"{source}|{mtime}|{at:.3f}|{THUMB_WIDTH}x{THUMB_HEIGHT}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class _Signals(QObject):
    """Signal carrier for the worker jobs.

    Owned by the :class:`ThumbnailLoader`, deliberately *not* by the job.
    ``QThreadPool`` deletes a ``QRunnable`` as soon as ``run`` returns, so a
    signals object held by the job is destroyed while its cross-thread
    emission is still queued, and Qt drops the pending delivery. The symptom
    is thumbnails that are generated on disk, and never appear.
    """

    done = Signal(str, str)  # clip_id, png path ("" on failure)


class ThumbnailJob(QRunnable):
    def __init__(self, clip_id: str, source: str, at: float, signals: _Signals) -> None:
        super().__init__()
        self.clip_id = clip_id
        self.source = Path(source)
        self.at = at
        self.signals = signals

    def run(self) -> None:
        out = cache_dir("thumbs") / f"{_key(self.source, self.at)}.png"
        if not out.exists() and not self._extract(out):
            self.signals.done.emit(self.clip_id, "")
            return
        self.signals.done.emit(self.clip_id, str(out))

    def _extract(self, out: Path) -> bool:
        # -ss before -i seeks by keyframe, which is approximate but fast; for a
        # thumbnail that is the right trade.
        cmd = [
            ffmpeg(), "-y", "-loglevel", "error",
            "-ss", f"{max(0.0, self.at):.3f}",
            "-i", str(self.source),
            "-frames:v", "1",
            "-vf",
            f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={THUMB_WIDTH}:{THUMB_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black",
            "-update", "1",
            str(out),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0 and out.exists()


class ThumbnailLoader(QObject):
    """Requests thumbnails and reports them back on the GUI thread."""

    ready = Signal(str, QPixmap)  # clip_id, pixmap

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._pending: set[str] = set()
        self._signals = _Signals(self)
        self._signals.done.connect(self._finish)
        # Remembered so a rebuilt list gets its icons back without waiting for
        # ffmpeg again. The list widget is cleared and repopulated on every
        # project change, which drops the icons that were already set.
        self._cache: dict[str, QPixmap] = {}

    def cached(self, clip_id: str) -> QPixmap | None:
        return self._cache.get(clip_id)

    def request(self, clip_id: str, source: str, at: float) -> None:
        token = f"{clip_id}|{at:.3f}"
        if token in self._pending:
            return
        self._pending.add(token)
        self._pool.start(ThumbnailJob(clip_id, source, at, self._signals))

    def _finish(self, clip_id: str, path: str) -> None:
        self._pending = {t for t in self._pending if not t.startswith(f"{clip_id}|")}
        if not path:
            return
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            self._cache[clip_id] = pixmap
            self.ready.emit(clip_id, pixmap)

    def shutdown(self) -> None:
        self._pool.clear()
        self._pool.waitForDone(2000)


__all__ = ["THUMB_HEIGHT", "THUMB_WIDTH", "ThumbnailLoader"]
