"""Export progress dialog.

The export runs on a worker thread so the UI stays responsive and Cancel can
actually be clicked. Progress is real - it comes from ffmpeg's own ``-progress``
output - rather than a timer pretending to know how long the encode will take.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from ..export import EncodeSettings, export_project
from ..ffmpeg_run import Cancelled, CancelToken, FFmpegError
from ..model import Project


class ExportWorker(QObject):
    progress = Signal(float)
    status = Signal(str)
    finished = Signal(str)   # output path on success
    failed = Signal(str, str)  # message, log
    cancelled = Signal()

    def __init__(self, project: Project, output_path: Path, encode: EncodeSettings | None) -> None:
        super().__init__()
        self.project = project
        self.output_path = output_path
        self.encode = encode
        self.token = CancelToken()

    def run(self) -> None:
        try:
            export_project(
                self.project,
                self.output_path,
                on_progress=self.progress.emit,
                on_status=self.status.emit,
                cancel=self.token,
                encode=self.encode,
            )
        except Cancelled:
            self.cancelled.emit()
        except FFmpegError as exc:
            self.failed.emit(str(exc), exc.log)
        except Exception as exc:
            self.failed.emit(str(exc), "")
        else:
            self.finished.emit(str(self.output_path))


class ExportDialog(QDialog):
    """Modal progress dialog owning the export thread."""

    def __init__(
        self,
        project: Project,
        output_path: Path,
        parent: QWidget | None = None,
        *,
        title: str = "Exporting",
        encode: EncodeSettings | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)

        self.result_path: str | None = None
        self.error: tuple[str, str] | None = None
        self.was_cancelled = False

        self.status_label = QLabel("Starting...")
        self.status_label.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.detail = QLabel(str(output_path))
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("color: palette(mid);")

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.rejected.connect(self.request_cancel)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addWidget(self.bar)
        layout.addWidget(self.detail)
        layout.addWidget(self.buttons)

        self.thread = QThread(self)
        self.worker = ExportWorker(project, output_path, encode)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_progress)
        self.worker.status.connect(self.status_label.setText)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(self._on_cancelled)
        self.thread.start()

    def _on_progress(self, fraction: float) -> None:
        self.bar.setValue(int(fraction * 1000))

    def _shutdown(self) -> None:
        self.thread.quit()
        self.thread.wait(5000)

    def _on_finished(self, path: str) -> None:
        self.result_path = path
        self._shutdown()
        self.accept()

    def _on_failed(self, message: str, log: str) -> None:
        self.error = (message, log)
        self._shutdown()
        self.reject()

    def _on_cancelled(self) -> None:
        self.was_cancelled = True
        self._shutdown()
        self.reject()

    def request_cancel(self) -> None:
        self.status_label.setText("Cancelling...")
        self.buttons.setEnabled(False)
        self.worker.token.cancel()

    def closeEvent(self, event) -> None:
        """Closing the window means cancelling, not abandoning a live thread."""
        if self.thread.isRunning():
            self.request_cancel()
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        if self.thread.isRunning():
            self.request_cancel()
            return
        super().reject()


__all__ = ["ExportDialog", "ExportWorker"]
