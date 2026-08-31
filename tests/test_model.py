from __future__ import annotations

import json

import pytest

from sve.model import Clip, OutputSpec, Project, Shape, SourceInfo


def make_project() -> Project:
    return Project(
        output=OutputSpec(width=1280, height=720, fps=30.0),
        clips=[
            Clip(
                kind="video", source_path="/a.mp4", in_point=1.0, out_point=4.0,
                info=SourceInfo(duration=10.0, width=1280, height=720, fps=30.0),
                shapes=[Shape(type="rect", points=[[0.1, 0.1], [0.5, 0.5]], start=1.5, end=3.0)],
            ),
            Clip(kind="image", source_path="/b.png", in_point=0.0, out_point=3.0),
        ],
    )


def test_roundtrip_is_lossless():
    project = make_project()
    again = Project.from_json(project.to_json())
    assert again.to_dict() == project.to_dict()


def test_save_load(tmp_path):
    project = make_project()
    path = tmp_path / "p.sve.json"
    project.save(path)
    assert Project.load(path).to_dict() == project.to_dict()
    # Saved form is real, readable JSON, not a pickle.
    assert json.loads(path.read_text())["output"]["width"] == 1280


def test_save_is_atomic(tmp_path):
    path = tmp_path / "p.json"
    make_project().save(path)
    assert not list(tmp_path.glob("*.tmp"))


def test_clip_offsets_follow_order():
    project = make_project()
    first, second = project.clips
    assert project.clip_offset(first.id) == 0.0
    assert project.clip_offset(second.id) == 3.0
    assert project.total_duration == 6.0

    project.clips.reverse()
    assert project.clip_offset(second.id) == 0.0
    assert project.clip_offset(first.id) == 3.0


def test_shape_times_survive_retrim():
    """Rule 2: shape times are source-relative, so retrimming does not move
    an annotation off the content it annotates."""
    project = make_project()
    clip = project.clips[0]
    shape = clip.shapes[0]
    before = (shape.start, shape.end)

    clip.in_point = 0.5
    clip.out_point = 5.0

    assert (shape.start, shape.end) == before
    # ...but its position on the output timeline shifts by the retrim amount.
    assert project.clip_offset(clip.id) == 0.0


def test_visible_at_is_half_open():
    shape = Shape(type="rect", points=[[0, 0], [1, 1]], start=2.0, end=4.0)
    assert not shape.visible_at(1.999)
    assert shape.visible_at(2.0)
    assert shape.visible_at(3.999)
    assert not shape.visible_at(4.0)


def test_rejects_newer_file_version():
    with pytest.raises(ValueError, match="newer version"):
        Project.from_dict({"version": 999, "clips": []})


def test_ids_are_unique():
    a = Shape(type="rect", points=[[0, 0], [1, 1]])
    b = Shape(type="rect", points=[[0, 0], [1, 1]])
    assert a.id != b.id
