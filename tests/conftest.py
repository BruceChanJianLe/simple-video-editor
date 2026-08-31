from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).parent / "assets"


def _ffmpeg() -> str:
    from sve.binaries import ffmpeg
    return ffmpeg()


def _run(*args: str) -> None:
    subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", *args], check=True)


@pytest.fixture(scope="session")
def assets_dir() -> Path:
    """Generate the media fixtures once per session.

    Generated rather than committed: they are a few megabytes, and generating
    them keeps the exact properties each test depends on (rotation metadata,
    VFR timestamps, missing audio track) visible in the test source.
    """
    ASSETS.mkdir(parents=True, exist_ok=True)

    if not (ASSETS / "clip_720p.mp4").exists():
        _run(
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "24",
            "-c:a", "aac", "-shortest", str(ASSETS / "clip_720p.mp4"),
        )

    if not (ASSETS / "clip_silent_640x480.mp4").exists():
        _run(
            "-f", "lavfi", "-i", "smptebars=size=640x480:rate=25:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "24",
            str(ASSETS / "clip_silent_640x480.mp4"),
        )

    if not (ASSETS / "clip_rot90.mp4").exists():
        flat = ASSETS / "_rot_src.mp4"
        _run(
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=330:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26",
            "-c:a", "aac", "-shortest", str(flat),
        )
        _run("-display_rotation", "90", "-i", str(flat), "-c", "copy",
             str(ASSETS / "clip_rot90.mp4"))

    if not (ASSETS / "clip_vfr.mp4").exists():
        # Real variable frame rate: 60fps for the first second, then ~12fps.
        _run(
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=60:duration=4",
            "-vf", "setpts='if(lt(N,60), N/60/TB, (1.0 + (N-60)/12)/TB)'",
            "-fps_mode", "passthrough",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26",
            str(ASSETS / "clip_vfr.mp4"),
        )

    if not (ASSETS / "still_1600x900.png").exists():
        _run("-f", "lavfi", "-i", "testsrc2=size=1600x900", "-frames:v", "1",
             str(ASSETS / "still_1600x900.png"))

    return ASSETS


@pytest.fixture(autouse=True, scope="session")
def _headless() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp(_headless):
    """One QApplication for the whole session.

    It must be a ``QApplication``, not a ``QGuiApplication``: Qt allows only
    one application object per process, and whichever type is constructed
    first wins. A ``QGuiApplication`` created by a rendering test is enough for
    QPainter but cannot host widgets, so a later widget test aborts the
    process rather than raising - which is how a green suite turns into a core
    dump the moment the files run in one session.
    """
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
