"""The speed-sections panel: spans of a clip that play at their own rate.

Lives in the inspector column, below the shape properties, because it edits
the *current clip* rather than the timeline's order - and because the
timeline column has no vertical room to spare.

Follows the same editing pattern as the trim controls and the shape
properties: block signals while syncing *from* the model (the ``_rebuilding``
guard), and wrap each user edit in one document transaction so it lands as one
undo step.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..model import SPEED_MAX, SPEED_MIN, Clip, SpeedSection, clamp_speed
from .state import EditorState

# A new section's rate. 1.0 would be a no-op; doubling is the common ask.
DEFAULT_SECTION_SPEED = 2.0


class SpeedSectionsPanel(QGroupBox):
    """List of the current clip's speed sections plus editors for one."""

    def __init__(self, state: EditorState, parent: QWidget | None = None) -> None:
        super().__init__("Speed sections", parent)
        self.state = state
        self._rebuilding = False
        self.setToolTip(
            "Spans of this clip that play at their own rate; the rest of the"
            " clip plays at the clip's Speed. Times are in the source clip's"
            " clock, like shape times, so retrimming does not move them."
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(4)

        self.section_list = QListWidget()
        self.section_list.setMaximumHeight(96)
        self.section_list.currentRowChanged.connect(self._on_selected)

        self.add_button = QPushButton("Add at playhead")
        self.add_button.clicked.connect(self.add_at_playhead)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self.remove_selected)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.add_button)
        row.addWidget(self.remove_button)

        self.start_spin = QDoubleSpinBox()
        self.end_spin = QDoubleSpinBox()
        for spin in (self.start_spin, self.end_spin):
            spin.setDecimals(3)
            spin.setSingleStep(0.1)
            spin.setSuffix(" s")
            spin.setMaximum(24 * 3600.0)
        self.start_spin.valueChanged.connect(lambda v: self._set_field("start", v))
        self.end_spin.valueChanged.connect(lambda v: self._set_field("end", v))

        self.start_here = QPushButton("Set start to playhead")
        self.start_here.clicked.connect(
            lambda: self.start_spin.setValue(self.state.playhead))
        self.end_here = QPushButton("Set end to playhead")
        self.end_here.clicked.connect(
            lambda: self.end_spin.setValue(self.state.playhead))

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setDecimals(2)
        self.speed_spin.setRange(SPEED_MIN, SPEED_MAX)
        self.speed_spin.setSingleStep(0.25)
        self.speed_spin.setValue(1.0)
        self.speed_spin.setSuffix(" x")
        self.speed_spin.valueChanged.connect(lambda v: self._set_field("speed", v))

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(4)
        form.addRow("Start", self.start_spin)
        form.addRow("", self.start_here)
        form.addRow("End", self.end_spin)
        form.addRow("", self.end_here)
        form.addRow("Speed", self.speed_spin)

        outer.addWidget(self.section_list)
        outer.addLayout(row)
        outer.addLayout(form)
        self.setEnabled(False)

        state.projectChanged.connect(self._sync)
        state.currentClipChanged.connect(lambda *_: self._sync())
        self._sync()

    # -- model access ----------------------------------------------------

    def _clip(self) -> Clip | None:
        clip = self.state.current_clip
        return clip if clip is not None and clip.kind == "video" else None

    def _selected(self) -> SpeedSection | None:
        clip = self._clip()
        row = self.section_list.currentRow()
        if clip is None or not 0 <= row < len(clip.speed_sections):
            return None
        return clip.speed_sections[row]

    # -- edits -----------------------------------------------------------

    def add_at_playhead(self) -> None:
        clip = self._clip()
        if clip is None:
            return
        start = min(max(self.state.playhead, clip.in_point), clip.out_point)
        # Run to the out point, like a new shape does, but never into the
        # next section.
        end = clip.out_point
        for section in clip.speed_sections:
            if section.start <= start < section.end:
                self.state.status("The playhead is already inside a speed section.")
                return
            if section.start > start:
                end = min(end, section.start)
                break
        if end - start < _min_section_span(clip):
            self.state.status("No room for a speed section at the playhead.")
            return
        section = SpeedSection(start=start, end=end, speed=DEFAULT_SECTION_SPEED)
        with self.state.document.edit("Add speed section"):
            clip.speed_sections.append(section)
            clip.speed_sections.sort(key=lambda s: s.start)
        self._sync(select=section.id)

    def remove_selected(self) -> None:
        clip = self._clip()
        section = self._selected()
        if clip is None or section is None:
            return
        with self.state.document.edit("Remove speed section"):
            clip.speed_sections.remove(section)
        self._sync()

    def _set_field(self, name: str, value: float) -> None:
        if self._rebuilding:
            return
        clip = self._clip()
        section = self._selected()
        if clip is None or section is None or abs(getattr(section, name) - value) < 1e-6:
            return

        # Clamp between the neighbouring sections, the way the trim spinboxes
        # clamp in against out, so the list can never hold an overlap.
        position = clip.speed_sections.index(section)
        span = _min_section_span(clip)
        with self.state.document.edit("Edit speed section"):
            if name == "start":
                lower = (clip.speed_sections[position - 1].end if position > 0 else 0.0)
                section.start = min(max(value, lower), section.end - span)
            elif name == "end":
                if position + 1 < len(clip.speed_sections):
                    upper = clip.speed_sections[position + 1].start
                else:
                    upper = clip.info.duration or section.end
                section.end = min(max(value, section.start + span), upper)
            else:
                section.speed = clamp_speed(value)
        self._sync(select=section.id)

    # -- syncing from the model -------------------------------------------

    def _on_selected(self, _row: int) -> None:
        if not self._rebuilding:
            self._sync_editors()

    def _sync(self, select: str | None = None) -> None:
        clip = self._clip()
        if select is None:
            previous = self._selected()
            select = previous.id if previous else None

        self._rebuilding = True
        self.section_list.clear()
        row = -1
        if clip is not None:
            for index, section in enumerate(clip.speed_sections):
                self.section_list.addItem(
                    f"{section.start:.2f}s to {section.end:.2f}s  @ {section.speed:g}x")
                if section.id == select:
                    row = index
            if row < 0 and clip.speed_sections:
                row = 0
            self.section_list.setCurrentRow(row)
        self._rebuilding = False
        self._sync_editors()

    def _sync_editors(self) -> None:
        clip = self._clip()
        self.setEnabled(clip is not None)
        section = self._selected()
        self._rebuilding = True
        for widget in (self.start_spin, self.end_spin, self.speed_spin,
                       self.start_here, self.end_here, self.remove_button):
            widget.setEnabled(section is not None)
        if section is not None:
            self.start_spin.setValue(section.start)
            self.end_spin.setValue(section.end)
            self.speed_spin.setValue(section.speed)
        else:
            self.start_spin.setValue(0.0)
            self.end_spin.setValue(0.0)
            self.speed_spin.setValue(1.0)
        self._rebuilding = False


def _min_section_span(clip: Clip) -> float:
    """Smallest allowed section: one frame of the source."""
    fps = clip.info.fps if clip.info.fps > 0 else 30.0
    return 1.0 / fps


__all__ = ["SpeedSectionsPanel"]
