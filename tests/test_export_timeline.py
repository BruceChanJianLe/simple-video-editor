"""Milestone 8/9: multi-clip timelines.

The interesting cases are the ones where the inputs disagree with each other -
different resolutions, aspect ratios, frame rates, a clip with no audio track, a
still with no timeline of its own, a portrait clip with rotation metadata. That
is exactly the situation in which the concat *demuxer* with stream copy appears
to work and then produces desynced output, and it is why every clip is
normalized to one spec first.

The other thing under test here is time conversion. A shape's times live in its
source file's clock; on the output timeline it must appear at
``clip_offset + (shape.start - clip.in_point)``. Reordering clips or retrimming
one must move annotations with their clip and leave them attached to the same
content.
"""

from __future__ import annotations

import pytest

from sve.export import (
    build_plan,
    export_project,
    global_range,
    rasterize_overlays,
    visible_shapes_for,
)
from sve.ffmpeg_run import Cancelled, CancelToken
from sve.importer import build_clip
from sve.model import Clip, OutputSpec, Project, Shape
from sve.probe import probe

OUTPUT = OutputSpec(width=960, height=540, fps=25.0, sample_rate=48000)


@pytest.fixture(scope="session", autouse=True)
def qt_app(qapp):
    return qapp


@pytest.fixture(scope="module")
def mixed_project(assets_dir):
    """Five clips that disagree about nearly everything."""
    clips = []
    for name, in_point, out_point in [
        ("clip_720p.mp4", 1.0, 3.0),            # 16:9, has audio
        ("still_1600x900.png", 0.0, 2.0),       # a still, no audio, 16:9
        ("clip_silent_640x480.mp4", 0.0, 1.5),  # 4:3, no audio track
        ("clip_rot90.mp4", 0.5, 2.0),           # portrait via rotation metadata
        ("clip_vfr.mp4", 0.0, 2.0),             # variable frame rate
    ]:
        report = build_clip(assets_dir / name)
        clip = report.clip
        clip.in_point, clip.out_point = in_point, out_point
        clips.append(clip)
    return Project(output=OutputSpec(**OUTPUT.to_dict()), clips=clips)


def probe_output(path):
    return probe(path)


class TestTimeConversion:
    """Pure arithmetic, so it is cheap to cover thoroughly."""

    def test_offset_accounts_for_earlier_clips(self):
        a = Clip(kind="video", source_path="/a.mp4", in_point=0.0, out_point=2.0)
        b = Clip(kind="video", source_path="/b.mp4", in_point=5.0, out_point=8.0)
        shape = Shape(type="rect", points=[[0, 0], [1, 1]], start=6.0, end=7.0)
        b.shapes.append(shape)
        project = Project(clips=[a, b])

        # Clip b starts 2s into the timeline; the shape starts 1s into b.
        assert global_range(project, b, shape) == (3.0, 4.0)

    def test_reordering_moves_annotations_with_their_clip(self):
        a = Clip(kind="video", source_path="/a.mp4", in_point=0.0, out_point=2.0)
        b = Clip(kind="video", source_path="/b.mp4", in_point=5.0, out_point=8.0)
        shape = Shape(type="rect", points=[[0, 0], [1, 1]], start=6.0, end=7.0)
        b.shapes.append(shape)
        project = Project(clips=[a, b])
        assert global_range(project, b, shape) == (3.0, 4.0)

        project.clips = [b, a]
        assert global_range(project, b, shape) == (1.0, 2.0)

    def test_retrimming_keeps_the_shape_on_the_same_content(self):
        """The shape annotates something 1s into the source. Trimming 0.5s more
        off the front must keep it on that content, so it appears 0.5s earlier
        on the output timeline."""
        clip = Clip(kind="video", source_path="/a.mp4", in_point=0.0, out_point=4.0)
        shape = Shape(type="rect", points=[[0, 0], [1, 1]], start=1.0, end=2.0)
        clip.shapes.append(shape)
        project = Project(clips=[clip])
        assert global_range(project, clip, shape) == (1.0, 2.0)

        clip.in_point = 0.5
        assert global_range(project, clip, shape) == (0.5, 1.5)

    def test_shape_is_clipped_to_the_trimmed_span(self):
        """A shape running past the out point must not bleed onto the next
        clip."""
        clip = Clip(kind="video", source_path="/a.mp4", in_point=1.0, out_point=3.0)
        shape = Shape(type="rect", points=[[0, 0], [1, 1]], start=0.0, end=99.0)
        clip.shapes.append(shape)
        project = Project(clips=[clip, Clip(kind="video", source_path="/b.mp4",
                                            in_point=0.0, out_point=2.0)])
        start, end = global_range(project, clip, shape)
        assert (start, end) == (0.0, 2.0)

    def test_shapes_entirely_outside_the_trim_are_dropped(self):
        clip = Clip(kind="video", source_path="/a.mp4", in_point=5.0, out_point=8.0)
        clip.shapes = [
            Shape(type="rect", points=[[0, 0], [1, 1]], start=0.0, end=2.0),   # before
            Shape(type="rect", points=[[0, 0], [1, 1]], start=9.0, end=10.0),  # after
            Shape(type="rect", points=[[0, 0], [1, 1]], start=6.0, end=7.0),   # inside
            Shape(type="rect", points=[[0, 0], [1, 1]], start=6.0, end=6.0),   # empty
        ]
        assert len(visible_shapes_for(clip)) == 1


