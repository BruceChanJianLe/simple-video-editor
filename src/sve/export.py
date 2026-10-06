"""Export: normalized clips -> trim -> concat -> overlay shapes -> encode.

One ffmpeg invocation, one encode pass, one filter graph.

**Where shape positioning lives.** Each shape is rasterized by the shared
renderer to a full-frame transparent PNG, so its position is baked into the
image and every ``overlay`` is at offset ``0:0``. The filter graph therefore
carries no coordinate arithmetic at all - if preview and export disagree about
where a shape is, the bug is in one place, not two.

**Why overlay comes after concat.** ``overlay``'s ``enable`` expression is
evaluated against the *output* timeline's clock. Applying overlays per clip,
before concatenation, would gate them on each clip's local time and put every
annotation after the first clip at the wrong moment.

**Time conversion.** ``Shape.start``/``end`` are in their source file's
timebase - the same clock as ``in_point``. A clip's speed compresses or
stretches that span onto the output timeline, so the global time at which a
shape appears is::

    global = clip_offset + (shape.start - clip.in_point) / clip.speed

which is also the only place in the codebase that conversion happens.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QSize

from .ffmpeg_run import Cancelled, CancelToken, run_ffmpeg
from .geometry import even
from .model import Clip, Project, Shape, clamp_speed
from .normalize import normalize_clip, normalized_path
from .render import render_to_image

# Past this many separate overlay inputs the command line and filter graph get
# unwieldy and ffmpeg's per-input overhead starts to dominate, so shapes are
# flattened into per-interval composites instead. Not optimized for up front:
# the simple path is the common one.
MAX_OVERLAY_INPUTS = 40

@dataclass
class EncodeSettings:
    """Encoder knobs for one export.

    The defaults are the delivery settings. The two other callers want
    something different for honest reasons: the full-timeline preview wants
    speed and a smaller frame, and the export-vs-preview verification wants
    lossless output so that two encodes of the same picture are identical
    outside the annotated pixels.
    """

    crf: int = 20
    preset: str = "medium"
    audio_bitrate: str = "192k"
    # Final output scale, 1.0 for delivery. The timeline preview renders at a
    # fraction; shapes are still rasterized at full resolution and scaled with
    # the picture, so the preview cannot disagree with the real export.
    scale: float = 1.0

    def video_args(self) -> list[str]:
        return [
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", str(self.crf),
            "-preset", self.preset,
            "-c:a", "aac",
            "-b:a", self.audio_bitrate,
            "-movflags", "+faststart",
        ]


PREVIEW_ENCODE = EncodeSettings(crf=28, preset="veryfast", audio_bitrate="128k", scale=0.5)
LOSSLESS_ENCODE = EncodeSettings(crf=0, preset="ultrafast")

COLOR_ARGS = [
    "-colorspace", "bt709",
    "-color_primaries", "bt709",
    "-color_trc", "bt709",
    "-color_range", "tv",
]


@dataclass
class TimedOverlay:
    """One rasterized PNG and the output-timeline window it is shown in."""

    image_path: Path
    start: float
    end: float


@dataclass
class ExportPlan:
    project: Project
    output_path: Path
    overlays: list[TimedOverlay] = field(default_factory=list)
    inputs: list[list[str]] = field(default_factory=list)
    filter_graph: str = ""
    total_duration: float = 0.0
    encode: EncodeSettings = field(default_factory=EncodeSettings)


def global_range(project: Project, clip: Clip, shape: Shape) -> tuple[float, float]:
    """Map a shape's source-time range onto the output timeline.

    Clamped to the clip's trimmed span: a shape whose range extends past the
    out point must not bleed onto the next clip. Speed - the clip's base rate
    and any speed sections - retimes the span through
    :meth:`Clip.output_time`, so a shape over sped-up content is shown for
    proportionally less output time; it stays attached to the same source
    frames.
    """
    offset = project.clip_offset(clip.id)
    return offset + clip.output_time(shape.start), offset + clip.output_time(shape.end)


def atempo_chain(speed: float) -> str:
    """``atempo`` filters multiplying to ``speed``, each within [0.5, 2.0].

    ``atempo`` rejects factors outside that range, so 0.25x is expressed as
    two 0.5x stages. Within SPEED_MIN..SPEED_MAX the chain is at most two
    filters long.
    """
    factors: list[float] = []
    remaining = clamp_speed(speed)
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return ",".join(f"atempo={f:.6f}" for f in factors)


def visible_shapes_for(clip: Clip) -> list[Shape]:
    """Shapes with a non-empty overlap with the clip's trimmed span."""
    return [
        s for s in clip.shapes
        if s.end > s.start and s.end > clip.in_point and s.start < clip.out_point
    ]


