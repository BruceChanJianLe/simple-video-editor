"""ffprobe wrapper.

Everything downstream - coordinate normalization, scale/pad filters, the
project's default output spec - depends on getting *display* dimensions right
here. Two things routinely break that:

* **Rotation metadata.** Phone video is stored landscape with a display matrix
  that the player applies at draw time. A 1920x1080 stream tagged -90 is a
  1080x1920 video as far as the user (and QVideoWidget, and ffmpeg's autorotate)
  is concerned. We swap here, once, so no other module has to think about it.
* **Variable frame rate.** Screen recordings are routinely VFR, where
  ``r_frame_rate`` is a meaningless upper bound (often 1000/1). We use
  ``avg_frame_rate`` and flag the discrepancy.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .binaries import ffprobe
from .model import SourceInfo

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg", ".wmv"}


class ProbeError(RuntimeError):
    pass


@dataclass
class ProbeResult:
    info: SourceInfo
    kind: str  # "video" | "image"
    is_vfr: bool
    r_fps: float
    codec: str
    raw: dict


def _parse_rate(value: str | None) -> float:
    """Parse ffprobe's ``num/den`` rate strings, tolerating ``0/0``."""
    if not value:
        return 0.0
    try:
        frac = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return float(frac) if frac.denominator else 0.0


def _rotation_of(stream: dict) -> int:
    """Normalized clockwise display rotation in degrees, one of 0/90/180/270.

    ffprobe reports rotation two ways depending on container and version, and
    the two use *opposite* sign conventions:

    * ``tags.rotate`` (legacy mp4/mov tag) - clockwise degrees.
    * ``side_data_list[].rotation`` (Display Matrix) - counter-clockwise.

    Verified empirically rather than taken from the docs, which are ambiguous:
    a 1920x1080 frame with a white marker in its top-left corner, muxed with
    ``-display_rotation 90``, probes as ``rotation=90`` and decodes (with
    ffmpeg's default autorotate) to 1080x1920 with the marker at *bottom*
    left. Top-left -> bottom-left is a counter-clockwise quarter turn, so the
    side-data value is negated to reach the clockwise convention used here.

    We never apply this rotation ourselves - both ffmpeg and Qt autorotate on
    decode. It is recorded so that the display dimension swap below happens
    exactly once, and so the import log can explain a surprising aspect ratio.
    """
    raw: float | None = None

    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            raw = -float(side["rotation"])  # display matrix is counter-clockwise
            break

    if raw is None:
        tag = (stream.get("tags") or {}).get("rotate")
        if tag is not None:
            try:
                raw = float(tag)
            except ValueError:
                raw = None

    if raw is None:
        return 0
    return int(round(raw / 90.0) * 90) % 360


def run_ffprobe(path: str | Path) -> dict:
    cmd = [
        ffprobe(),
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed on {path}: {proc.stderr.strip() or proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned unparseable JSON for {path}") from exc


def probe(path: str | Path) -> ProbeResult:
    path = Path(path)
    if not path.exists():
        raise ProbeError(f"No such file: {path}")

    raw = run_ffprobe(path)
    streams = raw.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ProbeError(f"{path.name} contains no video or image stream.")

    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    codec = video.get("codec_name", "")

    coded_w = int(video.get("width") or 0)
    coded_h = int(video.get("height") or 0)
    rotation = _rotation_of(video)
    if rotation in (90, 270):
        disp_w, disp_h = coded_h, coded_w
    else:
        disp_w, disp_h = coded_w, coded_h

    avg_fps = _parse_rate(video.get("avg_frame_rate"))
    r_fps = _parse_rate(video.get("r_frame_rate"))

    # A still decoded by ffmpeg looks like a 1-frame video with a nonsense
    # frame rate. Classify by suffix first, then corroborate with frame count.
    suffix = path.suffix.lower()
    nb_frames = int(video.get("nb_frames") or 0)
    if suffix in IMAGE_SUFFIXES or (codec in {"png", "mjpeg", "bmp", "webp", "gif", "tiff"} and nb_frames <= 1):
        kind = "image"
    else:
        kind = "video"

    duration = 0.0
    for source in (video.get("duration"), (raw.get("format") or {}).get("duration")):
        try:
            duration = float(source)
            break
        except (TypeError, ValueError):
            continue

    if kind == "image":
        duration = 0.0
        fps = 0.0
        is_vfr = False
    else:
        fps = avg_fps or r_fps
        # r_frame_rate is the least common multiple of observed frame
        # intervals; when it wildly exceeds the average, the file is VFR.
        is_vfr = bool(avg_fps and r_fps and r_fps > avg_fps * 1.15)

    info = SourceInfo(
        duration=duration,
        width=disp_w,
        height=disp_h,
        fps=fps,
        pix_fmt=video.get("pix_fmt", ""),
        rotation=rotation,
        has_audio=has_audio,
        coded_width=coded_w,
        coded_height=coded_h,
    )
    return ProbeResult(info=info, kind=kind, is_vfr=is_vfr, r_fps=r_fps, codec=codec, raw=raw)


def format_probe(path: str | Path, result: ProbeResult) -> str:
    i = result.info
    parts = [f"{result.kind}", f"{i.width}x{i.height}"]
    if result.kind == "video":
        parts.append(f"{i.duration:.3f}s")
        parts.append(f"{i.fps:.3f}fps" + (f" (VFR, r={result.r_fps:.0f})" if result.is_vfr else ""))
        parts.append("audio" if i.has_audio else "silent")
    parts.append(i.pix_fmt or "?")
    parts.append(result.codec)
    if i.rotation:
        parts.append(f"rot={i.rotation} coded={i.coded_width}x{i.coded_height}")
    return f"{Path(path).name}: " + ", ".join(parts)


__all__ = ["IMAGE_SUFFIXES", "VIDEO_SUFFIXES", "ProbeError", "ProbeResult", "format_probe", "probe"]
