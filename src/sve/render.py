"""The shared shape renderer.

This module is the single source of truth for what an annotation looks like.
The on-screen overlay calls :func:`draw_shape` against the preview widget's
painter at widget scale; the exporter calls it against a transparent
``QImage`` at full video resolution. There is deliberately no second
implementation and no ffmpeg ``drawbox``/``drawtext`` reconstruction of these
shapes, because two renderers that must agree will eventually stop agreeing,
usually in a way nobody notices until an export ships.

The contract that makes one renderer sufficient:

    draw_shape(painter, shape, target_size)

is a pure function of the shape and the target size. Every quantity in
:class:`~sve.model.Shape` is normalized - positions as 0..1 fractions of frame
width/height, stroke width and font size as fractions of frame *height* - so
rendering at 640x360 and at 1920x1080 differ only by a uniform scale factor.
Nothing here may read a widget, a player position, or global state.

Selection handles are *not* drawn here. They belong to the editing UI and must
never reach an export, so they live in :mod:`sve.ui.overlay`.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, QSizeF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)

from .model import Shape

# Arrow head proportions, expressed in multiples of the stroke width so that a
# thicker arrow gets a proportionally larger head.
ARROW_HEAD_LENGTH = 5.0
ARROW_HEAD_HALF_WIDTH = 2.6
# Never let the head eat more than this fraction of a short arrow.
ARROW_HEAD_MAX_FRACTION = 0.45

# Text is drawn with a *bundled* face rather than the system default. Relying
# on whatever fontconfig serves up would make the same project render
# differently on a developer's machine and in a packaged build - and, worse,
# differently between two exports of the same project on two machines.
# :func:`ensure_fonts` registers the shipped DejaVu files; the family list is
# the fallback chain if that registration ever fails.
BUNDLED_FONT_FILES = ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
FONT_FAMILIES = ["DejaVu Sans", "Noto Sans", "Helvetica", "Arial", "sans-serif"]

MIN_PEN_WIDTH = 1.0

_fonts_loaded = False


def font_dir() -> Path | None:
    """Directory holding the bundled font files, if we can find one."""
    env = os.environ.get("SVE_FONT_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    meipass = getattr(sys, "_MEIPASS", None)
    roots = [Path(meipass) / "fonts"] if meipass else []
    roots.append(Path(__file__).resolve().parent / "resources" / "fonts")
    return next((r for r in roots if r.is_dir()), None)


def ensure_fonts() -> None:
    """Register the bundled fonts with Qt. Idempotent; needs a QGuiApplication.

    Safe to call from anywhere in the drawing path - it does nothing after the
    first successful registration.
    """
    global _fonts_loaded
    if _fonts_loaded:
        return
    directory = font_dir()
    if directory is None:
        _fonts_loaded = True  # nothing to load; fall back to FONT_FAMILIES
        return
    for name in BUNDLED_FONT_FILES:
        path = directory / name
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    _fonts_loaded = True


def color(spec: str) -> QColor:
    """Parse a colour string in the document's ``#RRGGBBAA`` convention.

    Parsed by hand rather than handed to ``QColor``, which reads an 8-digit
    hex string as ``#AARRGGBB``. Feeding it a web-style ``#RRGGBBAA`` silently
    yields a different colour with a different alpha - ``#ff0000ff`` (opaque
    red) comes back as opaque *blue*, and ``#0000ffff`` comes back fully
    transparent. The document keeps the web/CSS order because that is what
    anyone hand-editing the project JSON will expect.

    Anything unparseable becomes fully transparent, so a corrupt colour makes
    a shape invisible rather than aborting a render.
    """
    if not isinstance(spec, str):
        return QColor(0, 0, 0, 0)
    text = spec.strip()

    if text.startswith("#") and len(text) == 9:
        try:
            r, g, b, a = (int(text[i:i + 2], 16) for i in (1, 3, 5, 7))
        except ValueError:
            return QColor(0, 0, 0, 0)
        return QColor(r, g, b, a)

    if text.startswith("#") and len(text) == 5:  # #RGBA shorthand
        try:
            r, g, b, a = (int(ch * 2, 16) for ch in text[1:])
        except ValueError:
            return QColor(0, 0, 0, 0)
        return QColor(r, g, b, a)

    parsed = QColor(text)  # #RGB, #RRGGBB and named colours are unambiguous
    return parsed if parsed.isValid() else QColor(0, 0, 0, 0)


def color_to_spec(value: QColor) -> str:
    """Inverse of :func:`color`: a QColor as ``#RRGGBBAA``."""
    return f"#{value.red():02x}{value.green():02x}{value.blue():02x}{value.alpha():02x}"


