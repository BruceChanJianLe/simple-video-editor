"""Milestone 7 gate: the export must agree with the preview renderer.

This is the test the architecture stands on. If it passes, "there is one
renderer, and ffmpeg only composites what it produces" is backed by evidence.

Method, for a frame at time ``t``:

1. Export the project normally, through the real pipeline.
2. Export the same project with no shapes, to get the un-annotated base frame.
3. Build a *reference* frame independently: composite
   ``render_to_image(shapes_visible_at_t)`` onto the base frame with QPainter -
   the same call the preview overlay makes, at the same resolution.
4. Compare the export against that reference.

Three assertions, because they fail for different reasons:

* **Nothing drawn may be missing** (``reference_ink - export_ink``). The strict
  direction, and the one that catches the classic bugs: a shape that is offset,
  scaled wrongly, letterbox-shifted, or gated to the wrong time window loses a
  large fraction of its pixels immediately.
* **Per-pixel mean absolute error** between export and reference. The literal
  "pixel for pixel" claim.
* **Ink bounding box** agreement, which pins position and size to within a
  pixel or two even where MAE would average a small shift away.

One measurement is worth recording, because it dictates how the base frame is
produced. Running the ``overlay`` filter at all shifts the *whole* picture by
about 0.9 levels per channel on average and up to 23 at a sharp colour edge:
overlay round-trips through RGB to blend alpha, and an export with no overlays
never does. Comparing an annotated export against a base that skipped the
overlay chain therefore floods the ink mask with pixels that have nothing to do
with any annotation, which is enough to move a measured bounding box by
hundreds of pixels.

So the base export carries one fully transparent shape. It composites nothing,
but it makes the two pipelines structurally identical, leaving the annotation
itself as the only difference between them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PySide6.QtCore import QRect, QSize
from PySide6.QtGui import QImage, QPainter

from sve.binaries import ffmpeg
from sve.export import LOSSLESS_ENCODE, export_project
from sve.model import Clip, OutputSpec, Project, Shape
from sve.probe import probe
from sve.render import render_to_image

WIDTH, HEIGHT = 1280, 720


@pytest.fixture(scope="session", autouse=True)
def qt_app(qapp):
    return qapp


SHAPES = [
    Shape(type="rect", points=[[0.10, 0.15], [0.45, 0.55]], start=0.0, end=2.0,
          stroke_color="#ff3b30ff", stroke_width=0.008),
    Shape(type="ellipse", points=[[0.55, 0.10], [0.92, 0.48]], start=0.0, end=2.0,
          stroke_color="#ffe000ff", fill_color="#0040ff60", stroke_width=0.006),
    Shape(type="arrow", points=[[0.55, 0.85], [0.90, 0.25]], start=2.0, end=4.0,
          stroke_color="#00ff40ff", stroke_width=0.012),
    Shape(type="text", points=[[0.12, 0.72]], text="Milestone 7", start=4.0, end=6.0,
          stroke_color="#ffffffff", fill_color="#000000c0", font_size=0.10),
]

TIMES = [1.0, 3.0, 5.0]

# Composites nothing, but forces the base export down the same overlay path as
# the annotated one. See the module docstring.
TRANSPARENT = [
    Shape(type="rect", points=[[0.0, 0.0], [1.0, 1.0]], start=0.0, end=6.0,
          stroke_color="#00000000", fill_color="#00000000"),
]


def visible(t: float) -> list[Shape]:
    return [s for s in SHAPES if s.visible_at(t)]


def _project(source: Path, shapes: list[Shape]) -> Project:
    clip = Clip(kind="video", source_path=str(source), in_point=0.0,
                out_point=6.0, info=probe(source).info, shapes=list(shapes))
    return Project(
        output=OutputSpec(width=WIDTH, height=HEIGHT, fps=30.0), clips=[clip]
    )


@pytest.fixture(scope="module")
def exported(assets_dir, tmp_path_factory):
    """Annotated and un-annotated exports, encoded losslessly.

    Lossless so that lossy rate-control noise - which differs between two
    encodes of different content - cannot be mistaken for a positioning error.
    """
    tmp = tmp_path_factory.mktemp("export")
    source = assets_dir / "clip_720p.mp4"
    annotated, base = tmp / "annotated.mp4", tmp / "base.mp4"
    export_project(_project(source, SHAPES), annotated, encode=LOSSLESS_ENCODE)
    export_project(_project(source, TRANSPARENT), base, encode=LOSSLESS_ENCODE)
    return annotated, base


@pytest.fixture(scope="module")
def exported_delivery(assets_dir, tmp_path_factory):
    """The same pair at the real delivery settings, for the colour check."""
    tmp = tmp_path_factory.mktemp("delivery")
    source = assets_dir / "clip_720p.mp4"
    annotated, base = tmp / "annotated.mp4", tmp / "base.mp4"
    export_project(_project(source, SHAPES), annotated)
    export_project(_project(source, TRANSPARENT), base)
    return annotated, base


def frame_at(video: Path, seconds: float, out: Path) -> QImage:
    subprocess.run(
        [ffmpeg(), "-y", "-loglevel", "error", "-ss", f"{seconds:.3f}",
         "-i", str(video), "-frames:v", "1", "-update", "1", str(out)],
        check=True,
    )
    image = QImage(str(out))
    assert not image.isNull(), f"could not read a frame from {video}"
    return image.convertToFormat(QImage.Format.Format_RGB32)


def composite_reference(base: QImage, shapes: list[Shape]) -> QImage:
    """Base frame plus the shared renderer's output - what the preview shows."""
    result = QImage(base)
    painter = QPainter(result)
    painter.drawImage(0, 0, render_to_image(shapes, QSize(base.width(), base.height())))
    painter.end()
    return result