def rasterize_overlays(project: Project, work_dir: Path) -> list[TimedOverlay]:
    """Render every shape to a full-frame transparent PNG.

    Uses the same :func:`sve.render.draw_shape` the preview uses, at the
    project's output resolution.
    """
    size = QSize(project.output.width, project.output.height)
    overlays: list[TimedOverlay] = []

    entries: list[tuple[Shape, float, float]] = []
    for clip in project.clips:
        for shape in visible_shapes_for(clip):
            start, end = global_range(project, clip, shape)
            if end > start:
                entries.append((shape, start, end))

    if len(entries) <= MAX_OVERLAY_INPUTS:
        for index, (shape, start, end) in enumerate(entries):
            path = work_dir / f"shape_{index:04d}.png"
            render_to_image([shape], size).save(str(path), "PNG")
            overlays.append(TimedOverlay(image_path=path, start=start, end=end))
        return overlays

    # Fallback: flatten into one PNG per interval between shape boundaries, so
    # the number of inputs depends on how many *distinct* moments there are
    # rather than how many shapes.
    boundaries = sorted({t for _, start, end in entries for t in (start, end)})
    for index in range(len(boundaries) - 1):
        lo, hi = boundaries[index], boundaries[index + 1]
        if hi - lo < 1e-6:
            continue
        mid = (lo + hi) / 2.0
        active = [shape for shape, start, end in entries if start <= mid < end]
        if not active:
            continue
        path = work_dir / f"interval_{index:04d}.png"
        render_to_image(active, size).save(str(path), "PNG")
        overlays.append(TimedOverlay(image_path=path, start=lo, end=hi))
    return overlays


def build_plan(
    project: Project,
    output_path: Path,
    work_dir: Path,
    encode: EncodeSettings | None = None,
) -> ExportPlan:
    """Assemble the input list and filter graph for the whole timeline."""
    if not project.clips:
        raise ValueError("Nothing to export: the timeline is empty.")

    spec = project.output
    plan = ExportPlan(project=project, output_path=output_path,
                      encode=encode or EncodeSettings())
    plan.total_duration = project.total_duration
    if plan.total_duration <= 0:
        raise ValueError("Nothing to export: every clip has zero duration.")

    segments: list[str] = []
    graph: list[str] = []

    for index, clip in enumerate(project.clips):
        source = normalized_path(clip, spec)
        if clip.kind == "image":
            plan.inputs.append([
                "-loop", "1",
                "-framerate", str(spec.fps),
                "-t", f"{clip.duration:.6f}",
                "-i", str(source),
            ])
            graph.append(
                f"[{index}:v]fps={spec.fps},format=yuv420p,setsar=1,"
                f"setpts=PTS-STARTPTS[v{index}]"
            )
            # A still has no audio of its own; synthesise exactly its duration
            # so the concat filter gets matching stream counts.
            graph.append(
                f"anullsrc=channel_layout=stereo:sample_rate={spec.sample_rate}:"
                f"d={clip.duration:.6f},asetpts=PTS-STARTPTS[a{index}]"
            )
        else:
            plan.inputs.append(["-i", str(source)])
            # One concat entry per constant-speed piece of the clip. A clip
            # without speed sections is one piece, and its chain is exactly
            # the graph this exporter always produced. With sections the input
            # is split (one decode, several branches) and each piece is
            # trimmed and retimed on its own.
            pieces = clip.speed_segments()
            if not pieces:  # zero-length trim; preserved as an empty chain
                pieces = [(clip.in_point, clip.out_point, clamp_speed(clip.speed))]
            if len(pieces) > 1:
                v_heads = [f"v{index}p{k}" for k in range(len(pieces))]
                a_heads = [f"a{index}p{k}" for k in range(len(pieces))]
                graph.append(f"[{index}:v]split={len(pieces)}"
                             + "".join(f"[{h}]" for h in v_heads))
                graph.append(f"[{index}:a]asplit={len(pieces)}"
                             + "".join(f"[{h}]" for h in a_heads))
            else:
                v_heads = [f"{index}:v"]
                a_heads = [f"{index}:a"]
            for k, (seg_in, seg_out, speed) in enumerate(pieces):
                label = str(index) if len(pieces) == 1 else f"{index}s{k}"
                # Retiming happens *before* the fps filter, so slow motion
                # gets frames duplicated and speed-up gets frames dropped
                # against the project's constant output rate. Audio is retimed
                # with atempo, which resamples without shifting pitch, keeping
                # it in sync with the video by construction.
                setpts = "PTS-STARTPTS" if speed == 1.0 else f"(PTS-STARTPTS)/{speed:.6f}"
                tempo = "" if speed == 1.0 else f"{atempo_chain(speed)},"
                graph.append(
                    f"[{v_heads[k]}]trim=start={seg_in:.6f}:end={seg_out:.6f},"
                    f"setpts={setpts},fps={spec.fps},format=yuv420p,setsar=1[v{label}]"
                )
                graph.append(
                    f"[{a_heads[k]}]atrim=start={seg_in:.6f}:end={seg_out:.6f},"
                    f"asetpts=PTS-STARTPTS,{tempo}aformat=sample_fmts=fltp:"
                    f"sample_rates={spec.sample_rate}:channel_layouts=stereo[a{label}]"
                )
                segments.append(f"[v{label}][a{label}]")
            continue
        segments.append(f"[v{index}][a{index}]")

    count = len(segments)
    if count == 1:
        # A single segment is always labelled [v0][a0]: one clip, one piece.
        graph.append("[v0]null[base]")
        graph.append("[a0]anull[aout]")
    else:
        graph.append(f"{''.join(segments)}concat=n={count}:v=1:a=1[base][aout]")

    plan.overlays = rasterize_overlays(project, work_dir)

    current = "base"
    overlay_input_base = len(plan.inputs)
    for offset, overlay in enumerate(plan.overlays):
        plan.inputs.append(["-i", str(overlay.image_path)])
        stream = overlay_input_base + offset
        label = f"ov{offset}"
        # The overlay input is a single PNG frame, so it hits EOF immediately.
        # repeatlast=1 and eof_action=repeat hold that frame available for the
        # whole timeline; without them the shape composites onto frame 0 only
        # and is absent for the rest of its enable window. `enable` then gates
        # when it is actually drawn, against the *output* clock.
        graph.append(
            f"[{current}][{stream}:v]overlay=0:0:format=auto:eof_action=repeat:"
            f"repeatlast=1:enable='between(t,{overlay.start:.6f},{overlay.end:.6f})'"
            f"[{label}]"
        )
        current = label

    tail = f"[{current}]"
    if plan.encode.scale != 1.0:
        # Downscale *after* compositing, so the preview shows exactly the
        # delivery frame at a smaller size rather than a differently-composed
        # one. Dimensions forced even for yuv420p chroma subsampling.
        width = even(int(spec.width * plan.encode.scale))
        height = even(int(spec.height * plan.encode.scale))
        tail += f"scale={width}:{height}:flags=bicubic,"
    plan.filter_graph = ";".join([*graph, f"{tail}format=yuv420p[vout]"])
    return plan