def is_transparent(spec: str) -> bool:
    return color(spec).alpha() == 0


def to_pixels(point: list[float] | tuple[float, float], size: QSizeF | QSize) -> QPointF:
    """Normalized 0..1 -> pixel position within a frame of ``size``."""
    return QPointF(point[0] * size.width(), point[1] * size.height())


def stroke_pixels(shape: Shape, size: QSizeF | QSize) -> float:
    """Stroke width in pixels for a given target size.

    Normalized against *height* only. Using height for both axes (rather than,
    say, the diagonal) keeps strokes visually constant when the same project is
    exported at 720p and 1080p, and avoids a width-dependent term that would
    make strokes thicken on ultrawide output.
    """
    return max(MIN_PEN_WIDTH, shape.stroke_width * size.height())


def font_pixels(shape: Shape, size: QSizeF | QSize) -> float:
    return max(1.0, shape.font_size * size.height())


def shape_font(shape: Shape, size: QSizeF | QSize) -> QFont:
    ensure_fonts()
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    font.setPixelSize(max(1, round(font_pixels(shape, size))))
    return font


def _rect_of(shape: Shape, size: QSizeF) -> QRectF:
    a = to_pixels(shape.points[0], size)
    b = to_pixels(shape.points[1], size)
    return QRectF(a, b).normalized()


def _configure(painter: QPainter, shape: Shape, size: QSizeF) -> QPen:
    pen = QPen(color(shape.stroke_color))
    pen.setWidthF(stroke_pixels(shape, size))
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    if is_transparent(shape.stroke_color):
        pen.setStyle(Qt.PenStyle.NoPen)
    painter.setPen(pen)
    painter.setBrush(QBrush(color(shape.fill_color)))
    return pen


def _draw_arrow(painter: QPainter, shape: Shape, size: QSizeF) -> None:
    tail = to_pixels(shape.points[0], size)
    head = to_pixels(shape.points[1], size)
    width = stroke_pixels(shape, size)

    dx, dy = head.x() - tail.x(), head.y() - tail.y()
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return
    ux, uy = dx / length, dy / length

    head_len = min(ARROW_HEAD_LENGTH * width, length * ARROW_HEAD_MAX_FRACTION)
    half = ARROW_HEAD_HALF_WIDTH * width * (head_len / (ARROW_HEAD_LENGTH * width))

    base = QPointF(head.x() - ux * head_len, head.y() - uy * head_len)
    stroke = color(shape.stroke_color)

    # Shaft stops at the head's base so the join never shows through a
    # semi-transparent stroke colour as a darker overlap.
    pen = QPen(stroke)
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(tail, base)

    nx, ny = -uy, ux
    head_poly = QPolygonF([
        head,
        QPointF(base.x() + nx * half, base.y() + ny * half),
        QPointF(base.x() - nx * half, base.y() - ny * half),
    ])
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(stroke))
    painter.drawPolygon(head_poly)


def _draw_rect(painter: QPainter, shape: Shape, size: QSizeF) -> None:
    _configure(painter, shape, size)
    painter.drawRect(_rect_of(shape, size))


def _draw_ellipse(painter: QPainter, shape: Shape, size: QSizeF) -> None:
    _configure(painter, shape, size)
    painter.drawEllipse(_rect_of(shape, size))


def _draw_text(painter: QPainter, shape: Shape, size: QSizeF) -> None:
    if not shape.text:
        return
    font = shape_font(shape, size)
    painter.setFont(font)
    metrics = QFontMetricsF(font)
    origin = to_pixels(shape.points[0], size)

    lines = shape.text.split("\n")
    line_height = metrics.height()

    # A filled background box, when the fill colour is not transparent, so
    # labels stay readable over busy footage.
    if not is_transparent(shape.fill_color):
        widest = max(metrics.horizontalAdvance(line) for line in lines)
        pad = line_height * 0.18
        box = QRectF(
            origin.x() - pad,
            origin.y() - pad,
            widest + 2 * pad,
            line_height * len(lines) + 2 * pad,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(color(shape.fill_color)))
        painter.drawRect(box)

    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(color(shape.stroke_color)))
    for index, line in enumerate(lines):
        baseline = origin.y() + metrics.ascent() + index * line_height
        painter.drawText(QPointF(origin.x(), baseline), line)


