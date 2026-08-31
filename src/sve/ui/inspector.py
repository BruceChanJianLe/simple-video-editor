"""Tool palette and shape properties.

The properties panel edits the selected shape in place. Every widget here
follows the same pattern: block signals while syncing *from* the model, and
wrap each user edit in a document transaction so it lands as one undo step.
Without the guard, syncing the panel after an undo re-emits valueChanged and
writes the old value straight back.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QColorDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..model import Shape
from ..render import color, color_to_spec
from .state import TOOLS, EditorState

TOOL_LABELS = {
    "select": ("Select", "V"),
    "arrow": ("Arrow", "A"),
    "rect": ("Box", "R"),
    "ellipse": ("Ellipse", "E"),
    "text": ("Text", "T"),
}


class ColorButton(QPushButton):
    """A swatch that opens a colour picker, alpha included."""

    colorChanged = Signal(str)

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = label
        self._spec = "#00000000"
        self.setFixedHeight(26)
        self.clicked.connect(self._pick)

    def spec(self) -> str:
        return self._spec

    def set_spec(self, spec: str) -> None:
        self._spec = spec
        value = color(spec)
        text_color = "#000000" if value.lightness() > 128 and value.alpha() > 96 else "#ffffff"
        if value.alpha() == 0:
            self.setStyleSheet("")
            self.setText(f"{self._label}: none")
        else:
            self.setStyleSheet(
                f"background-color: rgba({value.red()},{value.green()},"
                f"{value.blue()},{value.alpha() / 255:.3f}); color: {text_color};"
            )
            self.setText(f"{self._label}: {spec}")

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(
            color(self._spec),
            self,
            f"Choose {self._label.lower()} colour",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if chosen.isValid():
            self.set_spec(color_to_spec(chosen))
            self.colorChanged.emit(self._spec)


class InspectorPanel(QWidget):
    """Tool palette above the selected shape's properties."""

    def __init__(self, state: EditorState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)
        layout.addWidget(self._build_tools())
        layout.addWidget(self._build_properties())
        layout.addStretch(1)

        state.toolChanged.connect(self._sync_tool)
        state.selectionChanged.connect(self._sync_shape)
        state.shapesChanged.connect(lambda: self._sync_shape(state.current_shape))
        state.projectChanged.connect(lambda: self._sync_shape(state.current_shape))
        state.playheadChanged.connect(lambda *_: self._update_playhead_buttons())
        self._sync_tool(state.tool)
        self._sync_shape(None)

    # -- tools ---------------------------------------------------------------

    def _build_tools(self) -> QGroupBox:
        box = QGroupBox("Tools")
        row = QHBoxLayout(box)
        row.setContentsMargins(8, 8, 8, 8)
        row.setSpacing(4)

        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(True)
        self._tool_buttons: dict[str, QPushButton] = {}

        for tool in TOOLS:
            label, key = TOOL_LABELS[tool]
            button = QPushButton(label)
            button.setCheckable(True)
            button.setToolTip(f"{label} ({key})")
            button.clicked.connect(lambda _checked, t=tool: self.state.set_tool(t))
            self._tool_group.addButton(button)
            self._tool_buttons[tool] = button
            row.addWidget(button)
        return box

    def _sync_tool(self, tool: str) -> None:
        button = self._tool_buttons.get(tool)
        if button is not None:
            button.setChecked(True)

    # -- properties ----------------------------------------------------------

    def _build_properties(self) -> QGroupBox:
        box = QGroupBox("Shape")
        self._props_box = box
        form = QFormLayout(box)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(4)

        self.type_label = QLabel("-")

        self.start_spin = QDoubleSpinBox()
        self.end_spin = QDoubleSpinBox()
        for spin in (self.start_spin, self.end_spin):
            spin.setDecimals(3)
            spin.setSingleStep(0.1)
            spin.setSuffix(" s")
            spin.setMaximum(24 * 3600.0)
        self.start_spin.valueChanged.connect(lambda v: self._set_time("start", v))
        self.end_spin.valueChanged.connect(lambda v: self._set_time("end", v))

        self.start_here = QPushButton("Set start to playhead")
        self.start_here.clicked.connect(lambda: self.start_spin.setValue(self.state.playhead))
        self.end_here = QPushButton("Set end to playhead")
        self.end_here.clicked.connect(lambda: self.end_spin.setValue(self.state.playhead))

        self.stroke_color = ColorButton("Stroke")
        self.stroke_color.colorChanged.connect(lambda s: self._set_field("stroke_color", s, "Change stroke colour"))
        self.fill_color = ColorButton("Fill")
        self.fill_color.colorChanged.connect(lambda s: self._set_field("fill_color", s, "Change fill colour"))

        # Stroke width and font size are stored normalized; the user thinks in
        # pixels at the output resolution, so convert at the boundary.
        self.stroke_spin = QSpinBox()
        self.stroke_spin.setRange(1, 200)
        self.stroke_spin.setSuffix(" px")
        self.stroke_spin.valueChanged.connect(self._set_stroke_width)

        self.font_spin = QSpinBox()
        self.font_spin.setRange(4, 500)
        self.font_spin.setSuffix(" px")
        self.font_spin.valueChanged.connect(self._set_font_size)

        self.text_edit = QLineEdit()
        self.text_edit.setPlaceholderText("Label text")
        self.text_edit.textEdited.connect(lambda t: self._set_field("text", t, "Edit text"))

        form.addRow("Type", self.type_label)
        form.addRow("Text", self.text_edit)
        form.addRow("Start", self.start_spin)
        form.addRow("End", self.end_spin)
        form.addRow("", self.start_here)
        form.addRow("", self.end_here)
        form.addRow("Stroke", self.stroke_color)
        form.addRow("Width", self.stroke_spin)
        form.addRow("Fill", self.fill_color)
        form.addRow("Font", self.font_spin)

        self._text_rows = (self.text_edit, self.font_spin)
        box.setEnabled(False)
        return box

    # -- syncing -------------------------------------------------------------

    def _sync_shape(self, shape: Shape | None) -> None:
        self._syncing = True
        try:
            self._props_box.setEnabled(shape is not None)
            if shape is None:
                self.type_label.setText("No shape selected")
                self.text_edit.clear()
                return

            self.type_label.setText(shape.type)
            self.start_spin.setValue(shape.start)
            self.end_spin.setValue(shape.end)
            self.stroke_color.set_spec(shape.stroke_color)
            self.fill_color.set_spec(shape.fill_color)

            height = self.state.project.output.height or 1080
            self.stroke_spin.setValue(max(1, round(shape.stroke_width * height)))
            self.font_spin.setValue(max(4, round(shape.font_size * height)))

            is_text = shape.type == "text"
            if self.text_edit.text() != shape.text:
                self.text_edit.setText(shape.text)
            for widget in self._text_rows:
                widget.setEnabled(is_text)
            self.stroke_spin.setEnabled(not is_text)
        finally:
            self._syncing = False
        self._update_playhead_buttons()

    def _update_playhead_buttons(self) -> None:
        enabled = self.state.current_shape is not None
        self.start_here.setEnabled(enabled)
        self.end_here.setEnabled(enabled)

    def focus_text(self) -> None:
        """Called after creating a text shape so the user can just type."""
        self.text_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.text_edit.selectAll()

    # -- mutation ------------------------------------------------------------

    def _set_field(self, field: str, value, label: str) -> None:
        if self._syncing:
            return
        shape = self.state.current_shape
        if shape is None or getattr(shape, field) == value:
            return
        with self.state.document.edit(label):
            setattr(shape, field, value)
        self.state.notify_shapes_changed()

    def _set_time(self, field: str, value: float) -> None:
        if self._syncing:
            return
        shape = self.state.current_shape
        if shape is None or abs(getattr(shape, field) - value) < 1e-6:
            return
        with self.state.document.edit("Change shape timing"):
            setattr(shape, field, value)
            # Keep the range non-inverted; an inverted range means a shape
            # that never appears, which reads as a bug rather than an edit.
            if shape.end < shape.start:
                if field == "start":
                    shape.end = shape.start
                else:
                    shape.start = shape.end
        self._sync_shape(shape)
        self.state.notify_shapes_changed()

    def _set_stroke_width(self, pixels: int) -> None:
        if self._syncing:
            return
        height = self.state.project.output.height or 1080
        self._set_field("stroke_width", pixels / height, "Change stroke width")

    def _set_font_size(self, pixels: int) -> None:
        if self._syncing:
            return
        height = self.state.project.output.height or 1080
        self._set_field("font_size", pixels / height, "Change font size")


__all__ = ["ColorButton", "InspectorPanel"]