def plan_to_args(plan: ExportPlan) -> list[str]:
    args: list[str] = ["-y", "-loglevel", "error"]
    for entry in plan.inputs:
        args += entry
    args += [
        "-filter_complex", plan.filter_graph,
        "-map", "[vout]",
        "-map", "[aout]",
        *plan.encode.video_args(),
        *COLOR_ARGS,
        "-r", str(plan.project.output.fps),
        "-ar", str(plan.project.output.sample_rate),
        "-t", f"{plan.total_duration:.6f}",
        str(plan.output_path),
    ]
    return args


def export_project(
    project: Project,
    output_path: str | Path,
    *,
    on_progress: Callable[[float], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    cancel: CancelToken | None = None,
    encode: EncodeSettings | None = None,
    keep_work_dir: bool = False,
) -> Path:
    """Render the whole timeline to a single mp4.

    Normalizes any clip that is not already cached, then runs one encode.
    Progress spans both phases, weighted by how long each tends to take.
    """
    project = project.copy()  # detach from live GUI edits for the run's duration
    output_path = Path(output_path)
    work_dir = Path(tempfile.mkdtemp(prefix="sve-export-"))

    def status(message: str) -> None:
        if on_status is not None:
            on_status(message)

    try:
        pending = [c for c in project.clips if not normalized_path(c, project.output).exists()]
        # Normalization is roughly real-time-ish and the encode is faster, so
        # split the bar between them rather than jumping from 0 to 100.
        normalize_share = 0.5 if pending else 0.0

        for index, clip in enumerate(pending):
            if cancel is not None and cancel.cancelled:
                raise Cancelled()
            status(f"Preparing {clip.name} ({index + 1}/{len(pending)})")

            def clip_progress(fraction: float, i=index) -> None:
                if on_progress is not None:
                    on_progress(normalize_share * (i + fraction) / len(pending))

            normalize_clip(clip, project.output, cancel=cancel, on_progress=clip_progress)

        status("Rendering annotations")
        plan = build_plan(project, output_path, work_dir, encode)

        status("Encoding")

        def encode_progress(fraction: float) -> None:
            if on_progress is not None:
                on_progress(normalize_share + (1.0 - normalize_share) * fraction)

        run_ffmpeg(
            plan_to_args(plan),
            total_seconds=plan.total_duration,
            on_progress=encode_progress if on_progress else None,
            cancel=cancel,
        )
        if on_progress is not None:
            on_progress(1.0)
        return output_path
    except BaseException:
        # A cancelled or failed export must not leave a truncated file that
        # looks like a successful render.
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        raise
    finally:
        if not keep_work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


__all__ = [
    "LOSSLESS_ENCODE",
    "MAX_OVERLAY_INPUTS",
    "PREVIEW_ENCODE",
    "EncodeSettings",
    "ExportPlan",
    "TimedOverlay",
    "atempo_chain",
    "build_plan",
    "export_project",
    "global_range",
    "plan_to_args",
    "rasterize_overlays",
    "visible_shapes_for",
]