def ink_mask(frame: QImage, base: QImage, threshold: int) -> set[tuple[int, int]]:
    """Pixels where ``frame`` differs from ``base``, summed over RGB."""
    mask: set[tuple[int, int]] = set()
    for y in range(frame.height()):
        for x in range(frame.width()):
            a, b = frame.pixelColor(x, y), base.pixelColor(x, y)
            if (abs(a.red() - b.red()) + abs(a.green() - b.green())
                    + abs(a.blue() - b.blue())) > threshold:
                mask.add((x, y))
    return mask


def bbox(mask: set[tuple[int, int]]) -> QRect:
    xs = [p[0] for p in mask]
    ys = [p[1] for p in mask]
    return QRect(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)


def mean_abs_error(a: QImage, b: QImage, step: int = 2) -> float:
    total = 0
    count = 0
    for y in range(0, a.height(), step):
        for x in range(0, a.width(), step):
            pa, pb = a.pixelColor(x, y), b.pixelColor(x, y)
            total += (abs(pa.red() - pb.red()) + abs(pa.green() - pb.green())
                      + abs(pa.blue() - pb.blue()))
            count += 3
    return total / count


# With both exports running the same overlay chain, the only residual is
# yuv420p chroma rounding, so this can be low enough to catch a faint
# antialiased edge.
INK_THRESHOLD = 24

# Presence/absence tests use a higher bar. A translucent fill over a similarly
# coloured background changes the picture by barely more than INK_THRESHOLD -
# the blue ellipse fill here lands at 24 in the export and 28 in the reference -
# so a couple of levels of chroma rounding flips such pixels in and out of the
# mask and makes a set-difference metric measure rounding rather than geometry.
# Above this threshold only real strokes, text and opaque fills survive, and
# set differences mean what they say. Agreement on the faint pixels is covered
# instead by the mean-absolute-error test, which compares magnitudes.
STRONG_INK = 100


@pytest.mark.parametrize("t", TIMES)
def test_nothing_the_renderer_drew_is_missing_from_the_export(exported, tmp_path, t):
    annotated, base_video = exported
    export_frame = frame_at(annotated, t, tmp_path / f"e{t}.png")
    base_frame = frame_at(base_video, t, tmp_path / f"b{t}.png")
    reference = composite_reference(base_frame, visible(t))

    assert export_frame.size() == reference.size() == QSize(WIDTH, HEIGHT)

    export_ink = ink_mask(export_frame, base_frame, STRONG_INK)
    reference_ink = ink_mask(reference, base_frame, STRONG_INK)
    assert reference_ink, f"no shape was expected to be visible at t={t}"

    # A few percent of edge pixels legitimately disagree: a diagonal
    # antialiased stroke has a high perimeter-to-area ratio and 4:2:0 chroma
    # subsampling rounds its soft edge differently from QPainter's RGB
    # compositing. Measured at 1.9% for the arrow, under 1% for the axis
    # aligned shapes. This stays strict where it matters - a one pixel offset
    # of the 9px arrow would move about 12% of its pixels, and a scale error
    # or a mistimed enable window would move nearly all of them.
    missing = len(reference_ink - export_ink) / len(reference_ink)
    assert missing < 0.03, (
        f"t={t}: {missing:.2%} of the drawn annotation is absent from the "
        f"export - it is offset, scaled or mistimed"
    )

    iou = len(export_ink & reference_ink) / len(export_ink | reference_ink)
    assert iou > 0.90, f"t={t}: annotation geometry differs, IoU={iou:.3f}"


