"""Milestone 6: creating, selecting, moving, resizing and deleting shapes.

Driven through the overlay item's mouse handlers with synthesised events,
which is the same path a real click takes, minus the window manager.

The recurring hazard in this layer is the undo stack: a drag emits dozens of
mouse-move events and must still collapse into one undoable action, and the
properties panel must not write its own displayed value back into the model
while it is syncing itself from that model.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPointF, QSize, Qt, QThread
from PySide6.QtWidgets import QGraphicsSceneMouseEvent

from sve.model import Clip, OutputSpec, Project, Shape, SourceInfo
from sve.ui.preview import VideoCanvas
from sve.ui.state import EditorState
from sve.undo import Document

VIDEO = QSize(1280, 720)
WIDGET = QSize(782, 650)


@pytest.fixture
def canvas(qapp):
    info = SourceInfo(duration=6.0, width=VIDEO.width(), height=VIDEO.height(),
                      fps=30.0, has_audio=True)
    clip = Clip(kind="video", source_path="/nonexistent.mp4", in_point=0.0,
                out_point=6.0, info=info)
    project = Project(output=OutputSpec(width=1280, height=720, fps=30.0), clips=[clip])
    state = EditorState(Document(project))
    view = VideoCanvas(state)
    view.resize(WIDGET)
    view.show()
    state.set_current_clip(clip.id)
    state.set_playhead(1.0)
    qapp.processEvents()
    view._relayout()
    return view


def scene_point(canvas: VideoCanvas, nx: float, ny: float) -> QPointF:
    rect = canvas.picture_rect()
    return QPointF(rect.left() + nx * rect.width(), rect.top() + ny * rect.height())


def _event(kind, scene_pos: QPointF, button=Qt.MouseButton.LeftButton):
    event = QGraphicsSceneMouseEvent(kind)
    event.setScenePos(scene_pos)
    event.setButton(button)
    event.setButtons(button)
    return event


def drag(canvas: VideoCanvas, start: QPointF, end: QPointF, steps: int = 12) -> None:
    item = canvas.annotations
    item.mousePressEvent(_event(QEvent.Type.GraphicsSceneMousePress, start))
    for index in range(1, steps + 1):
        fraction = index / steps
        point = QPointF(
            start.x() + (end.x() - start.x()) * fraction,
            start.y() + (end.y() - start.y()) * fraction,
        )
        item.mouseMoveEvent(_event(QEvent.Type.GraphicsSceneMouseMove, point))
    item.mouseReleaseEvent(_event(QEvent.Type.GraphicsSceneMouseRelease, end))


def only_shape(canvas: VideoCanvas) -> Shape:
    shapes = canvas.state.current_clip.shapes
    assert len(shapes) == 1
    return shapes[0]


class TestCreation:
    @pytest.mark.parametrize("tool", ["rect", "ellipse", "arrow"])
    def test_drag_creates_a_shape_at_the_dragged_position(self, canvas, tool):
        canvas.state.set_tool(tool)
        drag(canvas, scene_point(canvas, 0.2, 0.3), scene_point(canvas, 0.7, 0.8))

        shape = only_shape(canvas)
        assert shape.type == tool
        assert shape.points[0][0] == pytest.approx(0.2, abs=0.01)
        assert shape.points[0][1] == pytest.approx(0.3, abs=0.01)
        assert shape.points[1][0] == pytest.approx(0.7, abs=0.01)
        assert shape.points[1][1] == pytest.approx(0.8, abs=0.01)

    def test_new_shape_starts_at_the_playhead_and_runs_to_the_clip_end(self, canvas):
        canvas.state.set_playhead(2.5)
        canvas.state.set_tool("rect")
        drag(canvas, scene_point(canvas, 0.1, 0.1), scene_point(canvas, 0.5, 0.5))

        shape = only_shape(canvas)
        assert shape.start == pytest.approx(2.5)
        assert shape.end == pytest.approx(canvas.state.current_clip.out_point)

    def test_click_without_drag_creates_nothing(self, canvas):
        """A stray click must not leave an invisible zero-size shape behind."""
        canvas.state.set_tool("rect")
        point = scene_point(canvas, 0.4, 0.4)
        drag(canvas, point, point, steps=1)
        assert canvas.state.current_clip.shapes == []

    def test_text_is_placed_by_a_single_click(self, canvas):
        canvas.state.set_tool("text")
        point = scene_point(canvas, 0.3, 0.6)
        canvas.annotations.mousePressEvent(
            _event(QEvent.Type.GraphicsSceneMousePress, point)
        )
        shape = only_shape(canvas)
        assert shape.type == "text"
        assert shape.text
        assert shape.points[0][0] == pytest.approx(0.3, abs=0.01)

    def test_creating_selects_the_shape_and_returns_to_select(self, canvas):
        canvas.state.set_tool("rect")
        drag(canvas, scene_point(canvas, 0.1, 0.1), scene_point(canvas, 0.5, 0.5))
        assert canvas.state.current_shape is only_shape(canvas)
        assert canvas.state.tool == "select"

    def test_dragging_outside_the_frame_clamps_to_it(self, canvas):
        canvas.state.set_tool("rect")
        rect = canvas.picture_rect()
        drag(canvas,
             scene_point(canvas, 0.5, 0.5),
             QPointF(rect.right() + 300, rect.bottom() + 300))
        shape = only_shape(canvas)
        assert shape.points[1][0] <= 1.0 and shape.points[1][1] <= 1.0

    def test_creation_is_one_undo_step(self, canvas):
        canvas.state.set_tool("rect")
        drag(canvas, scene_point(canvas, 0.2, 0.2), scene_point(canvas, 0.6, 0.6))
        assert len(canvas.state.current_clip.shapes) == 1
        canvas.state.document.undo()
        assert canvas.state.document.project.clips[0].shapes == []


class TestSelection:
    def _add_rect(self, canvas, a=(0.2, 0.2), b=(0.6, 0.6)):
        canvas.state.set_tool("rect")
        drag(canvas, scene_point(canvas, *a), scene_point(canvas, *b))
        canvas.state.set_tool("select")
        return only_shape(canvas)

    def test_clicking_an_edge_selects_it(self, canvas):
        shape = self._add_rect(canvas)
        canvas.state.select_shape(None)
        canvas.annotations.mousePressEvent(
            _event(QEvent.Type.GraphicsSceneMousePress, scene_point(canvas, 0.2, 0.4))
        )
        assert canvas.state.current_shape is shape

    def test_clicking_empty_space_deselects(self, canvas):
        self._add_rect(canvas)
        canvas.annotations.mousePressEvent(
            _event(QEvent.Type.GraphicsSceneMousePress, scene_point(canvas, 0.9, 0.9))
        )
        assert canvas.state.current_shape is None

    def test_shapes_outside_the_playhead_are_not_clickable(self, canvas):
        """They are invisible on screen, so clicking through them must reach
        whatever is behind. They stay reachable from the shape list."""
        shape = self._add_rect(canvas)
        shape.start, shape.end = 4.0, 5.0
        canvas.state.set_playhead(1.0)
        canvas.state.select_shape(None)
        canvas.annotations.mousePressEvent(
            _event(QEvent.Type.GraphicsSceneMousePress, scene_point(canvas, 0.2, 0.4))
        )
        assert canvas.state.current_shape is None


class TestMoveAndResize:
    def _add_selected_rect(self, canvas):
        canvas.state.set_tool("rect")
        drag(canvas, scene_point(canvas, 0.2, 0.2), scene_point(canvas, 0.6, 0.6))
        canvas.state.set_tool("select")
        return only_shape(canvas)

    # Grab the outline at a point that is not a handle. (0.2, 0.4) is the
    # left-edge handle of a 0.2..0.6 square, and (0.4, 0.4) is its hollow
    # centre, which an unfilled shape deliberately does not claim.
    GRAB = (0.2, 0.3)

    def test_dragging_the_body_moves_the_shape(self, canvas):
        shape = self._add_selected_rect(canvas)
        drag(canvas, scene_point(canvas, *self.GRAB), scene_point(canvas, 0.3, 0.4))

        assert shape.points[0][0] == pytest.approx(0.3, abs=0.02)
        assert shape.points[0][1] == pytest.approx(0.3, abs=0.02)
        assert shape.points[1][0] == pytest.approx(0.7, abs=0.02)

    def test_move_is_a_single_undo_step(self, canvas):
        """The drag emits twelve move events; undoing once must restore the
        original position, not step back through them."""
        self._add_selected_rect(canvas)
        drag(canvas, scene_point(canvas, *self.GRAB), scene_point(canvas, 0.4, 0.5))
        assert canvas.state.current_clip.shapes[0].points[0][0] == pytest.approx(0.4, abs=0.02)

        canvas.state.document.undo()
        shape = canvas.state.document.project.clips[0].shapes[0]
        assert shape.points[0][0] == pytest.approx(0.2, abs=0.02)

    def test_moving_clamps_the_whole_shape_to_the_frame(self, canvas):
        """Pushing a shape off the edge must pin it, not let one corner
        escape while the rest follows."""
        shape = self._add_selected_rect(canvas)
        rect = canvas.picture_rect()
        drag(canvas, scene_point(canvas, *self.GRAB),
             QPointF(rect.right() + 400, rect.top() + 0.3 * rect.height()))

        assert max(p[0] for p in shape.points) == pytest.approx(1.0, abs=0.01)
        width = abs(shape.points[1][0] - shape.points[0][0])
        assert width == pytest.approx(0.4, abs=0.02), "the shape changed size while moving"

    def test_dragging_a_corner_handle_resizes(self, canvas):
        shape = self._add_selected_rect(canvas)
        drag(canvas, scene_point(canvas, 0.6, 0.6), scene_point(canvas, 0.85, 0.9))

        assert shape.points[0][0] == pytest.approx(0.2, abs=0.02), "the far corner moved"
        assert shape.points[1][0] == pytest.approx(0.85, abs=0.02)
        assert shape.points[1][1] == pytest.approx(0.9, abs=0.02)

    def test_dragging_an_edge_handle_resizes_one_axis(self, canvas):
        shape = self._add_selected_rect(canvas)
        # Right edge midpoint of the 0.2..0.6 square.
        drag(canvas, scene_point(canvas, 0.6, 0.4), scene_point(canvas, 0.9, 0.4))

        assert shape.points[1][0] == pytest.approx(0.9, abs=0.02)
        assert shape.points[0][1] == pytest.approx(0.2, abs=0.02)
        assert shape.points[1][1] == pytest.approx(0.6, abs=0.02)

    def test_arrow_endpoints_are_draggable(self, canvas):
        canvas.state.set_tool("arrow")
        drag(canvas, scene_point(canvas, 0.2, 0.8), scene_point(canvas, 0.7, 0.3))
        canvas.state.set_tool("select")
        shape = only_shape(canvas)

        drag(canvas, scene_point(canvas, 0.7, 0.3), scene_point(canvas, 0.9, 0.15))
        assert shape.points[1][0] == pytest.approx(0.9, abs=0.02)
        assert shape.points[0][0] == pytest.approx(0.2, abs=0.02), "the tail moved"


class TestProperties:
    def test_panel_does_not_write_back_while_syncing(self, canvas, qapp):
        """Syncing the panel from the model re-emits valueChanged. Without the
        guard that writes the displayed value straight back, which silently
        undoes an undo."""
        from sve.ui.inspector import InspectorPanel

        canvas.state.set_tool("rect")
        drag(canvas, scene_point(canvas, 0.2, 0.2), scene_point(canvas, 0.6, 0.6))
        shape = only_shape(canvas)
        shape.start, shape.end = 1.0, 4.0

        panel = InspectorPanel(canvas.state)
        canvas.state.selectionChanged.emit(shape)
        qapp.processEvents()

        # The panel shows the model's values...
        assert panel.start_spin.value() == pytest.approx(1.0)
        assert panel.end_spin.value() == pytest.approx(4.0)
        # ...and displaying them did not write them back as an edit.
        assert shape.start == pytest.approx(1.0)
        assert shape.end == pytest.approx(4.0)
        assert (
            not canvas.state.document.can_undo
            or canvas.state.document.undo_label != "Change shape timing"
        )

    def test_setting_end_before_start_does_not_invert_the_range(self, canvas, qapp):
        from sve.ui.inspector import InspectorPanel

        canvas.state.set_tool("rect")
        drag(canvas, scene_point(canvas, 0.2, 0.2), scene_point(canvas, 0.6, 0.6))
        shape = only_shape(canvas)
        shape.start, shape.end = 2.0, 4.0

        panel = InspectorPanel(canvas.state)
        canvas.state.selectionChanged.emit(shape)
        panel.end_spin.setValue(1.0)
        qapp.processEvents()

        assert shape.end >= shape.start, "an inverted range makes a shape that never appears"

    def test_stroke_width_is_stored_normalized(self, canvas, qapp):
        from sve.ui.inspector import InspectorPanel

        canvas.state.set_tool("rect")
        drag(canvas, scene_point(canvas, 0.2, 0.2), scene_point(canvas, 0.6, 0.6))
        shape = only_shape(canvas)

        panel = InspectorPanel(canvas.state)
        canvas.state.selectionChanged.emit(shape)
        panel.stroke_spin.setValue(18)
        qapp.processEvents()

        # 18px at the project's 720px height.
        assert shape.stroke_width == pytest.approx(18 / 720, rel=1e-6)


def test_unfilled_shape_is_grabbed_by_its_outline_only(canvas):
    """Deliberate: a large empty box must not swallow clicks meant for what is
    behind it. Filling it makes the whole interior grabbable."""
    canvas.state.set_tool("rect")
    drag(canvas, scene_point(canvas, 0.1, 0.1), scene_point(canvas, 0.9, 0.9))
    canvas.state.set_tool("select")
    shape = only_shape(canvas)

    canvas.state.select_shape(None)
    canvas.annotations.mousePressEvent(
        _event(QEvent.Type.GraphicsSceneMousePress, scene_point(canvas, 0.5, 0.5))
    )
    assert canvas.state.current_shape is None

    shape.fill_color = "#0000ff80"
    canvas.annotations.mousePressEvent(
        _event(QEvent.Type.GraphicsSceneMousePress, scene_point(canvas, 0.5, 0.5))
    )
    assert canvas.state.current_shape is shape


class TestThumbnails:
    def test_signals_outlive_the_job(self, qapp, assets_dir, tmp_path, monkeypatch):
        """QThreadPool deletes a QRunnable the moment run() returns. If the
        job owns its signal emitter, the queued cross-thread delivery is
        dropped and thumbnails are generated but never shown."""
        monkeypatch.setenv("SVE_CACHE_DIR", str(tmp_path))
        from sve.thumbnails import ThumbnailLoader

        loader = ThumbnailLoader()
        received: list[str] = []
        loader.ready.connect(lambda clip_id, _pixmap: received.append(clip_id))
        loader.request("clip-1", str(assets_dir / "clip_720p.mp4"), 0.5)

        deadline = 30_000
        while not received and deadline > 0:
            qapp.processEvents()
            QThread.msleep(20)
            deadline -= 20

        assert received == ["clip-1"], "the thumbnail signal never arrived"
        assert loader.cached("clip-1") is not None
        loader.shutdown()

    def test_cached_pixmap_survives_a_list_rebuild(self, qapp, assets_dir, tmp_path, monkeypatch):
        monkeypatch.setenv("SVE_CACHE_DIR", str(tmp_path))
        from sve.thumbnails import ThumbnailLoader

        loader = ThumbnailLoader()
        got: list[str] = []
        loader.ready.connect(lambda clip_id, _p: got.append(clip_id))
        loader.request("clip-1", str(assets_dir / "clip_720p.mp4"), 0.5)
        deadline = 30_000
        while not got and deadline > 0:
            qapp.processEvents()
            QThread.msleep(20)
            deadline -= 20

        # A rebuild must be able to restore the icon without another ffmpeg run.
        assert loader.cached("clip-1") is not None
        loader.shutdown()
