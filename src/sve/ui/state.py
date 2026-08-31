"""Shared editor state.

The panels (timeline, preview, tool palette, shape list) all need the same few
pieces of state and all need to react when any of it changes. Threading half a
dozen callbacks between them produces a web nothing can be reasoned about, so
they share one observable object instead: panels read from it, mutate through
it, and subscribe to its signals.

The :class:`~sve.undo.Document` remains the owner of project data and history;
this adds only the things that are *editor* state rather than *document*
state - which clip is open, what is selected, which tool is armed, where the
playhead is. None of that is saved to the project file.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..model import Clip, Project, Shape
from ..undo import Document

TOOLS = ("select", "arrow", "rect", "ellipse", "text")


class EditorState(QObject):
    # The document changed structurally: reload everything.
    projectChanged = Signal()
    # A different clip is open in the preview.
    currentClipChanged = Signal(object)   # Clip | None
    # The selected shape changed (or was deselected).
    selectionChanged = Signal(object)     # Shape | None
    # A shape's geometry or properties changed; repaint but do not rebuild.
    shapesChanged = Signal()
    toolChanged = Signal(str)
    # Playhead position within the *source file*, in seconds.
    playheadChanged = Signal(float)
    statusMessage = Signal(str)

    def __init__(self, document: Document | None = None) -> None:
        super().__init__()
        self.document = document or Document()
        self._current_clip_id: str | None = None
        self._selected_shape_id: str | None = None
        self._tool = "select"
        self._playhead = 0.0
        self.document.add_listener(self._on_document_changed)

    # -- document ------------------------------------------------------------

    @property
    def project(self) -> Project:
        return self.document.project

    def _on_document_changed(self) -> None:
        """Re-validate editor state after undo/redo or a load swaps the project.

        Undo replaces the whole project object, so the ids we hold may no
        longer resolve. Drop them rather than letting stale ids surface as
        confusing no-ops.
        """
        if self._current_clip_id and self.project.clip_by_id(self._current_clip_id) is None:
            self._current_clip_id = self.project.clips[0].id if self.project.clips else None
        if self.current_shape is None:
            self._selected_shape_id = None
        self.projectChanged.emit()
        self.currentClipChanged.emit(self.current_clip)
        self.selectionChanged.emit(self.current_shape)

    # -- current clip --------------------------------------------------------

    @property
    def current_clip(self) -> Clip | None:
        if self._current_clip_id is None:
            return None
        return self.project.clip_by_id(self._current_clip_id)

    def set_current_clip(self, clip_id: str | None) -> None:
        if clip_id == self._current_clip_id:
            return
        self._current_clip_id = clip_id
        self._selected_shape_id = None
        clip = self.current_clip
        self._playhead = clip.in_point if clip else 0.0
        self.currentClipChanged.emit(clip)
        self.selectionChanged.emit(None)

    # -- selection -----------------------------------------------------------

    @property
    def current_shape(self) -> Shape | None:
        clip = self.current_clip
        if clip is None or self._selected_shape_id is None:
            return None
        return next((s for s in clip.shapes if s.id == self._selected_shape_id), None)

    def select_shape(self, shape_id: str | None) -> None:
        if shape_id == self._selected_shape_id:
            return
        self._selected_shape_id = shape_id
        self.selectionChanged.emit(self.current_shape)

    # -- tool ----------------------------------------------------------------

    @property
    def tool(self) -> str:
        return self._tool

    def set_tool(self, tool: str) -> None:
        if tool not in TOOLS or tool == self._tool:
            return
        self._tool = tool
        self.toolChanged.emit(tool)

    # -- playhead ------------------------------------------------------------

    @property
    def playhead(self) -> float:
        """Playhead position in *source file* seconds.

        Source time, not clip-relative and not timeline-global, so it can be
        compared directly against ``Shape.start``/``end`` and against the
        clip's in/out points without conversion.
        """
        return self._playhead

    def set_playhead(self, seconds: float) -> None:
        if abs(seconds - self._playhead) < 1e-6:
            return
        self._playhead = seconds
        self.playheadChanged.emit(seconds)

    # -- convenience ---------------------------------------------------------

    def visible_shapes(self) -> list[Shape]:
        clip = self.current_clip
        if clip is None:
            return []
        return [s for s in clip.shapes if s.visible_at(self._playhead)]

    def notify_shapes_changed(self) -> None:
        self.shapesChanged.emit()

    def status(self, message: str) -> None:
        self.statusMessage.emit(message)


__all__ = ["TOOLS", "EditorState"]
