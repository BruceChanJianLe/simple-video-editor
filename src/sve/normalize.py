"""Normalizing sources to the project's output spec.

**Why not stream copy.** The ffmpeg ``concat`` demuxer with ``-c copy`` only
works when every input matches exactly in codec, profile, resolution, pixel
format, frame rate, time base and audio parameters. Real inputs never do. The
failure mode is the dangerous kind: it appears to succeed and produces output
that is corrupt or progressively desynced after the first join. So every clip
is re-encoded to one common spec first, and only then concatenated.

**Normalize on import, not at export.** Import is where the user expects to
wait; export is where they expect not to. Intermediates are cached on disk
keyed by source path, mtime and output spec, so re-opening a project or
re-exporting costs nothing.

**What gets normalized.** A video becomes an mp4 at the project's resolution,
frame rate, pixel format and audio layout - the *whole* source, untrimmed, so
that changing a trim point never invalidates the cache. A still becomes a PNG
at exactly the output resolution; its duration stays a clip property and is
applied at export via ``-loop 1 -t``, so retiming a still is likewise free.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .cache import cache_dir
from .ffmpeg_run import CancelToken, run_ffmpeg
from .model import Clip, OutputSpec

# Aspect preserved, then padded. Never stretched: a 4:3 clip in a 16:9 project
# gets pillarbox bars, not distorted faces.
SCALE_PAD = (
    "scale={w}:{h}:force_original_aspect_ratio=decrease:flags=bicubic,"
    "pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
    "setsar=1"
)

# Colour handling is stated explicitly rather than inferred. Left to ffmpeg,
# a source tagged full-range and an untagged one converge on different matrices
# and the export looks washed out next to the preview.
COLOR_ARGS = [
    "-colorspace", "bt709",
    "-color_primaries", "bt709",
    "-color_trc", "bt709",
    "-color_range", "tv",
]

VIDEO_ENCODE = [
    "-c:v", "libx264",
    "-pix_fmt", "yuv420p",
    "-crf", "18",           # intermediate: a stop better than the final export
    "-preset", "veryfast",  # ...and fast, since quality is spent at the end
    "-g", "60",
]


@dataclass
class NormalizedClip:
    """A cached intermediate for one clip."""

    path: Path
    kind: str  # "video" | "image"


def spec_fingerprint(output: OutputSpec) -> str:
    return output.key()


def _cache_key(source: Path, output: OutputSpec) -> str:
    try:
        stat = source.stat()
        stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        stamp = "missing"
    raw = f"{source.resolve()}|{stamp}|{spec_fingerprint(output)}|v1"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def normalized_path(clip: Clip, output: OutputSpec) -> Path:
    source = Path(clip.source_path)
    suffix = ".png" if clip.kind == "image" else ".mp4"
    return cache_dir("normalized") / f"{_cache_key(source, output)}{suffix}"


def is_normalized(clip: Clip, output: OutputSpec) -> bool:
    path = normalized_path(clip, output)
    return path.exists() and path.stat().st_size > 0


def normalize_clip(
    clip: Clip,
    output: OutputSpec,
    *,
    cancel: CancelToken | None = None,
    on_progress=None,
) -> NormalizedClip:
    """Produce (or reuse) the normalized intermediate for ``clip``."""
    target = normalized_path(clip, output)
    kind = clip.kind
    if target.exists() and target.stat().st_size > 0:
        return NormalizedClip(path=target, kind=kind)

    # Write to a temporary name and rename on success, so a cancelled or
    # failed run can never leave a half-written file that looks like a hit.
    # The real extension stays last: ffmpeg picks the muxer from it, and a
    # trailing ".partial" makes it refuse to guess a format at all.
    partial = target.with_suffix(f".partial{target.suffix}")
    partial.unlink(missing_ok=True)

    args = (
        _image_args(clip, output, partial)
        if kind == "image"
        else _video_args(clip, output, partial)
    )
    try:
        run_ffmpeg(
            args,
            total_seconds=clip.info.duration if kind == "video" else None,
            on_progress=on_progress,
            cancel=cancel,
        )
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    os.replace(partial, target)
    return NormalizedClip(path=target, kind=kind)


def _video_args(clip: Clip, output: OutputSpec, target: Path) -> list[str]:
    video_filter = (
        SCALE_PAD.format(w=output.width, h=output.height)
        # fps= resamples variable frame rate to constant. Screen recordings are
        # routinely VFR, and concatenating VFR segments drifts.
        + f",fps={output.fps}"
    )
    args = ["-y", "-loglevel", "error", "-i", str(clip.source_path)]

    if not clip.info.has_audio:
        # Silence, so every normalized clip has exactly one audio stream and
        # the concat filter sees a consistent stream count.
        args += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={output.sample_rate}"]
        args += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    else:
        args += ["-map", "0:v:0", "-map", "0:a:0"]

    args += ["-vf", video_filter, *VIDEO_ENCODE, *COLOR_ARGS]
    args += [
        "-r", str(output.fps),
        "-c:a", "aac", "-b:a", "192k",
        "-ar", str(output.sample_rate), "-ac", "2",
        "-video_track_timescale", "90000",
        str(target),
    ]
    return args


def _image_args(clip: Clip, output: OutputSpec, target: Path) -> list[str]:
    return [
        "-y", "-loglevel", "error",
        "-i", str(clip.source_path),
        "-vf", SCALE_PAD.format(w=output.width, h=output.height),
        "-frames:v", "1",
        "-update", "1",
        str(target),
    ]


def clips_needing_normalization(clips: list[Clip], output: OutputSpec) -> list[Clip]:
    seen: set[Path] = set()
    pending: list[Clip] = []
    for clip in clips:
        path = normalized_path(clip, output)
        if path in seen:
            continue
        seen.add(path)
        if not is_normalized(clip, output):
            pending.append(clip)
    return pending


__all__ = [
    "SCALE_PAD",
    "NormalizedClip",
    "clips_needing_normalization",
    "is_normalized",
    "normalize_clip",
    "normalized_path",
]
