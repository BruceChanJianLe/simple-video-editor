"""Tests for the shared renderer.

The property that matters is not "the arrow looks nice" but *resolution
independence*: rendering a shape at preview scale and at export scale must
produce the same picture up to a uniform scale factor. If that holds, preview
and export cannot disagree about position or size, which is the whole reason
there is only one renderer.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, QRectF, QSize
from PySide6.QtGui import QColor, QImage, QPainter

from sve.model import Shape
from sve.render import bounding_rect, draw_shape, hit_test, render_to_image, stroke_pixels


@pytest.fixture(scope="session", autouse=True)
def qt_app(qapp):
    return qapp


def ink_bbox(image: QImage) -> QRectF | None:
    """Bounding box of non-transparent pixels, as 0..1 fractions."""
    w, h = image.width(), image.height()
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            if QColor.fromRgba(image.pixel(x, y)).alpha() > 8:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return QRectF(
        min(xs) / w, min(ys) / h, (max(xs) - min(xs) + 1) / w, (max(ys) - min(ys) + 1) / h
    )


SHAPES = [
    Shape(type="rect", points=[[0.2, 0.25], [0.7, 0.65]], stroke_width=0.008),
    Shape(type="ellipse", points=[[0.15, 0.2], [0.6, 0.8]], stroke_width=0.006),
    Shape(type="arrow", points=[[0.1, 0.9], [0.8, 0.2]], stroke_width=0.01),
    Shape(type="text", points=[[0.1, 0.1]], text="Hello", font_size=0.12),
]


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.type)
def test_render_is_resolution_independent(shape):
    """A shape drawn at 320x180 and at 1280x720 must cover the same *fraction*
    of the frame. This is the guard against the classic 'export is offset and
    scaled wrong versus preview' bug."""
    small = ink_bbox(render_to_image([shape], QSize(320, 180)))
    large = ink_bbox(render_to_image([shape], QSize(1280, 720)))
    assert small is not None and large is not None

    # One low-res pixel is 1/320 of the frame; allow a shade over that.
    tol = 2.0 / 320
    assert small.x() == pytest.approx(large.x(), abs=tol)
    assert small.y() == pytest.approx(large.y(), abs=tol)
    assert small.width() == pytest.approx(large.width(), abs=tol)
    assert small.height() == pytest.approx(large.height(), abs=tol)


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.type)
def test_render_stays_inside_the_frame(shape):
    box = ink_bbox(render_to_image([shape], QSize(640, 360)))
    assert box is not None
    assert box.x() >= 0 and box.y() >= 0
    assert box.right() <= 1.0 and box.bottom() <= 1.0


def test_output_image_is_exactly_target_size_and_transparent():
    image = render_to_image([], QSize(1920, 1080))
    assert image.size() == QSize(1920, 1080)
    assert image.hasAlphaChannel()
    assert QColor.fromRgba(image.pixel(5, 5)).alpha() == 0


def test_rect_lands_on_the_requested_normalized_position():
    shape = Shape(type="rect", points=[[0.25, 0.5], [0.75, 0.9]], stroke_width=0.002)
    box = ink_bbox(render_to_image([shape], QSize(800, 400)))
    assert box.x() == pytest.approx(0.25, abs=0.01)
    assert box.y() == pytest.approx(0.5, abs=0.01)
    assert box.right() == pytest.approx(0.75, abs=0.01)
    assert box.bottom() == pytest.approx(0.9, abs=0.01)


def test_stroke_width_scales_with_height_only():
    shape = Shape(type="rect", points=[[0.1, 0.1], [0.9, 0.9]], stroke_width=0.01)
    assert stroke_pixels(shape, QSize(1920, 1080)) == pytest.approx(10.8)
    # Twice as wide, same height -> same stroke, so an ultrawide export does
    # not get fatter lines.
    assert stroke_pixels(shape, QSize(3840, 1080)) == pytest.approx(10.8)
    assert stroke_pixels(shape, QSize(1280, 720)) == pytest.approx(7.2)


def test_minimum_pen_width_survives_tiny_targets():
    shape = Shape(type="rect", points=[[0.1, 0.1], [0.9, 0.9]], stroke_width=0.001)
    assert stroke_pixels(shape, QSize(32, 18)) >= 1.0
    assert ink_bbox(render_to_image([shape], QSize(32, 18))) is not None


def test_arrow_head_is_at_the_head_end():
    """points[0] is the tail, points[1] the head; the ink must be denser at
    the head end. Getting this backwards is invisible in a bounding box."""
    shape = Shape(type="arrow", points=[[0.05, 0.5], [0.95, 0.5]], stroke_width=0.02)
    image = render_to_image([shape], QSize(400, 200))

    def column_ink(x: int) -> int:
        return sum(
            1 for y in range(image.height())
            if QColor.fromRgba(image.pixel(x, y)).alpha() > 8
        )

    assert column_ink(360) > column_ink(40) * 2


def test_transparent_stroke_draws_nothing():
    shape = Shape(type="rect", points=[[0.2, 0.2], [0.8, 0.8]], stroke_color="#00000000")
    assert ink_bbox(render_to_image([shape], QSize(200, 200))) is None


