"""The annotation overlay.

This is a drawing surface that happens to have video behind it. It never
touches a decoded frame, never composites video, and has no notion of
presentation timestamps: it asks the player where it is, picks the shapes whose
time range contains that position, and draws them with the shared renderer.

**Why a QGraphicsScene rather than a transparent widget over QVideoWidget.**
The obvious arrangement - a translucent child ``QWidget`` on top of a
``QVideoWidget`` - does not work on Qt 6. ``QVideoWidget`` renders through its
own surface, which paints over sibling and child widgets alike; verified by
screenshotting both arrangements against live video, where the overlay was
simply absent. ``QGraphicsVideoItem`` is the same stock Qt Multimedia video
sink, but it renders inside a scene, so a sibling ``QGraphicsItem`` composites
above it through the ordinary QPainter path, alpha and all.

Nothing else about the architecture changes: playback is still ``QMediaPlayer``
on the source file, annotation is still QPainter, export is still ffmpeg.

**Letterboxing.** This item's own coordinate space *is* the video picture:
(0, 0) is the top-left of the frame and ``boundingRect()`` is exactly the
picture rect, so converting a mouse position to normalized coordinates is a
plain division with no offset term. :class:`~sve.ui.preview.VideoCanvas`
computes that rect once with :func:`sve.geometry.video_display_rect` and
positions both the video item and this one into it.

That arrangement is deliberate, and it replaced one that was subtly wrong.
Letting ``QGraphicsVideoItem`` do its own aspect fitting looks equivalent, but
the item centres the picture inside the size it is given while reporting a
``boundingRect`` that is already the fitted size. Re-deriving the picture rect
from that bounding rect silently drops the centring offset, and every
annotation is drawn exactly one letterbox bar too high - verified by drawing a
shape at (0,0)-(1,1) over a real video, where the border sat 105px above the
picture in a 650px-tall preview. Nothing about the on-screen result looks
scaled or obviously broken, which is why it warrants this note.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import QBrush, QColor, QCursor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsSceneMouseEvent

from ..model import Shape, new_id
from ..render import bounding_rect, draw_shape, hit_test
from .state import EditorState

HANDLE_SIZE = 9.0
HANDLE_HIT_SLACK = 5.0
MIN_DRAG_PIXELS = 3.0

SELECTION_COLOR = QColor(0, 160, 255)
HANDLE_FILL = QColor(255, 255, 255)

# Handle ids for two-point shapes. Corners move both coordinates; edges move
# one. Arrows expose only their two endpoints, which is what "resize" means
# for an arrow.
BOX_HANDLES = ("tl", "t", "tr", "r", "br", "b", "bl", "l")
ARROW_HANDLES = ("tail", "head")


class AnnotationItem(QGraphicsItem):
    """Draws and edits annotations above a :class:`QGraphicsVideoItem`."""

    def __init__(self, state: EditorState) -> None:
        super().__init__()
        self.state = state
        self._picture_size = QSizeF()
        self.setZValue(10)
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, False)

        # In-progress gesture.
        self._drag_mode: str | None = None      # "create" | "move" | "resize"
        self._drag_handle: str | None = None
        self._drag_origin_norm: QPointF | None = None
        self._drag_start_points: list[list[float]] | None = None
        self._draft: Shape | None = None
        self._press_scene: QPointF | None = None

    # -- geometry ------------------------------------------------------------

    def set_picture_size(self, size: QSizeF) -> None:
        """Set the on-screen size of the video picture.

        Called by the canvas, which also positions this item at the picture's
        top-left. Item coordinates and picture coordinates are then the same
        thing.
        """
        if size == self._picture_size:
            return
        self.prepareGeometryChange()
        self._picture_size = QSizeF(size)
        self.update()

    def boundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, self._picture_size.width(), self._picture_size.height())

    def video_rect(self) -> QRectF:
        """The picture rect, in this item's own coordinates - always at 0, 0."""
        return self.boundingRect()

    def to_normalized(self, scene_point: QPointF, *, clamp: bool = True) -> QPointF | None:
        rect = self.boundingRect()
        if rect.isEmpty():
            return None
        local = self.mapFromScene(scene_point)
        nx = local.x() / rect.width()
        ny = local.y() / rect.height()
        if clamp:
            nx, ny = min(1.0, max(0.0, nx)), min(1.0, max(0.0, ny))
        return QPointF(nx, ny)

    def to_frame_pixels(self, scene_point: QPointF) -> QPointF | None:
        """Scene position -> pixels within the video frame (renderer space)."""
        if self.boundingRect().isEmpty():
            return None
        return self.mapFromScene(scene_point)

    # -- painting ------------------------------------------------------------

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rect = self.video_rect()
        if rect.isEmpty():
            return

        shapes = list(self.state.visible_shapes())
        if self._draft is not None:
            shapes.append(self._draft)

        painter.save()
        painter.setClipRect(rect)
        size = rect.size()
        for shape in shapes:
            draw_shape(painter, shape, size)
        painter.restore()

        # Selection chrome is drawn *after* and *outside* the shared renderer,
        # so it can never reach an export.
        selected = self.state.current_shape
        if selected is not None and selected.visible_at(self.state.playhead):
            self._paint_selection(painter, selected, rect)

    def _paint_selection(self, painter: QPainter, shape: Shape, rect: QRectF) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        size = rect.size()

        outline = bounding_rect(shape, size)
        pen = QPen(SELECTION_COLOR)
        pen.setWidthF(1.0)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(outline.adjusted(-2, -2, 2, 2))

        painter.setPen(QPen(SELECTION_COLOR, 1.0))
        painter.setBrush(QBrush(HANDLE_FILL))
        for point in self.handle_positions(shape, size).values():
            painter.drawRect(self._handle_rect(point))
        painter.restore()

    @staticmethod
    def _handle_rect(centre: QPointF) -> QRectF:
        half = HANDLE_SIZE / 2.0
        return QRectF(centre.x() - half, centre.y() - half, HANDLE_SIZE, HANDLE_SIZE)

    def handle_positions(self, shape: Shape, size: QSizeF) -> dict[str, QPointF]:
        """Handle centres in frame-pixel space."""
        if shape.type == "text":
            return {}
        a = QPointF(shape.points[0][0] * size.width(), shape.points[0][1] * size.height())
        b = QPointF(shape.points[1][0] * size.width(), shape.points[1][1] * size.height())
        if shape.type == "arrow":
            return {"tail": a, "head": b}

        rect = QRectF(a, b).normalized()
        cx, cy = rect.center().x(), rect.center().y()
        return {
            "tl": rect.topLeft(), "t": QPointF(cx, rect.top()), "tr": rect.topRight(),
            "r": QPointF(rect.right(), cy), "br": rect.bottomRight(),
            "b": QPointF(cx, rect.bottom()), "bl": rect.bottomLeft(),
            "l": QPointF(rect.left(), cy),
        }

    def handle_at(self, shape: Shape, frame_point: QPointF) -> str | None:
        size = self.video_rect().size()
        slack = HANDLE_SIZE / 2.0 + HANDLE_HIT_SLACK
        for name, centre in self.handle_positions(shape, size).items():
            if abs(frame_point.x() - centre.x()) <= slack and abs(frame_point.y() - centre.y()) <= slack:
                return name
        return None

    # -- hit testing ---------------------------------------------------------

    def shape_at(self, frame_point: QPointF) -> Shape | None:
        """Topmost *visible* shape under the point.

        Only visible shapes are clickable, matching what is on screen. Shapes
        outside the current time range are reachable from the shape list.
        """
        size = self.video_rect().size()
        for shape in reversed(self.state.visible_shapes()):
            if hit_test(shape, frame_point, size):
                return shape
        return None

    # -- mouse ---------------------------------------------------------------

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        clip = self.state.current_clip
        frame_point = self.to_frame_pixels(event.scenePos())
        if clip is None or frame_point is None:
            event.ignore()
            return

        self._press_scene = event.scenePos()
        tool = self.state.tool

        if tool == "select":
            self._begin_select_gesture(frame_point)
        else:
            self._begin_create_gesture(event.scenePos(), tool)
        event.accept()

    def _begin_select_gesture(self, frame_point: QPointF) -> None:
        selected = self.state.current_shape
        if selected is not None and selected.visible_at(self.state.playhead):
            handle = self.handle_at(selected, frame_point)
            if handle is not None:
                self._drag_mode = "resize"
                self._drag_handle = handle
                self._drag_start_points = [list(p) for p in selected.points]
                self._drag_origin_norm = self._norm_of(frame_point)
                self.state.document.begin("Resize shape")
                return

        hit = self.shape_at(frame_point)
        self.state.select_shape(hit.id if hit else None)
        if hit is not None:
            self._drag_mode = "move"
            self._drag_start_points = [list(p) for p in hit.points]
            self._drag_origin_norm = self._norm_of(frame_point)
            self.state.document.begin("Move shape")
        self.update()

    def _begin_create_gesture(self, scene_point: QPointF, tool: str) -> None:
        norm = self.to_normalized(scene_point)
        if norm is None:
            return
        start, end = self._default_time_range()
        common = dict(start=start, end=end, id=new_id())

        if tool == "text":
            # Text is placed with a click; there is nothing to drag out.
            shape = Shape(type="text", points=[[norm.x(), norm.y()]], text="Text", **common)
            self._commit_new_shape(shape)
            return

        self._drag_mode = "create"
        self._drag_origin_norm = norm
        self._draft = Shape(
            type=tool,
            points=[[norm.x(), norm.y()], [norm.x(), norm.y()]],
            **common,
        )
        self.update()

    def _default_time_range(self) -> tuple[float, float]:
        """New shapes appear at the playhead and run to the end of the clip."""
        clip = self.state.current_clip
        start = self.state.playhead
        if clip is None:
            return start, start + 1.0
        end = clip.out_point
        if end <= start:
            end = start + 1.0
        return start, end

    def _norm_of(self, frame_point: QPointF) -> QPointF:
        rect = self.boundingRect()
        return QPointF(frame_point.x() / rect.width(), frame_point.y() / rect.height())

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._drag_mode is None:
            event.ignore()
            return
        norm = self.to_normalized(event.scenePos())
        if norm is None:
            return

        if self._drag_mode == "create" and self._draft is not None:
            self._draft.points[1] = [norm.x(), norm.y()]
        elif self._drag_mode == "move":
            self._apply_move(norm)
        elif self._drag_mode == "resize":
            self._apply_resize(norm)
        self.update()
        event.accept()

    def _apply_move(self, norm: QPointF) -> None:
        shape = self.state.current_shape
        if shape is None or self._drag_start_points is None or self._drag_origin_norm is None:
            return
        dx = norm.x() - self._drag_origin_norm.x()
        dy = norm.y() - self._drag_origin_norm.y()

        # Clamp the *whole* shape so a drag pins at the frame edge instead of
        # letting one corner slide outside while the rest follows.
        xs = [p[0] for p in self._drag_start_points]
        ys = [p[1] for p in self._drag_start_points]
        dx = min(max(dx, -min(xs)), 1.0 - max(xs))
        dy = min(max(dy, -min(ys)), 1.0 - max(ys))

        shape.points = [[p[0] + dx, p[1] + dy] for p in self._drag_start_points]
        self.state.notify_shapes_changed()

    def _apply_resize(self, norm: QPointF) -> None:
        shape = self.state.current_shape
        if shape is None or self._drag_start_points is None:
            return
        points = [list(p) for p in self._drag_start_points]
        handle = self._drag_handle

        if shape.type == "arrow":
            points[0 if handle == "tail" else 1] = [norm.x(), norm.y()]
        else:
            # points[0]/points[1] are opposite corners in whatever order the
            # user drew them, so map the handle through the normalized rect.
            left, right = sorted((points[0][0], points[1][0]))
            top, bottom = sorted((points[0][1], points[1][1]))
            if handle and "l" in handle:
                left = norm.x()
            if handle and "r" in handle:
                right = norm.x()
            if handle and "t" in handle:
                top = norm.y()
            if handle and "b" in handle:
                bottom = norm.y()
            points = [[left, top], [right, bottom]]

        shape.points = points
        self.state.notify_shapes_changed()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        mode, self._drag_mode = self._drag_mode, None
        draft, self._draft = self._draft, None
        self._drag_handle = None
        self._drag_start_points = None

        if mode == "create" and draft is not None:
            if self._is_degenerate(draft, event.scenePos()):
                self.update()
                event.accept()
                return
            self._commit_new_shape(draft)
        elif mode in ("move", "resize"):
            self.state.document.commit()
        self.update()
        event.accept()

    def _is_degenerate(self, draft: Shape, release_scene: QPointF) -> bool:
        """Discard click-without-drag, which would leave a zero-size shape."""
        if self._press_scene is None:
            return True
        delta = release_scene - self._press_scene
        return abs(delta.x()) < MIN_DRAG_PIXELS and abs(delta.y()) < MIN_DRAG_PIXELS

    def _commit_new_shape(self, shape: Shape) -> None:
        clip = self.state.current_clip
        if clip is None:
            return
        label = f"Add {shape.type}"
        with self.state.document.edit(label):
            clip.shapes.append(shape)
        self.state.select_shape(shape.id)
        self.state.set_tool("select")
        self.state.status(f"{label} ({shape.start:.2f}s - {shape.end:.2f}s)")
        self.update()

    # -- hover ---------------------------------------------------------------

    def hoverMoveEvent(self, event) -> None:
        cursor = Qt.CursorShape.ArrowCursor
        if self.state.tool != "select":
            cursor = Qt.CursorShape.CrossCursor
        else:
            frame_point = self.to_frame_pixels(event.scenePos())
            selected = self.state.current_shape
            if frame_point is not None:
                if selected is not None and selected.visible_at(self.state.playhead) \
                        and self.handle_at(selected, frame_point):
                    cursor = Qt.CursorShape.SizeAllCursor
                elif self.shape_at(frame_point) is not None:
                    cursor = Qt.CursorShape.OpenHandCursor
        self.setCursor(QCursor(cursor))
        super().hoverMoveEvent(event)


__all__ = ["ARROW_HANDLES", "BOX_HANDLES", "AnnotationItem"]
