from __future__ import annotations

import pytest

from sve.probe import ProbeError, probe


def test_plain_video(assets_dir):
    result = probe(assets_dir / "clip_720p.mp4")
    assert result.kind == "video"
    assert (result.info.width, result.info.height) == (1280, 720)
    assert result.info.fps == pytest.approx(30.0, abs=0.01)
    assert result.info.duration == pytest.approx(6.0, abs=0.1)
    assert result.info.has_audio
    assert not result.is_vfr
    assert result.info.rotation == 0


def test_missing_audio_is_detected(assets_dir):
    result = probe(assets_dir / "clip_silent_640x480.mp4")
    assert not result.info.has_audio
    assert (result.info.width, result.info.height) == (640, 480)


def test_rotation_swaps_display_dimensions(assets_dir):
    """A 1920x1080 stream with a quarter-turn display matrix is a portrait
    video. Every coordinate downstream depends on reporting it that way."""
    result = probe(assets_dir / "clip_rot90.mp4")
    assert result.info.rotation in (90, 270)
    assert (result.info.coded_width, result.info.coded_height) == (1920, 1080)
    assert (result.info.width, result.info.height) == (1080, 1920)


def test_variable_frame_rate_is_flagged(assets_dir):
    result = probe(assets_dir / "clip_vfr.mp4")
    assert result.is_vfr
    # avg_frame_rate is the honest number; r_frame_rate is an upper bound.
    assert result.info.fps < result.r_fps


def test_still_image(assets_dir):
    result = probe(assets_dir / "still_1600x900.png")
    assert result.kind == "image"
    assert (result.info.width, result.info.height) == (1600, 900)
    assert result.info.duration == 0.0
    assert not result.info.has_audio


def test_missing_file(tmp_path):
    with pytest.raises(ProbeError, match="No such file"):
        probe(tmp_path / "nope.mp4")


def test_non_media_file(tmp_path):
    junk = tmp_path / "notes.txt"
    junk.write_text("hello")
    with pytest.raises(ProbeError):
        probe(junk)
