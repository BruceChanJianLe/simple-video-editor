"""Project document model.

The whole editor state is one serializable ``Project`` document. Undo/redo is a
stack of deep copies of it (see :mod:`sve.undo`); at the scale this tool targets
(a five minute video, a handful of clips) that is cheaper than a command
pattern and cannot drift out of sync with the real state.

Two invariants that the rest of the codebase depends on:

1. **All shape geometry is normalized 0.0-1.0 against the video frame.**
   Never store widget pixels. ``stroke_width`` and ``font_size`` are likewise
   normalized, as a fraction of frame *height*, so that the shared renderer is
   a pure function of (shape, target size) and preview and export cannot
   disagree about scale.

2. **Shape times are in the source file's own timebase**, i.e. the same clock
   ``in_point``/``out_point`` use. They are not global timeline times and not
   offsets from ``in_point``. Retrimming a clip or reordering the timeline
   therefore leaves annotations attached to the content they annotate. The
   conversion to global timeline time happens once, at export.
"""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

PROJECT_VERSION = 1

ShapeType = Literal["arrow", "rect", "ellipse", "text"]
ClipKind = Literal["video", "image"]

DEFAULT_IMAGE_DURATION = 3.0
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_FPS = 30.0
DEFAULT_SAMPLE_RATE = 48000

# Normalized against frame height. 0.005 is ~5px at 1080p.
DEFAULT_STROKE_WIDTH = 0.005
DEFAULT_FONT_SIZE = 0.05
DEFAULT_STROKE_COLOR = "#ff3b30ff"
DEFAULT_FILL_COLOR = "#00000000"
DEFAULT_TEXT_COLOR = "#ff3b30ff"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Shape:
    """A single annotation.

    ``points`` are normalized 0..1 pairs whose meaning depends on ``type``:

    * ``arrow``   - ``[tail, head]``
    * ``rect``    - ``[corner, opposite_corner]``
    * ``ellipse`` - ``[corner, opposite_corner]`` of the bounding box
    * ``text``    - ``[anchor]``, the top-left of the text block
    """

    type: ShapeType
    points: list[list[float]]
    start: float = 0.0
    end: float = 0.0
    stroke_color: str = DEFAULT_STROKE_COLOR
    stroke_width: float = DEFAULT_STROKE_WIDTH
    fill_color: str = DEFAULT_FILL_COLOR
    text: str = ""
    font_size: float = DEFAULT_FONT_SIZE
    id: str = field(default_factory=new_id)

    def visible_at(self, t: float) -> bool:
        """True when source-time ``t`` falls in the half-open range [start, end).

        Half-open so that back-to-back shapes never both show on one frame.
        """
        return self.start <= t < self.end

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "start": self.start,
            "end": self.end,
            "points": [[float(x), float(y)] for x, y in self.points],
            "stroke_color": self.stroke_color,
            "stroke_width": self.stroke_width,
            "fill_color": self.fill_color,
            "text": self.text,
            "font_size": self.font_size,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Shape:
        return cls(
            id=d.get("id") or new_id(),
            type=d["type"],
            points=[[float(x), float(y)] for x, y in d["points"]],
            start=float(d.get("start", 0.0)),
            end=float(d.get("end", 0.0)),
            stroke_color=d.get("stroke_color", DEFAULT_STROKE_COLOR),
            stroke_width=float(d.get("stroke_width", DEFAULT_STROKE_WIDTH)),
            fill_color=d.get("fill_color", DEFAULT_FILL_COLOR),
            text=d.get("text", ""),
            font_size=float(d.get("font_size", DEFAULT_FONT_SIZE)),
        )


@dataclass
class SourceInfo:
    """Probe results for a clip's source file.

    Cached in the document so that opening a project does not require
    re-probing every file, and so the timeline can render without touching
    disk. ``width``/``height`` are *display* dimensions, i.e. already swapped
    if the file carries 90/270 degree rotation metadata.
    """

    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pix_fmt: str = ""
    rotation: int = 0
    has_audio: bool = False
    coded_width: int = 0
    coded_height: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "pix_fmt": self.pix_fmt,
            "rotation": self.rotation,
            "has_audio": self.has_audio,
            "coded_width": self.coded_width,
            "coded_height": self.coded_height,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SourceInfo:
        return cls(
            duration=float(d.get("duration", 0.0)),
            width=int(d.get("width", 0)),
            height=int(d.get("height", 0)),
            fps=float(d.get("fps", 0.0)),
            pix_fmt=d.get("pix_fmt", ""),
            rotation=int(d.get("rotation", 0)),
            has_audio=bool(d.get("has_audio", False)),
            coded_width=int(d.get("coded_width", 0)),
            coded_height=int(d.get("coded_height", 0)),
        )


