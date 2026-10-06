"""Per-clip speed: retiming a clip's trimmed span onto the output timeline.

Three properties matter and each gets its own coverage:

1. **Duration arithmetic.** A clip's output duration is its trimmed source
   span divided by its speed, and everything downstream (timeline totals,
   clip offsets, the export ``-t``) follows from that one property.
2. **Annotations stay attached.** Shape times are source time; a shape over
   sped-up content must land at the compressed output moment, not the
   uncompressed one.
3. **The rendered file agrees.** An export of a retimed clip is actually
   shorter or longer, stays in A/V sync (atempo mirrors setpts), and shows a
   shape exactly inside its retimed window.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QImage

from sve.binaries import ffmpeg
from sve.export import (
    EncodeSettings,
    atempo_chain,
    build_plan,
    export_project,
    global_range,
)
from sve.importer import build_clip
from sve.model import (
    SPEED_MAX,
    SPEED_MIN,
    Clip,
    OutputSpec,
    Project,
    Shape,
    SpeedSection,
    clamp_speed,
)
from sve.probe import probe

OUTPUT = OutputSpec(width=640, height=360, fps=30.0, sample_rate=48000)
FAST_ENCODE = EncodeSettings(crf=30, preset="ultrafast", audio_bitrate="96k")


@pytest.fixture(scope="session", autouse=True)
def qt_app(qapp):
    return qapp


# -- model ---------------------------------------------------------------


def test_duration_is_source_span_over_speed():
    clip = Clip(kind="video", source_path="/a.mp4", in_point=1.0, out_point=5.0, speed=2.0)
    assert clip.source_span == 4.0
    assert clip.duration == 2.0
    clip.speed = 0.5
    assert clip.duration == 8.0


def test_speed_round_trips_through_serialization():
    clip = Clip(kind="video", source_path="/a.mp4", in_point=0.0, out_point=2.0, speed=1.75)
    assert Clip.from_dict(clip.to_dict()).speed == 1.75


def test_projects_saved_before_speed_existed_load_at_1x():
    d = Clip(kind="video", source_path="/a.mp4", in_point=0.0, out_point=2.0).to_dict()
    del d["speed"]
    assert Clip.from_dict(d).speed == 1.0


def test_out_of_range_speed_is_clamped_on_load():
    d = Clip(kind="video", source_path="/a.mp4", out_point=2.0, speed=100.0).to_dict()
    assert Clip.from_dict(d).speed == SPEED_MAX
    d["speed"] = 0.01
    assert Clip.from_dict(d).speed == SPEED_MIN
    d["speed"] = -1.0
    assert Clip.from_dict(d).speed == 1.0


def test_an_image_clip_never_carries_a_rate():
    d = Clip(kind="image", source_path="/a.png", out_point=3.0).to_dict()
    d["speed"] = 2.0
    clip = Clip.from_dict(d)
    assert clip.speed == 1.0
    assert clip.duration == 3.0


def test_clip_offsets_use_retimed_durations():
    a = Clip(kind="video", source_path="/a.mp4", in_point=0.0, out_point=4.0, speed=2.0)
    b = Clip(kind="video", source_path="/b.mp4", in_point=0.0, out_point=2.0)
    project = Project(clips=[a, b])
    assert project.clip_offset(b.id) == 2.0
    assert project.total_duration == 4.0


# -- speed sections: piecewise time ----------------------------------------


def _sectioned_clip() -> Clip:
    """0..10s trimmed span at 1x, with 2..4s at 2x and 6..7s at 0.5x.

    Output pieces: 0..2 (2s), 2..4 (1s), 4..6 (2s), 6..7 (2s), 7..10 (3s)
    = 10s of output for 10s of source.
    """
    clip = Clip(kind="video", source_path="/a.mp4", in_point=0.0, out_point=10.0)
    clip.speed_sections = [
        SpeedSection(start=2.0, end=4.0, speed=2.0),
        SpeedSection(start=6.0, end=7.0, speed=0.5),
    ]
    return clip


def test_segments_cover_the_trim_with_base_speed_gaps():
    clip = _sectioned_clip()
    assert clip.speed_segments() == [
        (0.0, 2.0, 1.0), (2.0, 4.0, 2.0), (4.0, 6.0, 1.0),
        (6.0, 7.0, 0.5), (7.0, 10.0, 1.0),
    ]
    assert clip.duration == pytest.approx(10.0)


def test_sections_are_intersected_with_the_trim():
    clip = _sectioned_clip()
    clip.in_point, clip.out_point = 3.0, 6.5
    assert clip.speed_segments() == [
        (3.0, 4.0, 2.0), (4.0, 6.0, 1.0), (6.0, 6.5, 0.5),
    ]
    assert clip.duration == pytest.approx(0.5 + 2.0 + 1.0)


def test_overlapping_sections_first_wins():
    clip = Clip(kind="video", source_path="/a.mp4", in_point=0.0, out_point=10.0)
    clip.speed_sections = [
        SpeedSection(start=1.0, end=5.0, speed=2.0),
        SpeedSection(start=3.0, end=6.0, speed=0.5),
    ]
    assert clip.speed_segments() == [
        (0.0, 1.0, 1.0), (1.0, 5.0, 2.0), (5.0, 6.0, 0.5), (6.0, 10.0, 1.0),
    ]


def test_output_time_is_piecewise_and_inverts():
    clip = _sectioned_clip()
    assert clip.output_time(0.0) == 0.0
    assert clip.output_time(3.0) == pytest.approx(2.5)   # 2s + 1s at 2x
    assert clip.output_time(6.5) == pytest.approx(6.0)   # 5s + 0.5s at 0.5x
    assert clip.output_time(10.0) == pytest.approx(clip.duration)
    for t in (0.0, 1.0, 2.5, 4.0, 6.2, 8.0, 10.0):
        assert clip.source_at_output(clip.output_time(t)) == pytest.approx(t)


def test_rate_at_is_half_open_like_shape_visibility():
    clip = _sectioned_clip()
    assert clip.rate_at(1.9) == 1.0
    assert clip.rate_at(2.0) == 2.0
    assert clip.rate_at(4.0) == 1.0
    assert clip.rate_at(6.0) == 0.5
    assert clip.rate_at(9.0) == 1.0


def test_sections_round_trip_and_sort_through_serialization():
    clip = _sectioned_clip()
    clip.speed_sections.reverse()
    loaded = Clip.from_dict(clip.to_dict())
    assert [(s.start, s.end, s.speed) for s in loaded.speed_sections] == [
        (2.0, 4.0, 2.0), (6.0, 7.0, 0.5),
    ]


def test_sections_combine_with_a_base_speed():
    clip = Clip(kind="video", source_path="/a.mp4", in_point=0.0, out_point=8.0,
                speed=2.0)
    clip.speed_sections = [SpeedSection(start=4.0, end=6.0, speed=0.5)]
    # 0..4 at 2x (2s) + 4..6 at 0.5x (4s) + 6..8 at 2x (1s)
    assert clip.duration == pytest.approx(7.0)
    assert clip.rate_at(1.0) == 2.0
    assert clip.rate_at(5.0) == 0.5


# -- time conversion -----------------------------------------------------


def test_shape_window_is_compressed_by_speed():
    clip = Clip(kind="video", source_path="/a.mp4", in_point=1.0, out_point=5.0, speed=2.0)
    shape = Shape(type="rect", points=[[0, 0], [1, 1]], start=2.0, end=4.0)
    clip.shapes.append(shape)
    project = Project(clips=[clip])
    # Source 2..4 is 1..3s past the in point; at 2x that is 0.5..1.5s out.
    assert global_range(project, clip, shape) == (0.5, 1.5)


def test_shape_window_is_stretched_by_slow_motion():
    clip = Clip(kind="video", source_path="/a.mp4", in_point=0.0, out_point=2.0, speed=0.5)
    shape = Shape(type="rect", points=[[0, 0], [1, 1]], start=1.0, end=2.0)
    clip.shapes.append(shape)
    project = Project(clips=[clip])
    assert global_range(project, clip, shape) == (2.0, 4.0)


def test_a_speed_change_cannot_desynchronise_annotations():
    """The shape's *source* window is invariant under retiming: mapping the
    output window back through the speed always recovers it."""
    clip = Clip(kind="video", source_path="/a.mp4", in_point=1.0, out_point=9.0)
    shape = Shape(type="rect", points=[[0, 0], [1, 1]], start=3.0, end=6.0)
    clip.shapes.append(shape)
    project = Project(clips=[clip])
    for speed in (0.25, 0.5, 1.0, 2.5, 4.0):
        clip.speed = speed
        start, end = global_range(project, clip, shape)
        assert start * speed + clip.in_point == pytest.approx(3.0)
        assert end * speed + clip.in_point == pytest.approx(6.0)


def test_shape_over_a_speed_section_is_compressed_with_it():
    clip = _sectioned_clip()
    shape = Shape(type="rect", points=[[0, 0], [1, 1]], start=2.0, end=4.0)
    clip.shapes.append(shape)
    project = Project(clips=[clip])
    # The 2x section starts 2s of output in and lasts 1s of output.
    assert global_range(project, clip, shape) == (2.0, 3.0)


def test_shape_spanning_a_section_boundary_stays_attached():
    clip = _sectioned_clip()
    shape = Shape(type="rect", points=[[0, 0], [1, 1]], start=1.0, end=3.0)
    clip.shapes.append(shape)
    project = Project(clips=[clip])
    # 1s at 1x, then 1s of source at 2x = 0.5s of output.
    start, end = global_range(project, clip, shape)
    assert start == pytest.approx(1.0)
    assert end == pytest.approx(2.5)


# -- filter graph --------------------------------------------------------


def test_atempo_chain_factors_multiply_to_the_speed():
    import math
    for speed in (SPEED_MIN, 0.3, 0.5, 1.0, 1.3, 2.0, 3.7, SPEED_MAX):
        chain = atempo_chain(speed)
        factors = [float(part.split("=")[1]) for part in chain.split(",")]
        assert all(0.5 <= f <= 2.0 for f in factors), chain
        assert math.prod(factors) == pytest.approx(clamp_speed(speed), abs=1e-5)


def test_graph_is_untouched_at_1x_and_retimed_otherwise(assets_dir, tmp_path):
    report = build_clip(assets_dir / "clip_720p.mp4")
    clip = report.clip
    clip.in_point, clip.out_point = 1.0, 5.0
    project = Project(output=OutputSpec(**OUTPUT.to_dict()), clips=[clip])

    plan = build_plan(project, tmp_path / "a.mp4", tmp_path)
    assert "setpts=PTS-STARTPTS,fps" in plan.filter_graph
    assert "atempo" not in plan.filter_graph

    clip.speed = 2.0
    plan = build_plan(project, tmp_path / "b.mp4", tmp_path)
    assert "setpts=(PTS-STARTPTS)/2.000000,fps" in plan.filter_graph
    assert "atempo=2.000000" in plan.filter_graph
    assert plan.total_duration == pytest.approx(2.0)


def test_a_sectioned_clip_splits_the_input_and_concats_the_pieces(assets_dir, tmp_path):
    report = build_clip(assets_dir / "clip_720p.mp4")
    clip = report.clip
    clip.in_point, clip.out_point = 0.0, 6.0
    clip.speed_sections = [SpeedSection(start=2.0, end=4.0, speed=2.0)]
    project = Project(output=OutputSpec(**OUTPUT.to_dict()), clips=[clip])

    plan = build_plan(project, tmp_path / "a.mp4", tmp_path)
    graph = plan.filter_graph
    assert "split=3" in graph and "asplit=3" in graph
    assert "concat=n=3:v=1:a=1" in graph
    # The 1x pieces keep the plain setpts; only the section is retimed.
    assert graph.count("setpts=PTS-STARTPTS,fps") == 2
    assert "trim=start=2.000000:end=4.000000,setpts=(PTS-STARTPTS)/2.000000" in graph
    assert plan.total_duration == pytest.approx(5.0)


# -- the rendered file ---------------------------------------------------


def frame_at(video: Path, seconds: float, out: Path) -> QImage:
    subprocess.run(
        [ffmpeg(), "-y", "-loglevel", "error", "-ss", f"{seconds:.3f}",
         "-i", str(video), "-frames:v", "1", "-update", "1", str(out)],
        check=True,
    )
    image = QImage(str(out))
    assert not image.isNull(), f"could not read a frame from {video}"
    return image.convertToFormat(QImage.Format.Format_RGB32)


@pytest.mark.parametrize("speed,expected", [(2.0, 2.0), (0.5, 8.0)])
def test_export_duration_is_retimed(assets_dir, tmp_path, speed, expected):
    report = build_clip(assets_dir / "clip_720p.mp4")
    clip = report.clip
    clip.in_point, clip.out_point = 1.0, 5.0
    clip.speed = speed
    project = Project(output=OutputSpec(**OUTPUT.to_dict()), clips=[clip])

    out = tmp_path / f"speed_{speed}.mp4"
    export_project(project, out, encode=FAST_ENCODE)

    info = probe(out).info
    assert info.duration == pytest.approx(expected, abs=0.15)
    assert info.has_audio


def test_shape_appears_inside_its_retimed_window_only(assets_dir, tmp_path):
    """A shape over source 2..4s of a 2x clip must be on screen for output
    0.5..1.5s and gone after - visible proof that video retiming and the
    overlay's enable window moved together."""
    report = build_clip(assets_dir / "clip_720p.mp4")
    clip = report.clip
    clip.in_point, clip.out_point = 1.0, 5.0
    clip.speed = 2.0
    clip.shapes.append(Shape(
        type="rect", points=[[0.3, 0.3], [0.7, 0.7]], start=2.0, end=4.0,
        stroke_color="#ff0000ff", fill_color="#ff0000ff", stroke_width=0.01,
    ))
    project = Project(output=OutputSpec(**OUTPUT.to_dict()), clips=[clip])

    out = tmp_path / "windowed.mp4"
    export_project(project, out, encode=FAST_ENCODE)

    def center_is_red(t: float) -> bool:
        image = frame_at(out, t, tmp_path / f"f{t}.png")
        color = QColor(image.pixel(image.width() // 2, image.height() // 2))
        return color.red() > 180 and color.green() < 80 and color.blue() < 80

    assert not center_is_red(0.2)   # before the window
    assert center_is_red(1.0)       # inside it
    assert not center_is_red(1.8)   # after it


def test_sectioned_export_duration_and_shape_window(assets_dir, tmp_path):
    """6s of source with 2..4s at 2x exports as 5s, and a shape over exactly
    that section is on screen for output 2..3s only."""
    report = build_clip(assets_dir / "clip_720p.mp4")
    clip = report.clip
    clip.in_point, clip.out_point = 0.0, 6.0
    clip.speed_sections = [SpeedSection(start=2.0, end=4.0, speed=2.0)]
    clip.shapes.append(Shape(
        type="rect", points=[[0.3, 0.3], [0.7, 0.7]], start=2.0, end=4.0,
        stroke_color="#ff0000ff", fill_color="#ff0000ff", stroke_width=0.01,
    ))
    project = Project(output=OutputSpec(**OUTPUT.to_dict()), clips=[clip])

    out = tmp_path / "sectioned.mp4"
    export_project(project, out, encode=FAST_ENCODE)

    info = probe(out).info
    assert info.duration == pytest.approx(5.0, abs=0.15)
    assert info.has_audio

    def center_is_red(t: float) -> bool:
        image = frame_at(out, t, tmp_path / f"s{t}.png")
        color = QColor(image.pixel(image.width() // 2, image.height() // 2))
        return color.red() > 180 and color.green() < 80 and color.blue() < 80

    assert not center_is_red(1.5)   # before the section
    assert center_is_red(2.5)       # inside the compressed window
    assert not center_is_red(3.5)   # after it (1x again, shape gone)


# -- preview -------------------------------------------------------------


def test_preview_player_rate_follows_the_clip(assets_dir):
    from sve.ui.preview import PreviewPane
    from sve.ui.state import EditorState
    from sve.undo import Document

    document = Document()
    state = EditorState(document)
    pane = PreviewPane(state)

    report = build_clip(assets_dir / "clip_720p.mp4")
    clip = report.clip
    clip.speed = 2.0
    document.project.clips.append(clip)

    pane.load_clip(clip)
    assert pane.player.playbackRate() == pytest.approx(2.0)

    # An undo or inspector edit changes speed without reloading the clip.
    clip.speed = 0.5
    pane._on_project_changed()
    assert pane.player.playbackRate() == pytest.approx(0.5)

    pane.load_clip(None)
    assert pane.player.playbackRate() == pytest.approx(1.0)


def test_preview_rate_switches_across_section_boundaries(assets_dir):
    from sve.ui.preview import PreviewPane
    from sve.ui.state import EditorState
    from sve.undo import Document

    document = Document()
    state = EditorState(document)
    pane = PreviewPane(state)

    report = build_clip(assets_dir / "clip_720p.mp4")
    clip = report.clip
    clip.in_point, clip.out_point = 0.0, 6.0
    clip.speed_sections = [SpeedSection(start=2.0, end=4.0, speed=2.0)]
    document.project.clips.append(clip)

    pane.load_clip(clip)
    assert pane.player.playbackRate() == pytest.approx(1.0)
    pane.seek(3.0)
    assert pane.player.playbackRate() == pytest.approx(2.0)
    pane.seek(5.0)
    assert pane.player.playbackRate() == pytest.approx(1.0)

    # The label and scrubber run in output time: 3s of source is 2.5s out
    # (2s at 1x, then 1s at 2x), of a 5s total.
    pane.seek(3.0)
    assert pane.time_label.text().startswith("0:02.50 / 0:05.00")
    assert pane.slider.value() == 500