@pytest.mark.parametrize("t", TIMES)
def test_annotation_lands_at_the_same_position_and_size(exported, tmp_path, t):
    """Position and extent to within a pixel or two - the check that a
    letterbox offset or a widget-pixel coordinate bug cannot survive."""
    annotated, base_video = exported
    export_frame = frame_at(annotated, t, tmp_path / f"g{t}.png")
    base_frame = frame_at(base_video, t, tmp_path / f"h{t}.png")
    reference = composite_reference(base_frame, visible(t))

    export_box = bbox(ink_mask(export_frame, base_frame, INK_THRESHOLD))
    reference_box = bbox(ink_mask(reference, base_frame, INK_THRESHOLD))

    for name, got, want in (
        ("left", export_box.left(), reference_box.left()),
        ("top", export_box.top(), reference_box.top()),
        ("right", export_box.right(), reference_box.right()),
        ("bottom", export_box.bottom(), reference_box.bottom()),
    ):
        assert abs(got - want) <= 2, (
            f"t={t}: annotation {name} edge is at {got}, expected {want}"
        )


@pytest.mark.parametrize("t", TIMES)
def test_export_matches_preview_pixel_for_pixel(exported, tmp_path, t):
    """The literal claim, measured: the exported frame and the frame the
    preview renderer produces differ only by encoder rounding."""
    annotated, base_video = exported
    export_frame = frame_at(annotated, t, tmp_path / f"p{t}.png")
    base_frame = frame_at(base_video, t, tmp_path / f"q{t}.png")
    reference = composite_reference(base_frame, visible(t))

    error = mean_abs_error(export_frame, reference)
    assert error < 3.0, f"t={t}: mean channel difference {error:.2f}/255"


@pytest.mark.parametrize("t", TIMES)
def test_delivery_encode_does_not_shift_colour(exported_delivery, tmp_path, t):
    """Guards the limited/full range mismatch, whose signature is geometry
    that is right and a picture that is uniformly washed out."""
    annotated, base_video = exported_delivery
    export_frame = frame_at(annotated, t, tmp_path / f"c{t}.png")
    base_frame = frame_at(base_video, t, tmp_path / f"d{t}.png")
    reference = composite_reference(base_frame, visible(t))

    error = mean_abs_error(export_frame, reference)
    assert error < 6.0, f"t={t}: mean channel difference {error:.2f}/255 versus preview"


def test_shapes_do_not_leak_outside_their_time_range(exported, tmp_path):
    """The failure the hard-cut timing model exists to prevent."""
    for t, expected in [(1.0, {"rect", "ellipse"}), (3.0, {"arrow"}), (5.0, {"text"})]:
        assert {s.type for s in visible(t)} == expected

    annotated, base_video = exported
    export_frame = frame_at(annotated, 3.0, tmp_path / "leak_e.png")
    base_frame = frame_at(base_video, 3.0, tmp_path / "leak_b.png")

    # At t=3 only the arrow is drawn, so the rect's stroke must be absent.
    rect = SHAPES[0]
    y = int(rect.points[0][1] * HEIGHT) + 2
    for x in range(int(rect.points[0][0] * WIDTH) + 4,
                   int(rect.points[1][0] * WIDTH) - 4, 8):
        a, b = export_frame.pixelColor(x, y), base_frame.pixelColor(x, y)
        difference = abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue())
        assert difference < INK_THRESHOLD, f"a shape is visible outside its range at x={x}"
