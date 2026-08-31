"""Shape list for the current clip.

Its job is reaching shapes the overlay cannot offer: anything whose time range
does not contain the playhead is invisible on screen and therefore unclickable
there. Rows show the time range, and selecting one moves the playhead into
that range so the shape is actually on screen when it becomes selected.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..model import Clip, Shape
from .state import EditorState

SHAPE_ID_ROLE = Qt.ItemDataRole.UserRole + 1
COLUMNS = ("Type", "Detail", "Start", "End", "Visible")


class ShapeListPanel(QWidget):
    """Table of the current clip's shapes."""

    seekRequested = Signal(float)

    def __init__(self, state: EditorState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._syncing = False

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._on_selection)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 6)
        layout.addWidget(self.table)

        state.currentClipChanged.connect(lambda *_: self.rebuild())
        state.projectChanged.connect(self.rebuild)
        state.shapesChanged.connect(self.rebuild)
        state.selectionChanged.connect(self._sync_selection)
        state.playheadChanged.connect(lambda *_: self._refresh_visibility())
        self.rebuild()

    # -- rebuild -------------------------------------------------------------

    def rebuild(self) -> None:
        self._syncing = True
        clip: Clip | None = self.state.current_clip
        shapes = clip.shapes if clip else []
        self.table.setRowCount(len(shapes))
        for row, shape in enumerate(shapes):
            self._fill_row(row, shape)
        self._syncing = False
        self._sync_selection(self.state.current_shape)
        self._refresh_visibility()

    def _fill_row(self, row: int, shape: Shape) -> None:
        values = (
            shape.type,
            self._detail(shape),
            f"{shape.start:.2f}s",
            f"{shape.end:.2f}s",
            "",
        )
        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setData(SHAPE_ID_ROLE, shape.id)
            if column >= 2:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, column, item)

    @staticmethod
    def _detail(shape: Shape) -> str:
        if shape.type == "text":
            return shape.text or "(empty)"
        first = shape.points[0]
        return f"({first[0]:.2f}, {first[1]:.2f})"

    def _refresh_visibility(self) -> None:
        clip = self.state.current_clip
        if clip is None:
            return
        playhead = self.state.playhead
        for row, shape in enumerate(clip.shapes):
            item = self.table.item(row, 4)
            if item is not None:
                item.setText("shown" if shape.visible_at(playhead) else "")

    # -- selection -----------------------------------------------------------

    def _on_selection(self) -> None:
        if self._syncing:
            return
        items = self.table.selectedItems()
        if not items:
            return
        shape_id = items[0].data(SHAPE_ID_ROLE)
        self.state.select_shape(shape_id)

        # Move the playhead into the shape's range so selecting it from the
        # list actually shows it.
        shape = self.state.current_shape
        if shape is not None and not shape.visible_at(self.state.playhead) and shape.end > shape.start:
            self.seekRequested.emit(shape.start)

    def _sync_selection(self, shape: Shape | None) -> None:
        clip = self.state.current_clip
        if clip is None:
            return
        self._syncing = True
        if shape is None:
            self.table.clearSelection()
        else:
            for row, candidate in enumerate(clip.shapes):
                if candidate.id == shape.id:
                    self.table.selectRow(row)
                    break
        self._syncing = False


__all__ = ["ShapeListPanel"]
