"""Preview geometry: the overlay must sit exactly on the video picture.

The bug these guard against is the one the whole coordinate design exists to
prevent, and it is not hypothetical - it shipped into this codebase once and
was caught only by drawing a (0,0)-(1,1) shape over real video and looking at
where it landed.

``QGraphicsVideoItem``, when sized to the whole viewport, centres the picture
inside that size while reporting a ``boundingRect`` that is already the fitted
size. Code that recomputes the picture rect from that bounding rect therefore
drops the centring offset and draws every annotation one letterbox bar out of
place. On a 782x650 preview of a 16:9 clip that was 105px - clearly visible,
but not obviously *wrong* unless you have a reference to compare against.

The canvas now computes the picture rect itself and positions every layer into
it, so these tests can check the arithmetic without a decoder.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, QSize, QSizeF
from PySide6.QtGui import QColor, QImage, QPainter

from sve.geometry import video_display_rect
from sve.model import Clip, OutputSpec, Project, Shape, SourceInfo
from sve.ui.preview import VideoCanvas
from sve.ui.state import EditorState
from sve.undo import Document


def make_canvas(qapp, video_size: QSize, widget_size: QSize, shapes=None) -> VideoCanvas:
    info = SourceInfo(duration=6.0, width=video_size.width(), height=video_size.height(),
                      fps=30.0, has_audio=True)
    clip = Clip(kind="video", source_path="/nonexistent.mp4", in_point=0.0,
                out_point=6.0, info=info, shapes=list(shapes or []))
    project = Project(
        output=OutputSpec(width=video_size.width(), height=video_size.height(), fps=30.0),
        clips=[clip],
    )
    state = EditorState(Document(project))
    canvas = VideoCanvas(state)
    canvas.resize(widget_size)
    canvas.show()
    state.set_current_clip(clip.id)
    state.set_playhead(1.0)
    qapp.processEvents()
    canvas._relayout()
    return canvas


GEOMETRIES = [
    (QSize(1280, 720), QSize(782, 650)),    # letterboxed: the case that broke
    (QSize(1080, 1920), QSize(782, 650)),   # pillarboxed portrait
    (QSize(1280, 720), QSize(640, 360)),    # exact aspect match
    (QSize(640, 480), QSize(500, 900)),     # 4:3 in a tall widget
]


@pytest.mark.parametrize("video,widget", GEOMETRIES)
def test_picture_rect_matches_the_aspect_fit(qapp, video, widget):
    canvas = make_canvas(qapp, video, widget)
    expected = video_display_rect(QSizeF(canvas.viewport().size()), QSizeF(video))
    assert canvas.picture_rect() == expected


@pytest.mark.parametrize("video,widget", GEOMETRIES)
def test_every_layer_is_positioned_on_the_picture(qapp, video, widget):
    """The video item, the still item and the overlay must share one rect."""
    canvas = make_canvas(qapp, video, widget)
    rect = canvas.picture_rect()

    assert canvas.annotations.pos() == rect.topLeft()
    assert canvas.annotations.boundingRect().size() == rect.size()
    assert canvas.video_item.pos() == rect.topLeft()
    assert canvas.video_item.size() == rect.size()
    assert canvas.still_item.pos() == rect.topLeft()


@pytest.mark.parametrize("video,widget", GEOMETRIES)
def test_full_frame_shape_renders_exactly_onto_the_picture(qapp, video, widget):
    """Draw a shape covering the whole frame; its ink must coincide with the
    picture rect, not with the widget and not offset by a letterbox bar."""
    shape = Shape(type="rect", points=[[0.0, 0.0], [1.0, 1.0]], start=0.0, end=6.0,
                  stroke_color="#00000000", fill_color="#ff00ffff")
    canvas = make_canvas(qapp, video, widget, [shape])

    image = QImage(canvas.viewport().size(), QImage.Format.Format_RGB32)
    image.fill(QColor("black"))
    painter = QPainter(image)
    canvas.render(painter)
    painter.end()

    xs, ys = [], []
    for y in range(image.height()):
        for x in range(image.width()):
            colour = image.pixelColor(x, y)
            if colour.red() > 120 and colour.blue() > 120 and colour.green() < 90:
                xs.append(x)
                ys.append(y)
    assert xs, "the full-frame shape did not render at all"

    rect = canvas.picture_rect()
    assert min(xs) == pytest.approx(rect.left(), abs=2)
    assert min(ys) == pytest.approx(rect.top(), abs=2)
    assert max(xs) == pytest.approx(rect.right(), abs=2)
    assert max(ys) == pytest.approx(rect.bottom(), abs=2)


@pytest.mark.parametrize("video,widget", GEOMETRIES)
@pytest.mark.parametrize("norm", [(0.0, 0.0), (1.0, 1.0), (0.5, 0.5), (0.2, 0.85)])
def test_scene_coordinate_roundtrip(qapp, video, widget, norm):
    """Mouse position to normalized and back must be the identity, including
    for a click on the very edge of the picture."""
    canvas = make_canvas(qapp, video, widget)
    item = canvas.annotations
    rect = canvas.picture_rect()

    scene_point = QPointF(
        rect.left() + norm[0] * rect.width(),
        rect.top() + norm[1] * rect.height(),
    )
    got = item.to_normalized(scene_point)
    assert got.x() == pytest.approx(norm[0], abs=1e-6)
    assert got.y() == pytest.approx(norm[1], abs=1e-6)


def test_clicking_a_letterbox_bar_clamps_into_the_frame(qapp):
    canvas = make_canvas(qapp, QSize(1280, 720), QSize(782, 650))
    rect = canvas.picture_rect()
    assert rect.top() > 0, "expected this geometry to letterbox"

    above = QPointF(rect.center().x(), rect.top() - 20)
    clamped = canvas.annotations.to_normalized(above)
    assert clamped.y() == 0.0
    assert canvas.annotations.to_normalized(above, clamp=False).y() < 0.0


def test_resizing_keeps_the_overlay_on_the_picture(qapp):
    canvas = make_canvas(qapp, QSize(1280, 720), QSize(782, 650))
    for size in (QSize(400, 900), QSize(1200, 300), QSize(700, 700)):
        canvas.resize(size)
        qapp.processEvents()
        canvas._relayout()
        rect = canvas.picture_rect()
        assert canvas.annotations.pos() == rect.topLeft()
        assert canvas.annotations.boundingRect().size() == rect.size()


def test_conversions_report_no_mapping_before_layout(qapp):
    """Between construction and the first layout there is no picture rect.

    Callers must get ``None`` rather than a division by zero - this is the
    state the overlay is in during the first paint of a freshly opened window.
    """
    canvas = make_canvas(qapp, QSize(1280, 720), QSize(782, 650))
    canvas.annotations.set_picture_size(QSizeF())
    assert canvas.annotations.boundingRect().isEmpty()
    assert canvas.annotations.to_normalized(QPointF(0, 0)) is None
    assert canvas.annotations.to_frame_pixels(QPointF(0, 0)) is None


def test_canvas_has_a_minimum_size(qapp):
    """The viewport never collapses to zero, so the picture rect stays valid
    however hard the splitter is dragged."""
    canvas = make_canvas(qapp, QSize(1280, 720), QSize(782, 650))
    canvas.resize(QSize(1, 1))
    qapp.processEvents()
    canvas._relayout()
    assert not canvas.picture_rect().isEmpty()