@dataclass
class Clip:
    kind: ClipKind
    source_path: str
    in_point: float = 0.0
    out_point: float = 0.0
    shapes: list[Shape] = field(default_factory=list)
    info: SourceInfo = field(default_factory=SourceInfo)
    id: str = field(default_factory=new_id)

    @property
    def duration(self) -> float:
        return max(0.0, self.out_point - self.in_point)

    @property
    def name(self) -> str:
        return Path(self.source_path).name

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source_path": self.source_path,
            "in_point": self.in_point,
            "out_point": self.out_point,
            "shapes": [s.to_dict() for s in self.shapes],
            "info": self.info.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Clip:
        return cls(
            id=d.get("id") or new_id(),
            kind=d["kind"],
            source_path=d["source_path"],
            in_point=float(d.get("in_point", 0.0)),
            out_point=float(d.get("out_point", 0.0)),
            shapes=[Shape.from_dict(s) for s in d.get("shapes", [])],
            info=SourceInfo.from_dict(d.get("info", {})),
        )


@dataclass
class OutputSpec:
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    fps: float = DEFAULT_FPS
    sample_rate: int = DEFAULT_SAMPLE_RATE

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "sample_rate": self.sample_rate,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OutputSpec:
        return cls(
            width=int(d.get("width", DEFAULT_WIDTH)),
            height=int(d.get("height", DEFAULT_HEIGHT)),
            fps=float(d.get("fps", DEFAULT_FPS)),
            sample_rate=int(d.get("sample_rate", DEFAULT_SAMPLE_RATE)),
        )

    def key(self) -> str:
        """Stable identity used in the normalized-intermediate cache key."""
        return f"{self.width}x{self.height}@{self.fps:g}:{self.sample_rate}"


@dataclass
class Project:
    version: int = PROJECT_VERSION
    output: OutputSpec = field(default_factory=OutputSpec)
    clips: list[Clip] = field(default_factory=list)
    output_locked: bool = False

    def clip_by_id(self, clip_id: str) -> Clip | None:
        return next((c for c in self.clips if c.id == clip_id), None)

    def clip_index(self, clip_id: str) -> int:
        return next(i for i, c in enumerate(self.clips) if c.id == clip_id)

    def clip_offset(self, clip_id: str) -> float:
        """Start of ``clip_id`` on the assembled output timeline, in seconds."""
        offset = 0.0
        for clip in self.clips:
            if clip.id == clip_id:
                return offset
            offset += clip.duration
        raise KeyError(clip_id)

    @property
    def total_duration(self) -> float:
        return sum(c.duration for c in self.clips)

    def copy(self) -> Project:
        return copy.deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "output": self.output.to_dict(),
            "output_locked": self.output_locked,
            "clips": [c.to_dict() for c in self.clips],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Project:
        version = int(d.get("version", PROJECT_VERSION))
        if version > PROJECT_VERSION:
            raise ValueError(
                f"Project was written by a newer version of the app "
                f"(file version {version}, supported {PROJECT_VERSION})."
            )
        return cls(
            version=PROJECT_VERSION,
            output=OutputSpec.from_dict(d.get("output", {})),
            output_locked=bool(d.get("output_locked", False)),
            clips=[Clip.from_dict(c) for c in d.get("clips", [])],
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Project:
        return cls.from_dict(json.loads(text))

    def save(self, path: str | Path) -> None:
        """Write atomically so an interrupted save cannot destroy the project."""
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: str | Path) -> Project:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


__all__ = [
    "PROJECT_VERSION",
    "Clip",
    "OutputSpec",
    "Project",
    "Shape",
    "SourceInfo",
    "new_id",
    "replace",
]
