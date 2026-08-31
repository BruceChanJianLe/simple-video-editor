"""Timeline strip: the ordered list of clips, with trim controls.

A list rather than a scrubbable multi-track timeline, because the project has
exactly one track and clips play back to back. Reordering is drag-and-drop;
the model is rewritten from the list's order on drop, so what is on screen is
always what will be exported.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..model import Clip
from ..thumbnails import THUMB_HEIGHT, THUMB_WIDTH, ThumbnailLoader
from .state import EditorState

CLIP_ID_ROLE = Qt.ItemDataRole.UserRole + 1


class TimelinePanel(QWidget):
    """Clip list plus per-clip trim controls."""

    addClipsRequested = Signal()

    def __init__(self, state: EditorState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._rebuilding = False

        self.list = QListWidget()
        self.list.setIconSize(QSize(THUMB_WIDTH, THUMB_HEIGHT))
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setUniformItemSizes(False)
        self.list.setSpacing(2)
        self.list.currentItemChanged.connect(self._on_current_changed)
        self.list.model().rowsMoved.connect(self._on_rows_moved)

        self.add_button = QPushButton("Add media...")
        self.add_button.clicked.connect(self.addClipsRequested.emit)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self.remove_current)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)

        self.trim_box = self._build_trim_box()
        self.total_label = QLabel("Total: 0:00.00")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addWidget(QLabel("<b>Timeline</b>"))
        layout.addWidget(self.list, 1)
        layout.addWidget(self.total_label)
        layout.addLayout(buttons)
        layout.addWidget(self.trim_box)

        self.thumbnails = ThumbnailLoader(self)
        self.thumbnails.ready.connect(self._on_thumbnail)

        state.projectChanged.connect(self.rebuild)
        state.currentClipChanged.connect(self._sync_selection)
        state.playheadChanged.connect(lambda *_: self._update_playhead_buttons())
        self.rebuild()

    # -- trim controls -------------------------------------------------------

    def _build_trim_box(self) -> QGroupBox:
        box = QGroupBox("Clip")
        form = QFormLayout(box)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(4)

        self.in_spin = QDoubleSpinBox()
        self.out_spin = QDoubleSpinBox()
        for spin in (self.in_spin, self.out_spin):
            spin.setDecimals(3)
            spin.setSingleStep(0.1)
            spin.setSuffix(" s")
            spin.setMaximum(24 * 3600.0)
        self.in_spin.valueChanged.connect(lambda v: self._set_trim("in_point", v))
        self.out_spin.valueChanged.connect(lambda v: self._set_trim("out_point", v))

        self.set_in_button = QPushButton("Set in to playhead")
        self.set_in_button.clicked.connect(lambda: self._set_from_playhead("in_point"))
        self.set_out_button = QPushButton("Set out to playhead")
        self.set_out_button.clicked.connect(lambda: self._set_from_playhead("out_point"))

        self.duration_label = QLabel("-")

        form.addRow("In", self.in_spin)
        form.addRow("Out", self.out_spin)
        form.addRow("", self.set_in_button)
        form.addRow("", self.set_out_button)
        form.addRow("Duration", self.duration_label)
        box.setEnabled(False)
        return box

    # -- rebuild -------------------------------------------------------------

    def rebuild(self) -> None:
        self._rebuilding = True
        current_id = self.state.current_clip.id if self.state.current_clip else None
        self.list.clear()
        for index, clip in enumerate(self.state.project.clips):
            item = QListWidgetItem(self._label_for(index, clip))
            item.setData(CLIP_ID_ROLE, clip.id)
            item.setSizeHint(QSize(THUMB_WIDTH + 24, THUMB_HEIGHT + 12))
            item.setToolTip(
                f"{clip.name}\n{clip.source_path}\n"
                f"{clip.info.width}x{clip.info.height}"
                + (f" @ {clip.info.fps:.2f}fps" if clip.kind == "video" else " (still)")
                + f"\nin {clip.in_point:.3f}s  out {clip.out_point:.3f}s"
            )
            self.list.addItem(item)
            # Re-apply a thumbnail we already have: the list is cleared on
            # every rebuild, which drops icons that were set earlier.
            cached = self.thumbnails.cached(clip.id)
            if cached is not None:
                item.setIcon(QIcon(cached))
            self.thumbnails.request(clip.id, clip.source_path, clip.in_point)
            if clip.id == current_id:
                self.list.setCurrentItem(item)
        self._rebuilding = False
        self._sync_trim_controls()
        self._update_total()

    @staticmethod
    def _label_for(index: int, clip: Clip) -> str:
        shapes = len(clip.shapes)
        suffix = f", {shapes} shape{'s' if shapes != 1 else ''}" if shapes else ""
        return f"{index + 1}. {clip.name}\n{clip.duration:.2f}s{suffix}"

    def _update_total(self) -> None:
        total = self.state.project.total_duration
        minutes, rest = divmod(total, 60.0)
        self.total_label.setText(f"Total: {int(minutes)}:{rest:05.2f}  ({len(self.state.project.clips)} clips)")

    def _on_thumbnail(self, clip_id: str, pixmap: QPixmap) -> None:
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(CLIP_ID_ROLE) == clip_id:
                item.setIcon(QIcon(pixmap))
                return

    # -- selection -----------------------------------------------------------

    def _on_current_changed(self, current: QListWidgetItem | None, _previous) -> None:
        if self._rebuilding or current is None:
            return
        self.state.set_current_clip(current.data(CLIP_ID_ROLE))

    def _sync_selection(self, clip: Clip | None) -> None:
        if self._rebuilding:
            return
        target = clip.id if clip else None
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(CLIP_ID_ROLE) == target:
                if self.list.currentItem() is not item:
                    self._rebuilding = True
                    self.list.setCurrentItem(item)
                    self._rebuilding = False
                break
        self._sync_trim_controls()

    # -- reordering ----------------------------------------------------------

    def _on_rows_moved(self, *_args) -> None:
        """Rewrite clip order from the list, which is the user's intent."""
        if self._rebuilding:
            return
        order = [self.list.item(row).data(CLIP_ID_ROLE) for row in range(self.list.count())]
        project = self.state.project
        if order == [c.id for c in project.clips]:
            return
        by_id = {c.id: c for c in project.clips}
        with self.state.document.edit("Reorder clips"):
            project.clips = [by_id[cid] for cid in order if cid in by_id]

    # -- mutation ------------------------------------------------------------

    def remove_current(self) -> None:
        clip = self.state.current_clip
        if clip is None:
            return
        project = self.state.project
        index = project.clip_index(clip.id)
        with self.state.document.edit(f"Remove {clip.name}"):
            project.clips.remove(clip)
        remaining = project.clips
        if remaining:
            self.state.set_current_clip(remaining[min(index, len(remaining) - 1)].id)
        else:
            self.state.set_current_clip(None)

    def _set_trim(self, field: str, value: float) -> None:
        if self._rebuilding:
            return
        clip = self.state.current_clip
        if clip is None or abs(getattr(clip, field) - value) < 1e-6:
            return

        limit = clip.info.duration if clip.kind == "video" else None
        with self.state.document.edit("Trim clip"):
            if field == "in_point":
                upper = clip.out_point - _min_span(clip)
                clip.in_point = max(0.0, min(value, upper))
            else:
                lower = clip.in_point + _min_span(clip)
                value = max(value, lower)
                if limit is not None:
                    value = min(value, limit)
                clip.out_point = value
        self._sync_trim_controls()

    def _set_from_playhead(self, field: str) -> None:
        self._sync_spin(field, self.state.playhead)

    def _sync_spin(self, field: str, value: float) -> None:
        spin = self.in_spin if field == "in_point" else self.out_spin
        spin.setValue(value)

    def _update_playhead_buttons(self) -> None:
        enabled = self.state.current_clip is not None and self.state.current_clip.kind == "video"
        self.set_in_button.setEnabled(enabled)
        self.set_out_button.setEnabled(enabled)

    def _sync_trim_controls(self) -> None:
        clip = self.state.current_clip
        self.trim_box.setEnabled(clip is not None)
        if clip is None:
            self.duration_label.setText("-")
            return

        self._rebuilding = True
        is_image = clip.kind == "image"
        upper = clip.info.duration if not is_image else 24 * 3600.0
        self.in_spin.setMaximum(max(0.0, upper))
        self.out_spin.setMaximum(max(0.0, upper))
        self.in_spin.setValue(clip.in_point)
        self.out_spin.setValue(clip.out_point)
        # A still has no internal timeline; only its duration is meaningful.
        self.in_spin.setEnabled(not is_image)
        self.trim_box.setTitle("Image duration" if is_image else "Clip trim")
        self._rebuilding = False

        self.duration_label.setText(f"{clip.duration:.3f} s")
        self._update_playhead_buttons()


def _min_span(clip: Clip) -> float:
    """Smallest allowed clip length: one frame, or 0.1s for a still."""
    if clip.kind == "image":
        return 0.1
    fps = clip.info.fps if clip.info.fps > 0 else 30.0
    return 1.0 / fps


__all__ = ["TimelinePanel"]
