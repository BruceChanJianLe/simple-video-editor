"""Per-clip preview: a stock QMediaPlayer plus the annotation overlay.

Per-clip rather than whole-timeline preview is deliberate. Gapless playback
across several files is genuinely hard and buys nothing here: annotation is a
per-clip activity, and "how does the whole thing look" is answered honestly by
rendering the real export pipeline to a temp file (see
:meth:`sve.ui.main_window.MainWindow.preview_timeline`) rather than by an
approximation that might disagree with the export.

The player plays the *source* file directly and untrimmed; the clip's in/out
points are enforced here as playback bounds. That keeps the player's position
in source time, which is the same clock ``Shape.start``/``end`` use, so the
overlay never has to convert anything.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSizeF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..geometry import video_display_rect
from ..model import Clip
from .overlay import AnnotationItem
from .state import EditorState

# QMediaPlayer emits positionChanged too coarsely to keep a hard-cut overlay
# in sync at the boundary, so the overlay is also repainted on this timer
# while playing. It only triggers a repaint of one item, not a decode.
OVERLAY_TICK_MS = 33

BACKGROUND = QColor(24, 24, 27)


class StillItem(QGraphicsItem):
    """Draws an image clip's picture.

    ``QMediaPlayer`` does not play stills, so image clips get their own item.
    Like the video item it is positioned and sized to the picture rect the
    canvas computes, so it fills its own bounding rect exactly and a still and
    its annotations cannot drift apart.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pixmap = QPixmap()
        self._size = QSizeF()
        self.setZValue(0)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self.prepareGeometryChange()
        self._pixmap = pixmap
        self.update()

    def set_size(self, size: QSizeF) -> None:
        self.prepareGeometryChange()
        self._size = size
        self.update()

    def native_size(self) -> QSizeF:
        return QSizeF(self._pixmap.size())

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._size.width(), self._size.height())

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rect = self.boundingRect()
        if self._pixmap.isNull() or rect.isEmpty():
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(rect, self._pixmap, QRectF(self._pixmap.rect()))


class VideoCanvas(QGraphicsView):
    """Scene holding the video item with the annotation item above it."""

    def __init__(self, state: EditorState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setBackgroundBrush(BACKGROUND)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 180)

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self.video_item = QGraphicsVideoItem()
        self.video_item.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self._scene.addItem(self.video_item)

        self.still_item = StillItem()
        self.still_item.setVisible(False)
        self._scene.addItem(self.still_item)

        self.annotations = AnnotationItem(state)
        self._scene.addItem(self.annotations)

        self.video_item.nativeSizeChanged.connect(self._relayout)
        state.currentClipChanged.connect(lambda *_: self._relayout())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    def native_size(self) -> QSizeF:
        """The current clip's display resolution.

        The probed size is preferred over the player's ``nativeSize``: it is
        already rotation-corrected, it is known before the first frame
        arrives, and it does not lag a clip behind when the selection changes,
        which would place every shape wrongly for one repaint.
        """
        clip = self.state.current_clip
        if clip is not None and clip.info.width > 0 and clip.info.height > 0:
            return QSizeF(clip.info.width, clip.info.height)
        native = self.video_item.nativeSize()
        if native.isValid() and native.width() > 0 and native.height() > 0:
            return QSizeF(native)
        output = self.state.project.output
        return QSizeF(output.width, output.height)

    def picture_rect(self) -> QRectF:
        """Where the video picture goes inside the viewport."""
        return video_display_rect(QSizeF(self.viewport().size()), self.native_size())

    def _relayout(self, *_args) -> None:
        """Place the picture, then put every layer in exactly that rect.

        The aspect fit is computed here, once, and the video item, the still
        item and the annotation overlay are all *positioned* at the resulting
        rect and sized to it. None of them does any fitting of its own.

        This is the fix for a real bug rather than a stylistic preference.
        Sizing ``QGraphicsVideoItem`` to the whole viewport and letting it fit
        internally leaves it centring the picture inside that size while
        reporting an already-fitted ``boundingRect``; anything that recomputes
        the picture rect from that bounding rect loses the centring offset and
        draws every annotation one letterbox bar out of place.
        """
        viewport = QSizeF(self.viewport().size())
        rect = self.picture_rect()
        self._scene.setSceneRect(0, 0, viewport.width(), viewport.height())

        if rect.isEmpty():
            self.annotations.set_picture_size(QSizeF())
            return

        self.video_item.setSize(rect.size())
        self.video_item.setPos(rect.topLeft())
        self.still_item.set_size(rect.size())
        self.still_item.setPos(rect.topLeft())
        self.annotations.setPos(rect.topLeft())
        self.annotations.set_picture_size(rect.size())
        self.annotations.update()

    def show_still(self, path: str) -> bool:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return False
        self.still_item.set_pixmap(pixmap)
        self.still_item.setVisible(True)
        self.video_item.setVisible(False)
        self._relayout()
        return True

    def show_video(self) -> None:
        self.still_item.setVisible(False)
        self.still_item.set_pixmap(QPixmap())
        self.video_item.setVisible(True)
        self._relayout()

    def refresh(self) -> None:
        self.annotations.update()