class TestMixedTimelineExport:
    @pytest.fixture(scope="class")
    def rendered(self, mixed_project, tmp_path_factory):
        out = tmp_path_factory.mktemp("timeline") / "timeline.mp4"
        seen: list[float] = []
        export_project(mixed_project, out, on_progress=seen.append)
        return out, seen

    def test_duration_is_the_sum_of_trimmed_clips(self, rendered, mixed_project):
        out, _ = rendered
        result = probe_output(out)
        expected = mixed_project.total_duration
        assert expected == pytest.approx(2.0 + 2.0 + 1.5 + 1.5 + 2.0)
        assert result.info.duration == pytest.approx(expected, abs=0.15)

    def test_output_matches_the_project_spec(self, rendered):
        out, _ = rendered
        result = probe_output(out)
        assert (result.info.width, result.info.height) == (OUTPUT.width, OUTPUT.height)
        assert result.info.fps == pytest.approx(OUTPUT.fps, abs=0.01)
        assert result.info.pix_fmt == "yuv420p"

    def test_audio_is_continuous_even_though_clips_lack_it(self, rendered):
        """Three of the five sources have no audio track. The output must still
        have exactly one, spanning the whole timeline, or players will desync."""
        out, _ = rendered
        streams = probe_output(out).raw["streams"]
        audio = [s for s in streams if s["codec_type"] == "audio"]
        assert len(audio) == 1
        assert float(audio[0]["duration"]) == pytest.approx(
            probe_output(out).info.duration, abs=0.25
        )

    def test_progress_is_reported_and_completes(self, rendered):
        _, seen = rendered
        assert seen, "no progress was reported"
        assert seen[-1] == pytest.approx(1.0)
        assert all(0.0 <= value <= 1.0 for value in seen)
        assert seen == sorted(seen), "progress went backwards"


class TestPlan:
    def test_every_clip_contributes_one_video_and_one_audio_segment(self, mixed_project, tmp_path):
        plan = build_plan(mixed_project, tmp_path / "out.mp4", tmp_path)
        for index in range(len(mixed_project.clips)):
            assert f"[v{index}]" in plan.filter_graph
            assert f"[a{index}]" in plan.filter_graph
        assert f"concat=n={len(mixed_project.clips)}:v=1:a=1" in plan.filter_graph

    def test_stills_get_synthesised_audio(self, mixed_project, tmp_path):
        plan = build_plan(mixed_project, tmp_path / "out.mp4", tmp_path)
        assert "anullsrc" in plan.filter_graph

    def test_stills_are_looped_for_their_duration(self, mixed_project, tmp_path):
        plan = build_plan(mixed_project, tmp_path / "out.mp4", tmp_path)
        flat = [" ".join(entry) for entry in plan.inputs]
        assert any("-loop 1" in entry and "-t 2.000000" in entry for entry in flat)

    def test_overlays_are_gated_on_global_time(self, assets_dir, tmp_path):
        first = build_clip(assets_dir / "clip_720p.mp4").clip
        first.out_point = 2.0
        second = build_clip(assets_dir / "clip_720p.mp4").clip
        second.in_point, second.out_point = 1.0, 3.0
        second.shapes = [
            Shape(type="rect", points=[[0.2, 0.2], [0.8, 0.8]], start=1.5, end=2.5)
        ]
        project = Project(output=OutputSpec(**OUTPUT.to_dict()), clips=[first, second])

        plan = build_plan(project, tmp_path / "out.mp4", tmp_path)
        # Clip two starts at t=2; the shape starts 0.5s into it.
        assert "between(t,2.500000,3.500000)" in plan.filter_graph

    def test_overlay_offset_is_always_zero(self, assets_dir, tmp_path):
        """Position is baked into the full-frame PNG, so the graph must never
        carry a coordinate. If it does, positioning has escaped the renderer."""
        clip = build_clip(assets_dir / "clip_720p.mp4").clip
        clip.out_point = 3.0
        clip.shapes = [
            Shape(type="arrow", points=[[0.1, 0.9], [0.7, 0.2]], start=0.0, end=2.0),
            Shape(type="text", points=[[0.5, 0.5]], text="hi", start=1.0, end=3.0),
        ]
        project = Project(output=OutputSpec(**OUTPUT.to_dict()), clips=[clip])
        plan = build_plan(project, tmp_path / "out.mp4", tmp_path)
        assert plan.filter_graph.count("overlay=0:0") == 2

    def test_rasterized_overlays_are_full_frame(self, assets_dir, tmp_path):
        from PySide6.QtGui import QImage

        clip = build_clip(assets_dir / "clip_720p.mp4").clip
        clip.out_point = 3.0
        clip.shapes = [Shape(type="rect", points=[[0.1, 0.1], [0.4, 0.4]], start=0.0, end=2.0)]
        project = Project(output=OutputSpec(**OUTPUT.to_dict()), clips=[clip])

        overlays = rasterize_overlays(project, tmp_path)
        assert len(overlays) == 1
        image = QImage(str(overlays[0].image_path))
        assert image.width() == OUTPUT.width and image.height() == OUTPUT.height
        assert image.hasAlphaChannel()

    def test_empty_timeline_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="empty"):
            build_plan(Project(), tmp_path / "out.mp4", tmp_path)

    def test_zero_length_timeline_is_rejected(self, tmp_path):
        clip = Clip(kind="video", source_path="/a.mp4", in_point=1.0, out_point=1.0)
        with pytest.raises(ValueError, match="zero duration"):
            build_plan(Project(clips=[clip]), tmp_path / "out.mp4", tmp_path)


