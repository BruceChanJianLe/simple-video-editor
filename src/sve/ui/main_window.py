"""Main window: menus, layout and the actions that tie the panels together."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
)

from ..export import PREVIEW_ENCODE
from ..importer import MEDIA_FILTER, apply_first_import_defaults, build_clip
from ..model import DEFAULT_IMAGE_DURATION
from ..probe import ProbeError
from ..undo import Document
from .export_dialog import ExportDialog
from .inspector import InspectorPanel
from .preview import PreviewPane
from .shape_list import ShapeListPanel
from .state import EditorState
from .timeline import TimelinePanel

PROJECT_FILTER = "Video editor project (*.sve.json *.json);;All files (*)"
EXPORT_FILTER = "MP4 video (*.mp4);;All files (*)"

TOOL_SHORTCUTS = {
    "V": "select",
    "A": "arrow",
    "R": "rect",
    "E": "ellipse",
    "T": "text",
}


class MainWindow(QMainWindow):
    def __init__(self, document: Document | None = None) -> None:
        super().__init__()
        self.state = EditorState(document or Document())
        self.settings = QSettings("simple-video-editor", "simple-video-editor")

        self.setWindowTitle("Simple Video Editor")
        self.resize(1500, 940)

        self._build_layout()
        self._build_actions()
        self._connect()
        self._update_title()

    # -- layout --------------------------------------------------------------

    def _build_layout(self) -> None:
        self.preview = PreviewPane(self.state)
        self.timeline = TimelinePanel(self.state)
        self.inspector = InspectorPanel(self.state)
        self.shape_list = ShapeListPanel(self.state)

        centre = QSplitter(Qt.Orientation.Vertical)
        centre.addWidget(self.preview)
        centre.addWidget(self.shape_list)
        centre.setStretchFactor(0, 4)
        centre.setStretchFactor(1, 1)
        centre.setSizes([680, 200])

        main = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(self.timeline)
        main.addWidget(centre)
        main.addWidget(self.inspector)
        main.setStretchFactor(0, 0)
        main.setStretchFactor(1, 1)
        main.setStretchFactor(2, 0)
        main.setSizes([300, 900, 300])

        self.setCentralWidget(main)

        self.status_label = QLabel("")
        self.statusBar().addPermanentWidget(self.status_label)

    def _build_actions(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.act_new = self._action("&New project", QKeySequence.StandardKey.New, self.new_project)
        self.act_open = self._action("&Open project...", QKeySequence.StandardKey.Open, self.open_project)
        self.act_save = self._action("&Save project", QKeySequence.StandardKey.Save, self.save_project)
        self.act_save_as = self._action("Save project &as...", QKeySequence.StandardKey.SaveAs, self.save_project_as)
        self.act_import = self._action("&Add media...", QKeySequence("Ctrl+I"), self.import_media)
        self.act_export = self._action("&Export video...", QKeySequence("Ctrl+E"), self.export_video)
        self.act_preview_timeline = self._action(
            "Preview full &timeline", QKeySequence("Ctrl+Shift+P"), self.preview_timeline
        )
        self.act_quit = self._action("&Quit", QKeySequence.StandardKey.Quit, self.close)

        file_menu.addActions([self.act_new, self.act_open])
        file_menu.addSeparator()
        file_menu.addActions([self.act_save, self.act_save_as])
        file_menu.addSeparator()
        file_menu.addAction(self.act_import)
        file_menu.addSeparator()
        file_menu.addActions([self.act_export, self.act_preview_timeline])
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

        edit_menu = self.menuBar().addMenu("&Edit")
        self.act_undo = self._action("&Undo", QKeySequence.StandardKey.Undo, self.undo)
        self.act_redo = self._action("&Redo", QKeySequence.StandardKey.Redo, self.redo)
        self.act_delete = self._action("&Delete selection", QKeySequence.StandardKey.Delete, self.delete_selection)
        edit_menu.addActions([self.act_undo, self.act_redo])
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_delete)

        # Backspace deletes too; on macOS it is the key people actually press.
        self.act_delete_backspace = self._action("", QKeySequence(Qt.Key.Key_Backspace), self.delete_selection)
        self.act_delete_backspace.setVisible(False)

        view_menu = self.menuBar().addMenu("&View")
        self.act_play = self._action("Play / pause", QKeySequence(Qt.Key.Key_Space), self.preview.toggle_play)
        self.act_prev_frame = self._action("Previous frame", QKeySequence(Qt.Key.Key_Left),
                                           lambda: self.preview.step_frames(-1))
        self.act_next_frame = self._action("Next frame", QKeySequence(Qt.Key.Key_Right),
                                           lambda: self.preview.step_frames(1))
        view_menu.addActions([self.act_play, self.act_prev_frame, self.act_next_frame])

        tools_menu = self.menuBar().addMenu("&Tools")
        for key, tool in TOOL_SHORTCUTS.items():
            action = self._action(tool.capitalize(), QKeySequence(key),
                                  lambda _=False, t=tool: self.state.set_tool(t))
            tools_menu.addAction(action)
        tools_menu.addSeparator()
        tools_menu.addAction(self._action("Clear media cache", None, self.clear_cache))

    def _action(self, text: str, shortcut, slot) -> QAction:
        action = QAction(text, self)
        if shortcut is not None:
            action.setShortcut(shortcut)
        action.triggered.connect(slot)
        self.addAction(action)
        return action

    def _connect(self) -> None:
        self.timeline.addClipsRequested.connect(self.import_media)
        self.shape_list.seekRequested.connect(self.preview.seek)
        self.state.statusMessage.connect(self.statusBar().showMessage)
        self.state.projectChanged.connect(self._update_status_hint)
        self.state.projectChanged.connect(self._update_title)
        self.state.projectChanged.connect(self._update_actions)
        self.state.selectionChanged.connect(lambda *_: self._update_actions())
        self.state.currentClipChanged.connect(lambda *_: self._update_actions())
        self._update_actions()
        self._update_status_hint()

    # -- project lifecycle ---------------------------------------------------

    def _confirm_discard(self) -> bool:
        if not self.state.document.dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            "This project has unsaved changes. Save before continuing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save_project()
        return answer == QMessageBox.StandardButton.Discard

    def new_project(self) -> None:
        if not self._confirm_discard():
            return
        self.state.document.reset()
        self.state.set_current_clip(None)
        self.statusBar().showMessage("New project.")

    def open_project(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open project", self._last_dir(), PROJECT_FILTER)
        if not path:
            return
        try:
            self.state.document.load(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not open project", str(exc))
            return
        self._remember_dir(path)
        clips = self.state.project.clips
        self.state.set_current_clip(clips[0].id if clips else None)
        self._warn_about_missing_sources()
        self.statusBar().showMessage(f"Opened {Path(path).name}")

    def _warn_about_missing_sources(self) -> None:
        missing = [c.name for c in self.state.project.clips if not Path(c.source_path).exists()]
        if missing:
            QMessageBox.warning(
                self,
                "Missing media",
                "These source files are no longer where the project expects them:\n\n"
                + "\n".join(f"  {name}" for name in missing)
                + "\n\nThe project opened, but exporting will fail until they are restored.",
            )

    def save_project(self) -> bool:
        if self.state.document.path is None:
            return self.save_project_as()
        try:
            self.state.document.save()
        except OSError as exc:
            QMessageBox.critical(self, "Could not save", str(exc))
            return False
        self.statusBar().showMessage(f"Saved {Path(self.state.document.path).name}")
        return True

    def save_project_as(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save project", str(Path(self._last_dir()) / "project.sve.json"), PROJECT_FILTER
        )
        if not path:
            return False
        if not path.endswith(".json"):
            path += ".sve.json"
        try:
            self.state.document.save(path)
        except OSError as exc:
            QMessageBox.critical(self, "Could not save", str(exc))
            return False
        self._remember_dir(path)
        self._update_title()
        self.statusBar().showMessage(f"Saved {Path(path).name}")
        return True

    # -- import --------------------------------------------------------------

    def import_media(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add media", self._last_dir(), MEDIA_FILTER)
        if not paths:
            return
        self._remember_dir(paths[0])

        notes: list[str] = []
        failures: list[str] = []
        added_ids: list[str] = []

        with self.state.document.edit(f"Add {len(paths)} file(s)"):
            for path in paths:
                try:
                    report = build_clip(path, image_duration=DEFAULT_IMAGE_DURATION)
                except ProbeError as exc:
                    failures.append(str(exc))
                    continue
                notes += apply_first_import_defaults(self.state.project, report)
                notes += report.notes
                self.state.project.clips.append(report.clip)
                added_ids.append(report.clip.id)

        if added_ids:
            self.state.set_current_clip(added_ids[0])
            self.statusBar().showMessage(f"Added {len(added_ids)} clip(s).")
        if notes:
            QMessageBox.information(self, "Import notes", "\n\n".join(notes))
        if failures:
            QMessageBox.warning(self, "Some files could not be added", "\n\n".join(failures))

    # -- editing -------------------------------------------------------------

    def undo(self) -> None:
        if self.state.document.undo():
            self.statusBar().showMessage("Undo")
        self._update_actions()

    def redo(self) -> None:
        if self.state.document.redo():
            self.statusBar().showMessage("Redo")
        self._update_actions()

    def delete_selection(self) -> None:
        """Delete the selected shape, or the selected clip if none is."""
        shape = self.state.current_shape
        clip = self.state.current_clip
        if shape is not None and clip is not None:
            with self.state.document.edit(f"Delete {shape.type}"):
                clip.shapes = [s for s in clip.shapes if s.id != shape.id]
            self.state.select_shape(None)
            self.state.notify_shapes_changed()
            self.statusBar().showMessage("Deleted shape.")
        elif clip is not None:
            self.timeline.remove_current()

    # -- export --------------------------------------------------------------

    def _export_preconditions(self) -> bool:
        project = self.state.project
        if not project.clips:
            QMessageBox.information(self, "Nothing to export", "Add at least one clip first.")
            return False
        missing = [c.name for c in project.clips if not Path(c.source_path).exists()]
        if missing:
            QMessageBox.critical(
                self, "Missing media",
                "These source files are missing:\n\n" + "\n".join(missing),
            )
            return False
        return True

    def export_video(self) -> None:
        if not self._export_preconditions():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export video", str(Path(self._last_dir()) / "export.mp4"), EXPORT_FILTER
        )
        if not path:
            return
        if not path.lower().endswith(".mp4"):
            path += ".mp4"
        self._remember_dir(path)
        self._run_export(Path(path), title="Exporting video")

    def preview_timeline(self) -> None:
        """Render the real export pipeline to a temp file and play it.

        Deliberately not a second playback engine: the honest answer to "how
        does the whole thing look" is the thing that will actually be
        delivered, just smaller and faster.
        """
        if not self._export_preconditions():
            return
        from ..cache import cache_dir

        target = cache_dir("preview") / "timeline_preview.mp4"
        dialog = self._run_export(target, title="Rendering timeline preview",
                                  encode=PREVIEW_ENCODE)
        if dialog is not None and dialog.result_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(dialog.result_path))

    def _run_export(self, path: Path, *, title: str, encode=None) -> ExportDialog | None:
        self.preview.stop()
        dialog = ExportDialog(self.state.project, path, self, title=title, encode=encode)
        dialog.exec()

        if dialog.result_path:
            self.statusBar().showMessage(f"Wrote {Path(dialog.result_path).name}")
            return dialog
        if dialog.was_cancelled:
            self.statusBar().showMessage("Export cancelled.")
            return None
        if dialog.error:
            message, log = dialog.error
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("Export failed")
            box.setText(message)
            if log:
                box.setDetailedText(log)
            box.exec()
        return None

    def clear_cache(self) -> None:
        from ..cache import cache_size_bytes, clear_cache

        size = cache_size_bytes() / (1024 * 1024)
        answer = QMessageBox.question(
            self, "Clear cache",
            f"Delete {size:.1f} MB of normalized media and thumbnails?\n\n"
            "Projects are unaffected; the files are rebuilt on the next export.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            clear_cache()
            self.statusBar().showMessage("Cache cleared.")

    # -- chrome --------------------------------------------------------------

    def _update_title(self) -> None:
        name = Path(self.state.document.path).name if self.state.document.path else "Untitled"
        dirty = "*" if self.state.document.dirty else ""
        self.setWindowTitle(f"{dirty}{name} - Simple Video Editor")

    def _update_status_hint(self) -> None:
        """A resting message that reflects the project, not the last action."""
        project = self.state.project
        if not project.clips:
            self.statusBar().showMessage("Add media to begin.")
            return
        minutes, seconds = divmod(project.total_duration, 60.0)
        shapes = sum(len(c.shapes) for c in project.clips)
        self.statusBar().showMessage(
            f"{len(project.clips)} clip(s), {int(minutes)}:{seconds:05.2f} total, "
            f"{shapes} annotation(s)."
        )

    def _update_actions(self) -> None:
        document = self.state.document
        self.act_undo.setEnabled(document.can_undo)
        self.act_redo.setEnabled(document.can_redo)
        self.act_undo.setText(f"Undo {document.undo_label}" if document.can_undo else "Undo")
        self.act_redo.setText(f"Redo {document.redo_label}" if document.can_redo else "Redo")
        has_clips = bool(self.state.project.clips)
        self.act_export.setEnabled(has_clips)
        self.act_preview_timeline.setEnabled(has_clips)
        self.act_delete.setEnabled(
            self.state.current_shape is not None or self.state.current_clip is not None
        )
        self._update_title()

    def _last_dir(self) -> str:
        return str(self.settings.value("last_dir", str(Path.home())))

    def _remember_dir(self, path: str) -> None:
        self.settings.setValue("last_dir", str(Path(path).parent))

    def closeEvent(self, event) -> None:
        if not self._confirm_discard():
            event.ignore()
            return
        self.timeline.thumbnails.shutdown()
        super().closeEvent(event)


__all__ = ["MainWindow"]