class PreviewPane(QWidget):
    """Video canvas plus transport controls."""

    playbackStateChanged = Signal(bool)

    def __init__(self, state: EditorState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self._clip: Clip | None = None
        self._loaded_path: str | None = None
        self._scrub_active = False
        self._suppress_slider = False
        # Set when a new source is loaded, cleared once it has been positioned.
        # See _on_media_status.
        self._needs_initial_seek = False

        self.audio = QAudioOutput()
        self.player = QMediaPlayer()
        self.player.setAudioOutput(self.audio)

        self.canvas = VideoCanvas(state, self)
        self.player.setVideoOutput(self.canvas.video_item)

        self._build_transport()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(self.transport, 0)

        self._tick = QTimer(self)
        self._tick.setInterval(OVERLAY_TICK_MS)
        self._tick.timeout.connect(self._on_tick)

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_playback_state)
        self.player.errorOccurred.connect(self._on_error)
        self.player.mediaStatusChanged.connect(self._on_media_status)

        state.currentClipChanged.connect(self.load_clip)
        state.shapesChanged.connect(self.canvas.refresh)
        state.selectionChanged.connect(lambda *_: self.canvas.refresh())
        state.toolChanged.connect(lambda *_: self.canvas.refresh())
        state.projectChanged.connect(self._on_project_changed)

        self._update_enabled()

    # -- construction --------------------------------------------------------

    def _build_transport(self) -> None:
        self.transport = QWidget(self)
        self.transport.setObjectName("transport")

        self.play_button = QPushButton("Play")
        self.play_button.setFixedWidth(72)
        self.play_button.clicked.connect(self.toggle_play)

        self.back_button = QPushButton("<")
        self.back_button.setFixedWidth(32)
        self.back_button.setToolTip("Step back one frame (Left)")
        self.back_button.clicked.connect(lambda: self.step_frames(-1))

        self.forward_button = QPushButton(">")
        self.forward_button.setFixedWidth(32)
        self.forward_button.setToolTip("Step forward one frame (Right)")
        self.forward_button.clicked.connect(lambda: self.step_frames(1))

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.sliderPressed.connect(self._on_scrub_start)
        self.slider.sliderReleased.connect(self._on_scrub_end)
        self.slider.valueChanged.connect(self._on_scrub_value)

        self.time_label = QLabel("0:00.00 / 0:00.00")
        self.time_label.setMinimumWidth(140)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        row = QHBoxLayout(self.transport)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(6)
        for widget in (self.play_button, self.back_button, self.forward_button):
            row.addWidget(widget)
        row.addWidget(self.slider, 1)
        row.addWidget(self.time_label)

    # -- clip loading --------------------------------------------------------

    def load_clip(self, clip: Clip | None) -> None:
        self._clip = clip
        if clip is None:
            self.player.stop()
            self.player.setSource(QUrl())
            self._apply_playback_rate()
            self._loaded_path = None
            self.canvas.show_video()
            self._update_enabled()
            self.canvas.refresh()
            return

        if clip.kind == "image":
            self.player.stop()
            self.player.setSource(QUrl())
            self._apply_playback_rate()
            self._loaded_path = None
            if not self.canvas.show_still(clip.source_path):
                self.state.status(f"Could not read image {clip.name}")
        else:
            self.canvas.show_video()
            if clip.source_path != self._loaded_path:
                self._loaded_path = clip.source_path
                self._needs_initial_seek = True
                self.player.setSource(QUrl.fromLocalFile(clip.source_path))
            self._apply_playback_rate()
            # Pause *after* the source is set. Pausing a player that has no
            # source leaves it in StoppedState, and a stopped player never
            # presents a frame - the preview stays black until you press play,
            # which looks exactly like a broken video output.
            self.player.pause()

        self.seek(clip.in_point)
        self._update_enabled()
        self._update_labels()
        self.canvas.refresh()

    def _on_project_changed(self) -> None:
        # in/out points or speed may have moved under us (undo, trim edit).
        self._apply_playback_rate()
        self._update_labels()
        self.canvas.refresh()

    def _apply_playback_rate(self) -> None:
        """Match the player's rate to the speed in force at the playhead.

        The player's *position* stays in source time regardless of rate, so
        the overlay's source-time comparison and the out-point stop are
        unaffected; only the wall-clock pace changes, same as the export.
        Re-checked on every position update, which is what makes playback
        change pace as it crosses a speed-section boundary.
        """
        clip = self._clip
        rate = 1.0
        if clip is not None and clip.kind == "video":
            rate = clip.rate_at(self.state.playhead)
        if abs(self.player.playbackRate() - rate) > 1e-9:
            self.player.setPlaybackRate(rate)

    def _update_enabled(self) -> None:
        clip = self._clip
        playable = clip is not None and clip.kind == "video"
        for widget in (self.play_button, self.back_button, self.forward_button, self.slider):
            widget.setEnabled(clip is not None)
        # A still has nothing to play, but its shapes still have time ranges,
        # so the scrubber stays live to move the playhead across them.
        self.play_button.setEnabled(playable)

    # -- transport -----------------------------------------------------------

    @property
    def is_playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def toggle_play(self) -> None:
        if self._clip is None or self._clip.kind != "video":
            return
        if self.is_playing:
            self.player.pause()
        else:
            # Restarting from the out point would immediately re-pause.
            if self.state.playhead >= self._clip.out_point - 1e-3:
                self.seek(self._clip.in_point)
            self.player.play()

    def stop(self) -> None:
        self.player.pause()

    def seek(self, seconds: float) -> None:
        clip = self._clip
        if clip is not None:
            seconds = min(max(seconds, clip.in_point), clip.out_point)
        self.state.set_playhead(seconds)
        if clip is not None and clip.kind == "video":
            self.player.setPosition(round(seconds * 1000))
        self._apply_playback_rate()
        self._update_labels()
        self.canvas.refresh()

    def frame_step_seconds(self) -> float:
        clip = self._clip
        fps = 0.0
        if clip is not None and clip.info.fps > 0:
            fps = clip.info.fps
        if fps <= 0:
            fps = self.state.project.output.fps or 30.0
        return 1.0 / fps

    def step_frames(self, count: int) -> None:
        if self._clip is None:
            return
        self.player.pause()
        self.seek(self.state.playhead + count * self.frame_step_seconds())

    # -- player signals ------------------------------------------------------

    def _on_position(self, ms: int) -> None:
        if self._scrub_active or self._clip is None:
            return
        seconds = ms / 1000.0
        clip = self._clip
        if self.is_playing and seconds >= clip.out_point:
            # Stop at the out point rather than running into the rest of the
            # source file, which is not part of this clip.
            self.player.pause()
            seconds = clip.out_point
            self.player.setPosition(round(seconds * 1000))
        self.state.set_playhead(seconds)
        self._apply_playback_rate()
        self._update_labels()
        self.canvas.refresh()

    def _on_duration(self, _ms: int) -> None:
        self._update_labels()

    def _on_playback_state(self, playing_state) -> None:
        playing = playing_state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setText("Pause" if playing else "Play")
        if playing:
            self._tick.start()
        else:
            self._tick.stop()
        self.playbackStateChanged.emit(playing)

    def _on_media_status(self, status) -> None:
        """Position the player once its media has finished loading.

        Loading is asynchronous, so the seek issued by :meth:`load_clip` can
        land before the media is ready and be discarded.

        The one-shot guard is essential rather than tidy: ``setPosition`` on
        loaded media re-emits ``mediaStatusChanged(LoadedMedia)`` synchronously,
        so seeking from this handler unguarded recurses until the stack blows.
        """
        if status != QMediaPlayer.MediaStatus.LoadedMedia or self._clip is None:
            return
        if not self._needs_initial_seek:
            return
        self._needs_initial_seek = False

        if self._clip.kind == "video" and not self.is_playing:
            self.player.pause()
        self.seek(max(self.state.playhead, self._clip.in_point))

    def _on_error(self, _error, message: str) -> None:
        if message:
            self.state.status(f"Playback error: {message}")

    def _on_tick(self) -> None:
        """positionChanged is too coarse for a hard cut; repaint on a timer."""
        if self._clip is None:
            return
        self.state.set_playhead(self.player.position() / 1000.0)
        self._apply_playback_rate()
        self.canvas.refresh()
        self._update_labels()

    # -- scrubbing -----------------------------------------------------------

    def _on_scrub_start(self) -> None:
        self._scrub_active = True

    def _on_scrub_end(self) -> None:
        self._scrub_active = False
        self._apply_scrub(self.slider.value())

    def _on_scrub_value(self, value: int) -> None:
        if self._suppress_slider:
            return
        self._apply_scrub(value)

    def _apply_scrub(self, value: int) -> None:
        clip = self._clip
        if clip is None:
            return
        # The slider is linear in *output* time, matching the time label and
        # the exported pacing: with a 2x section in the middle, dragging at a
        # constant rate crosses that content proportionally faster. The seek
        # target it maps to is still a source position.
        self.seek(clip.source_at_output(clip.duration * value / 1000.0))

    def _update_labels(self) -> None:
        clip = self._clip
        if clip is None:
            self.time_label.setText("0:00.00 / 0:00.00")
            return
        # Displayed times are *output* seconds - what this clip contributes to
        # the export - so a 10s span at 2x reads as 5s and counts up at 1s/s.
        elapsed = clip.output_time(self.state.playhead)
        duration = clip.duration
        self.time_label.setText(f"{_fmt(elapsed)} / {_fmt(duration)}")

        self._suppress_slider = True
        if not self._scrub_active:
            self.slider.setValue(round(1000 * elapsed / max(duration, 1e-6)))
        self._suppress_slider = False

    # -- keyboard ------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.toggle_play()
            event.accept()
            return
        if key == Qt.Key.Key_Left:
            self.step_frames(-1)
            event.accept()
            return
        if key == Qt.Key.Key_Right:
            self.step_frames(1)
            event.accept()
            return
        super().keyPressEvent(event)


def _fmt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes, rest = divmod(seconds, 60.0)
    return f"{int(minutes)}:{rest:05.2f}"


__all__ = ["PreviewPane", "VideoCanvas"]
