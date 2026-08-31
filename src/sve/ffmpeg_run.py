"""Running ffmpeg with progress reporting and cancellation.

Progress comes from ``-progress pipe:1 -nostats``, which emits key=value lines
on stdout. Note that ``out_time_ms`` is misnamed: ffmpeg writes *microseconds*
into it, the same value as ``out_time_us``. Reading it as milliseconds makes a
progress bar advance at a thousandth of the real rate, which looks like a hang.
We prefer ``out_time_us`` and treat ``out_time_ms`` as microseconds too.
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .binaries import ffmpeg


class FFmpegError(RuntimeError):
    def __init__(self, message: str, *, command: list[str], log: str) -> None:
        super().__init__(message)
        self.command = command
        self.log = log


class Cancelled(Exception):
    """Raised when a run was stopped on request rather than failing."""


@dataclass
class CancelToken:
    """Cooperative cancellation shared between the GUI and a worker thread."""

    _event: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


def run_ffmpeg(
    args: Iterable[str],
    *,
    total_seconds: float | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancel: CancelToken | None = None,
    log_lines: int = 60,
) -> None:
    """Run ffmpeg to completion.

    ``on_progress`` receives a 0..1 fraction whenever ffmpeg reports one.
    Raises :class:`Cancelled` if ``cancel`` fires, :class:`FFmpegError` on a
    non-zero exit. Only the tail of stderr is kept: ffmpeg is verbose, and the
    last few lines are what actually says why it failed.
    """
    command = [ffmpeg(), "-hide_banner", "-nostdin", *args]
    if on_progress is not None:
        command = [*command[:1], "-progress", "pipe:1", "-nostats", *command[1:]]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    tail: list[str] = []

    def drain_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            tail.append(line.rstrip())
            del tail[:-log_lines]

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    was_cancelled = False
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if cancel is not None and cancel.cancelled:
                was_cancelled = True
                break
            if on_progress is None or total_seconds is None or total_seconds <= 0:
                continue
            key, _, value = line.strip().partition("=")
            micros = _progress_micros(key, value)
            if micros is not None:
                on_progress(min(1.0, max(0.0, micros / 1e6 / total_seconds)))
    finally:
        if was_cancelled or (cancel is not None and cancel.cancelled):
            _terminate(process)
            stderr_thread.join(timeout=2)
            raise Cancelled()
        process.wait()
        stderr_thread.join(timeout=2)

    if process.returncode != 0:
        raise FFmpegError(
            f"ffmpeg exited with status {process.returncode}",
            command=command,
            log="\n".join(tail),
        )


def _progress_micros(key: str, value: str) -> float | None:
    # Both keys carry microseconds despite the name of the second.
    if key not in ("out_time_us", "out_time_ms"):
        return None
    try:
        micros = float(value)
    except ValueError:
        return None
    return micros if micros >= 0 else None


def _terminate(process: subprocess.Popen) -> None:
    """Stop ffmpeg, escalating if it ignores the polite request."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=3)
    except OSError:
        pass


__all__ = ["CancelToken", "Cancelled", "FFmpegError", "run_ffmpeg"]