class TestCancellation:
    def test_cancel_stops_the_export_and_leaves_no_output(self, assets_dir, tmp_path):
        clip = build_clip(assets_dir / "clip_720p.mp4").clip
        project = Project(output=OutputSpec(**OUTPUT.to_dict()), clips=[clip])
        out = tmp_path / "cancelled.mp4"

        token = CancelToken()
        token.cancel()  # cancelled before it starts

        with pytest.raises(Cancelled):
            export_project(project, out, cancel=token, on_progress=lambda _f: None)
        assert not out.exists(), "a cancelled export left a partial file behind"

    def test_cancel_midway(self, assets_dir, tmp_path):
        """Cancel once progress is actually flowing, which is the real case."""
        clip = build_clip(assets_dir / "clip_720p.mp4").clip
        project = Project(output=OutputSpec(**OUTPUT.to_dict()), clips=[clip])
        out = tmp_path / "midway.mp4"
        token = CancelToken()

        def progress(_fraction: float) -> None:
            token.cancel()

        with pytest.raises(Cancelled):
            export_project(project, out, cancel=token, on_progress=progress)
        assert not out.exists()


def test_single_clip_timeline_skips_concat(assets_dir, tmp_path):
    """One clip needs no concat filter; it should not appear."""
    clip = build_clip(assets_dir / "clip_720p.mp4").clip
    clip.out_point = 2.0
    project = Project(output=OutputSpec(**OUTPUT.to_dict()), clips=[clip])
    plan = build_plan(project, tmp_path / "out.mp4", tmp_path)
    assert "concat=" not in plan.filter_graph
    assert "[v0]null[base]" in plan.filter_graph


class TestPreviewEncode:
    """The full-timeline preview reuses the export pipeline at a smaller size,
    which is what makes it impossible for it to disagree with the export."""

    def test_preview_is_scaled_but_otherwise_identical(self, assets_dir, tmp_path):
        from sve.export import PREVIEW_ENCODE

        clip = build_clip(assets_dir / "clip_720p.mp4").clip
        clip.out_point = 2.0
        clip.shapes = [
            Shape(type="rect", points=[[0.2, 0.2], [0.8, 0.8]], start=0.0, end=2.0)
        ]
        project = Project(output=OutputSpec(**OUTPUT.to_dict()), clips=[clip])

        out = tmp_path / "preview.mp4"
        export_project(project, out, encode=PREVIEW_ENCODE)
        result = probe(out)

        assert result.info.width == int(OUTPUT.width * PREVIEW_ENCODE.scale)
        assert result.info.height == int(OUTPUT.height * PREVIEW_ENCODE.scale)
        assert result.info.fps == pytest.approx(OUTPUT.fps, abs=0.01)
        assert result.info.duration == pytest.approx(clip.duration, abs=0.15)
        assert result.info.has_audio

    def test_scaled_output_dimensions_stay_even(self, assets_dir, tmp_path):
        """yuv420p subsamples chroma 2x2, so H.264 rejects odd dimensions.
        A scale factor that lands on an odd number must be rounded down."""
        from sve.export import EncodeSettings, build_plan

        clip = build_clip(assets_dir / "clip_720p.mp4").clip
        clip.out_point = 1.0
        # 1918 x 1080 scaled by 0.5 would give 959, which is odd.
        project = Project(
            output=OutputSpec(width=1918, height=1080, fps=25.0), clips=[clip]
        )
        plan = build_plan(project, tmp_path / "o.mp4", tmp_path,
                          EncodeSettings(scale=0.5))
        assert "scale=958:540" in plan.filter_graph
