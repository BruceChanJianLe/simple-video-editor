"""Application entry point."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from .binaries import BinaryNotFound, ffmpeg, ffprobe
from .render import ensure_fonts
from .undo import Document


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setApplicationName("Simple Video Editor")
    app.setOrganizationName("simple-video-editor")

    try:
        ffmpeg()
        ffprobe()
    except BinaryNotFound as exc:
        QMessageBox.critical(None, "Missing ffmpeg", str(exc))
        return 2

    # Register the bundled font before anything draws, so the first render is
    # not done with a system fallback and then silently change later.
    ensure_fonts()

    from .ui.main_window import MainWindow

    document = Document()
    window = MainWindow(document)

    for argument in argv[1:]:
        if argument.endswith(".json"):
            try:
                document.load(argument)
                clips = document.project.clips
                window.state.set_current_clip(clips[0].id if clips else None)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(window, "Could not open project", str(exc))
            break

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
