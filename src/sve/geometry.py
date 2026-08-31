"""Mapping between widget pixels and normalized frame coordinates.

``QVideoWidget`` letterboxes: with the default ``KeepAspectRatio`` mode the
video is scaled to fit and centred, so the displayed video rect is *not* the
widget rect unless the aspect ratios happen to match exactly. Converting mouse
positions against the widget rect instead of the video rect produces a small
constant offset that is easy to miss on screen and obvious in an export.

Everything here is pure arithmetic on sizes, so the awkward cases - a portrait
video in a landscape widget, a widget briefly sized 0 during layout - are
testable without constructing a window.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, QSizeF


def video_display_rect(widget: QSize | QSizeF, video: QSize | QSizeF) -> QRectF:
    """The rect inside ``widget`` actually covered by the video image.

    Matches ``Qt::KeepAspectRatio``: scale to fit, then centre. Returns an
    empty rect when either size is degenerate, which callers must treat as
    "no mapping available yet" rather than dividing by it.
    """
    ww, wh = float(widget.width()), float(widget.height())
    vw, vh = float(video.width()), float(video.height())
    if ww <= 0 or wh <= 0 or vw <= 0 or vh <= 0:
        return QRectF()

    scale = min(ww / vw, wh / vh)
    dw, dh = vw * scale, vh * scale
    return QRectF((ww - dw) / 2.0, (wh - dh) / 2.0, dw, dh)


def widget_to_normalized(
    point: QPointF, widget: QSize | QSizeF, video: QSize | QSizeF, *, clamp: bool = True
) -> QPointF | None:
    """Widget pixel -> 0..1 fraction of the video frame.

    Returns ``None`` when there is no video rect to map against. With
    ``clamp`` the result is confined to the frame, which is what shape editing
    wants: dragging into the letterbox bars should pin the shape to the frame
    edge rather than place it outside the visible picture.
    """
    rect = video_display_rect(widget, video)
    if rect.isEmpty():
        return None
    nx = (point.x() - rect.x()) / rect.width()
    ny = (point.y() - rect.y()) / rect.height()
    if clamp:
        nx = min(1.0, max(0.0, nx))
        ny = min(1.0, max(0.0, ny))
    return QPointF(nx, ny)


def normalized_to_widget(
    point: QPointF, widget: QSize | QSizeF, video: QSize | QSizeF
) -> QPointF | None:
    rect = video_display_rect(widget, video)
    if rect.isEmpty():
        return None
    return QPointF(rect.x() + point.x() * rect.width(), rect.y() + point.y() * rect.height())


def contains_video(point: QPointF, widget: QSize | QSizeF, video: QSize | QSizeF) -> bool:
    rect = video_display_rect(widget, video)
    return bool(not rect.isEmpty() and rect.contains(point))


def fit_size(source: QSize, bound: QSize) -> QSize:
    """Largest size with ``source``'s aspect ratio fitting inside ``bound``."""
    rect = video_display_rect(bound, source)
    if rect.isEmpty():
        return QSize(0, 0)
    return QSize(max(1, round(rect.width())), max(1, round(rect.height())))


def even(value: int) -> int:
    """Round down to an even number.

    yuv420p subsamples chroma 2x2, so H.264 will refuse or silently pad odd
    dimensions. Applied wherever a user-supplied output size reaches ffmpeg.
    """
    return value - (value % 2)


__all__ = [
    "contains_video",
    "even",
    "fit_size",
    "normalized_to_widget",
    "video_display_rect",
    "widget_to_normalized",
]
