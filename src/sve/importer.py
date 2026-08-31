"""Turning files on disk into clips.

Import probes the file, decides whether it is footage or a still, and derives
the clip's initial trim. The first import also fixes the project's output
resolution and frame rate, on the theory that a project assembled mostly from
one camera should not be silently upscaled or resampled.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .geometry import even
from .model import (
    DEFAULT_FPS,
    DEFAULT_HEIGHT,
    DEFAULT_IMAGE_DURATION,
    DEFAULT_WIDTH,
    Clip,
    Project,
)
from .probe import IMAGE_SUFFIXES, VIDEO_SUFFIXES, ProbeError, ProbeResult, probe

MEDIA_FILTER = (
    "Media files ("
    + " ".join(f"*{s}" for s in sorted(VIDEO_SUFFIXES | IMAGE_SUFFIXES))
    + ");;Video ("
    + " ".join(f"*{s}" for s in sorted(VIDEO_SUFFIXES))
    + ");;Images ("
    + " ".join(f"*{s}" for s in sorted(IMAGE_SUFFIXES))
    + ");;All files (*)"
)


@dataclass
class ImportReport:
    clip: Clip
    result: ProbeResult
    notes: list[str]


def build_clip(path: str | Path, *, image_duration: float = DEFAULT_IMAGE_DURATION) -> ImportReport:
    """Probe ``path`` and construct an untrimmed clip for it."""
    path = Path(path).expanduser().resolve()
    result = probe(path)
    notes: list[str] = []

    if result.kind == "image":
        clip = Clip(
            kind="image",
            source_path=str(path),
            in_point=0.0,
            out_point=image_duration,
            info=result.info,
        )
    else:
        duration = result.info.duration
        if duration <= 0:
            raise ProbeError(f"{path.name}: could not determine duration.")
        clip = Clip(
            kind="video",
            source_path=str(path),
            in_point=0.0,
            out_point=duration,
            info=result.info,
        )
        if result.is_vfr:
            notes.append(
                f"{path.name} has a variable frame rate (average "
                f"{result.info.fps:.2f}fps). It will be resampled to the "
                f"project frame rate on export."
            )
        if not result.info.has_audio:
            notes.append(f"{path.name} has no audio; silence will be added on export.")

    if result.info.rotation:
        notes.append(
            f"{path.name} carries {result.info.rotation}deg rotation metadata; "
            f"treating it as {result.info.width}x{result.info.height}."
        )
    return ImportReport(clip=clip, result=result, notes=notes)


def apply_first_import_defaults(project: Project, report: ImportReport) -> list[str]:
    """Adopt the first imported file's resolution and frame rate.

    Only once, and only if the user has not pinned the output spec. A still as
    the first import fixes nothing but the resolution, since it has no frame
    rate to adopt; the default 30fps stands.
    """
    if project.output_locked or project.clips:
        return []

    notes: list[str] = []
    info = report.result.info
    width, height = even(info.width), even(info.height)
    if width > 0 and height > 0:
        project.output.width = width
        project.output.height = height
    else:
        project.output.width, project.output.height = DEFAULT_WIDTH, DEFAULT_HEIGHT

    if report.result.kind == "video" and info.fps > 0:
        # A VFR file's average rate is rarely a sensible output rate; round to
        # a common one so the timeline is not stuck at, say, 15.19fps.
        project.output.fps = _sensible_fps(info.fps) if report.result.is_vfr else round(info.fps, 3)
    else:
        project.output.fps = DEFAULT_FPS

    notes.append(
        f"Project output set from first import: "
        f"{project.output.width}x{project.output.height} @ {project.output.fps:g}fps."
    )
    return notes


COMMON_RATES = (23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0)


def _sensible_fps(measured: float) -> float:
    """Snap an odd measured rate to the nearest common one, never downward
    past 24fps - a 15fps average from a mostly-static screen recording should
    still export at a normal rate."""
    if measured >= 24.0:
        return min(COMMON_RATES, key=lambda r: abs(r - measured))
    return DEFAULT_FPS


__all__ = [
    "MEDIA_FILTER",
    "ImportReport",
    "apply_first_import_defaults",
    "build_clip",
]
