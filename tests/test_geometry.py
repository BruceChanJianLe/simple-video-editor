from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, QSize

from sve.geometry import (
    even,
    fit_size,
    normalized_to_widget,
    video_display_rect,
    widget_to_normalized,
)


def test_exact_aspect_match_fills_widget():
    rect = video_display_rect(QSize(1280, 720), QSize(1920, 1080))
    assert (rect.x(), rect.y(), rect.width(), rect.height()) == (0.0, 0.0, 1280.0, 720.0)


def test_pillarbox_portrait_video_in_landscape_widget():
    # 1080x1920 video in an 800x600 widget: height-limited, bars left/right.
    rect = video_display_rect(QSize(800, 600), QSize(1080, 1920))
    assert rect.height() == pytest.approx(600.0)
    assert rect.width() == pytest.approx(600.0 * 1080 / 1920)
    assert rect.x() == pytest.approx((800 - rect.width()) / 2)
    assert rect.y() == pytest.approx(0.0)


def test_letterbox_wide_video_in_square_widget():
    rect = video_display_rect(QSize(600, 600), QSize(1920, 1080))
    assert rect.width() == pytest.approx(600.0)
    assert rect.height() == pytest.approx(600.0 * 1080 / 1920)
    assert rect.y() == pytest.approx((600 - rect.height()) / 2)


def test_degenerate_sizes_give_empty_rect():
    assert video_display_rect(QSize(0, 0), QSize(1920, 1080)).isEmpty()
    assert video_display_rect(QSize(800, 600), QSize(0, 1080)).isEmpty()
    assert widget_to_normalized(QPointF(1, 1), QSize(0, 0), QSize(16, 9)) is None


@pytest.mark.parametrize(
    "widget,video",
    [
        (QSize(800, 600), QSize(1920, 1080)),   # letterboxed
        (QSize(800, 600), QSize(1080, 1920)),   # pillarboxed
        (QSize(640, 360), QSize(1280, 720)),    # exact
        (QSize(333, 777), QSize(640, 480)),     # awkward
    ],
)
@pytest.mark.parametrize("norm", [(0.0, 0.0), (1.0, 1.0), (0.5, 0.5), (0.25, 0.8)])
def test_roundtrip_is_identity(widget, video, norm):
    """The bug this guards: preview offset by the letterbox bar thickness."""
    point = QPointF(*norm)
    widget_pt = normalized_to_widget(point, widget, video)
    back = widget_to_normalized(widget_pt, widget, video)
    assert back.x() == pytest.approx(point.x(), abs=1e-9)
    assert back.y() == pytest.approx(point.y(), abs=1e-9)


def test_letterbox_bar_maps_to_frame_edge_when_clamped():
    # Click 10px above a letterboxed video: clamps to the top of the frame.
    widget, video = QSize(600, 600), QSize(1920, 1080)
    rect = video_display_rect(widget, video)
    above = QPointF(300, rect.y() - 10)
    assert widget_to_normalized(above, widget, video).y() == 0.0
    assert widget_to_normalized(above, widget, video, clamp=False).y() < 0.0


def test_widget_centre_is_frame_centre():
    for video in (QSize(1920, 1080), QSize(1080, 1920), QSize(640, 480)):
        got = widget_to_normalized(QPointF(400, 300), QSize(800, 600), video)
        assert got.x() == pytest.approx(0.5)
        assert got.y() == pytest.approx(0.5)


def test_fit_size():
    assert fit_size(QSize(1920, 1080), QSize(640, 640)) == QSize(640, 360)
    assert fit_size(QSize(1080, 1920), QSize(640, 640)) == QSize(360, 640)


def test_even():
    assert even(1081) == 1080
    assert even(1080) == 1080