def test_fill_is_drawn_when_opaque():
    shape = Shape(
        type="rect", points=[[0.2, 0.2], [0.8, 0.8]],
        stroke_color="#00000000", fill_color="#0000ffff",
    )
    image = render_to_image([shape], QSize(200, 200))
    assert QColor.fromRgba(image.pixel(100, 100)).alpha() > 200


def test_shapes_composite_in_order():
    lower = Shape(type="rect", points=[[0.1, 0.1], [0.9, 0.9]],
                  stroke_color="#00000000", fill_color="#ff0000ff")
    upper = Shape(type="rect", points=[[0.3, 0.3], [0.7, 0.7]],
                  stroke_color="#00000000", fill_color="#00ff00ff")
    image = render_to_image([lower, upper], QSize(200, 200))
    assert QColor.fromRgba(image.pixel(100, 100)).green() > 200


def test_text_renders_with_the_bundled_font():
    shape = Shape(type="text", points=[[0.1, 0.1]], text="Ag", font_size=0.3)
    assert ink_bbox(render_to_image([shape], QSize(400, 200))) is not None


def test_empty_text_renders_nothing():
    shape = Shape(type="text", points=[[0.1, 0.1]], text="", font_size=0.3)
    assert ink_bbox(render_to_image([shape], QSize(400, 200))) is None


def test_degenerate_arrow_does_not_crash():
    shape = Shape(type="arrow", points=[[0.5, 0.5], [0.5, 0.5]])
    render_to_image([shape], QSize(100, 100))


def test_draw_shape_honours_painter_translation():
    """The overlay draws into the letterboxed video rect by translating the
    painter; the renderer must respect that rather than assuming widget 0,0."""
    shape = Shape(type="rect", points=[[0.0, 0.0], [1.0, 1.0]],
                  stroke_color="#00000000", fill_color="#ff0000ff")
    image = QImage(QSize(200, 200), QImage.Format.Format_RGBA8888)
    image.fill(0)
    painter = QPainter(image)
    painter.translate(50, 50)
    draw_shape(painter, shape, QSize(100, 100))
    painter.end()

    assert QColor.fromRgba(image.pixel(75, 75)).alpha() > 200   # inside
    assert QColor.fromRgba(image.pixel(25, 25)).alpha() == 0    # outside


class TestHitTest:
    def test_unfilled_rect_is_grabbed_by_its_outline_not_its_interior(self):
        shape = Shape(type="rect", points=[[0.2, 0.2], [0.8, 0.8]], stroke_width=0.01)
        size = QSize(1000, 1000)
        assert hit_test(shape, QPointF(200, 500), size)      # on the left edge
        assert not hit_test(shape, QPointF(500, 500), size)  # in the hollow middle

    def test_filled_rect_is_grabbed_anywhere(self):
        shape = Shape(type="rect", points=[[0.2, 0.2], [0.8, 0.8]], fill_color="#0000ff80")
        assert hit_test(shape, QPointF(500, 500), QSize(1000, 1000))

    def test_arrow_is_grabbed_along_its_line_not_its_bounding_box(self):
        shape = Shape(type="arrow", points=[[0.1, 0.1], [0.9, 0.9]], stroke_width=0.008)
        size = QSize(1000, 1000)
        assert hit_test(shape, QPointF(500, 500), size)       # on the diagonal
        assert not hit_test(shape, QPointF(850, 150), size)   # corner of the bbox

    def test_ellipse_corner_is_not_a_hit(self):
        shape = Shape(type="ellipse", points=[[0.1, 0.1], [0.9, 0.9]], stroke_width=0.006)
        size = QSize(1000, 1000)
        assert hit_test(shape, QPointF(500, 105), size)       # top of the arc
        assert not hit_test(shape, QPointF(140, 140), size)   # bbox corner

    def test_bounding_rect_includes_stroke(self):
        shape = Shape(type="rect", points=[[0.25, 0.25], [0.75, 0.75]], stroke_width=0.02)
        rect = bounding_rect(shape, QSize(1000, 1000))
        assert rect.x() < 250 and rect.right() > 750


class TestColourParsing:
    """QColor reads 8-digit hex as #AARRGGBB; the document uses web-order
    #RRGGBBAA. Delegating to QColor silently swaps channels and alpha."""

    def test_rrggbbaa_order(self):
        from sve.render import color
        c = color("#ff0000ff")
        assert (c.red(), c.green(), c.blue(), c.alpha()) == (255, 0, 0, 255)

    def test_alpha_is_the_last_pair(self):
        from sve.render import color
        assert color("#0000ff00").alpha() == 0
        assert color("#0000ff80").alpha() == 0x80
        assert color("#0000ffff").blue() == 255

    def test_six_digit_and_named_still_work(self):
        from sve.render import color
        assert color("#ff0000").alpha() == 255
        assert color("red").red() == 255

    def test_roundtrip(self):
        from sve.render import color, color_to_spec
        for spec in ("#ff3b30ff", "#0000ff80", "#12345678"):
            assert color_to_spec(color(spec)) == spec

    def test_garbage_is_transparent_not_fatal(self):
        from sve.render import color
        assert color("not a colour").alpha() == 0
        assert color("#zzzzzzzz").alpha() == 0