_DRAWERS = {
    "arrow": _draw_arrow,
    "rect": _draw_rect,
    "ellipse": _draw_ellipse,
    "text": _draw_text,
}


def draw_shape(painter: QPainter, shape: Shape, target: QSize | QSizeF) -> None:
    """Draw one shape onto ``painter``, sized for a frame of ``target``.

    ``painter`` must already be translated so that (0, 0) is the top-left of
    the video frame; the overlay does that with the letterboxed video rect, the
    exporter does not need to because its image *is* the frame.
    """
    size = QSizeF(target)
    drawer = _DRAWERS.get(shape.type)
    if drawer is None:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    try:
        drawer(painter, shape, size)
    finally:
        painter.restore()


def render_to_image(shapes: list[Shape], target: QSize) -> QImage:
    """Rasterize ``shapes`` onto a transparent image of exactly ``target``.

    This is what the exporter feeds to ffmpeg's ``overlay`` filter. Because the
    image is full frame size, the overlay offset is always ``0:0`` and all
    positioning correctness lives in :func:`draw_shape` - the same code the
    preview uses.
    """
    image = QImage(target, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        for shape in shapes:
            draw_shape(painter, shape, target)
    finally:
        painter.end()
    return image


def bounding_rect(shape: Shape, target: QSize | QSizeF) -> QRectF:
    """Pixel bounding box of ``shape``, used for hit-testing and handles.

    Includes stroke width so that clicking the visible edge of a thick shape
    selects it.
    """
    size = QSizeF(target)
    if shape.type == "text":
        origin = to_pixels(shape.points[0], size)
        metrics = QFontMetricsF(shape_font(shape, size))
        lines = shape.text.split("\n") or [""]
        widest = max((metrics.horizontalAdvance(line) for line in lines), default=0.0)
        return QRectF(origin.x(), origin.y(), max(widest, 1.0),
                      metrics.height() * max(len(lines), 1))

    a = to_pixels(shape.points[0], size)
    b = to_pixels(shape.points[1], size)
    rect = QRectF(a, b).normalized()
    pad = stroke_pixels(shape, size) / 2.0
    return rect.adjusted(-pad, -pad, pad, pad)


def hit_test(shape: Shape, point: QPointF, target: QSize | QSizeF) -> bool:
    """True when ``point`` (in frame pixels) selects ``shape``.

    Unfilled shapes are selected by their outline rather than their interior,
    so that a large empty box does not swallow clicks meant for what is behind
    it. Arrows use a stroked path so the diagonal is grabbable along its
    length rather than anywhere in its bounding box.
    """
    size = QSizeF(target)
    slack = max(4.0, stroke_pixels(shape, size))

    if shape.type == "text":
        return bounding_rect(shape, size).adjusted(-slack, -slack, slack, slack).contains(point)

    if shape.type == "arrow":
        tail = to_pixels(shape.points[0], size)
        head = to_pixels(shape.points[1], size)
        path = QPainterPath(tail)
        path.lineTo(head)
        stroker_pen = QPen()
        stroker_pen.setWidthF(slack * 2)
        from PySide6.QtGui import QPainterPathStroker

        stroker = QPainterPathStroker(stroker_pen)
        return stroker.createStroke(path).contains(point)

    rect = QRectF(to_pixels(shape.points[0], size), to_pixels(shape.points[1], size)).normalized()
    if not is_transparent(shape.fill_color):
        return rect.adjusted(-slack, -slack, slack, slack).contains(point)

    outer = rect.adjusted(-slack, -slack, slack, slack)
    inner = rect.adjusted(slack, slack, -slack, -slack)
    if shape.type == "ellipse":
        outer_path = QPainterPath()
        outer_path.addEllipse(outer)
        inner_path = QPainterPath()
        if inner.isValid():
            inner_path.addEllipse(inner)
        return outer_path.contains(point) and not inner_path.contains(point)
    return outer.contains(point) and not (inner.isValid() and inner.contains(point))


__all__ = [
    "bounding_rect",
    "color",
    "color_to_spec",
    "draw_shape",
    "ensure_fonts",
    "font_pixels",
    "hit_test",
    "is_transparent",
    "render_to_image",
    "shape_font",
    "stroke_pixels",
    "to_pixels",
]
