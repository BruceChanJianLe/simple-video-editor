from __future__ import annotations

from sve.model import Clip, Project, Shape
from sve.undo import Document


def doc_with_clip() -> Document:
    project = Project(clips=[Clip(kind="video", source_path="/a.mp4", out_point=5.0)])
    return Document(project)


def test_edit_creates_one_undo_entry():
    doc = doc_with_clip()
    with doc.edit("Add shape"):
        doc.project.clips[0].shapes.append(Shape(type="rect", points=[[0, 0], [1, 1]]))

    assert doc.can_undo and doc.undo_label == "Add shape"
    assert len(doc.project.clips[0].shapes) == 1
    doc.undo()
    assert doc.project.clips[0].shapes == []
    doc.redo()
    assert len(doc.project.clips[0].shapes) == 1


def test_nested_edits_coalesce_into_one_entry():
    doc = doc_with_clip()
    with doc.edit("Outer"):
        doc.project.clips[0].out_point = 6.0
        with doc.edit("Inner"):
            doc.project.clips[0].out_point = 7.0

    assert doc.undo_label == "Outer"
    doc.undo()
    assert doc.project.clips[0].out_point == 5.0
    assert not doc.can_undo


def test_gesture_begin_commit_is_one_entry():
    """A drag emits many mutations but must undo as a single action."""
    doc = doc_with_clip()
    shape = Shape(type="rect", points=[[0.0, 0.0], [0.5, 0.5]])
    doc.project.clips[0].shapes.append(shape)
    doc.mark_clean()

    doc.begin("Move shape")
    for step in range(1, 11):
        doc.project.clips[0].shapes[0].points[0] = [step / 100, step / 100]
    doc.commit()

    assert len(doc._undo) == 1
    doc.undo()
    assert doc.project.clips[0].shapes[0].points[0] == [0.0, 0.0]


def test_exception_rolls_back():
    doc = doc_with_clip()
    try:
        with doc.edit("Boom"):
            doc.project.clips[0].out_point = 99.0
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert doc.project.clips[0].out_point == 5.0
    assert not doc.can_undo


def test_new_edit_clears_redo():
    doc = doc_with_clip()
    with doc.edit("A"):
        doc.project.clips[0].out_point = 6.0
    doc.undo()
    assert doc.can_redo
    with doc.edit("B"):
        doc.project.clips[0].out_point = 7.0
    assert not doc.can_redo


def test_dirty_tracking(tmp_path):
    doc = doc_with_clip()
    assert not doc.dirty
    with doc.edit("A"):
        doc.project.clips[0].out_point = 6.0
    assert doc.dirty
    doc.save(str(tmp_path / "p.json"))
    assert not doc.dirty
    doc.undo()
    assert doc.dirty


def test_undo_snapshots_are_isolated():
    """The snapshot must be a deep copy, or undo restores the mutated object."""
    doc = doc_with_clip()
    doc.project.clips[0].shapes.append(Shape(type="rect", points=[[0.0, 0.0], [1.0, 1.0]]))
    doc.mark_clean()
    with doc.edit("Nudge"):
        doc.project.clips[0].shapes[0].points[1][0] = 0.25
    doc.undo()
    assert doc.project.clips[0].shapes[0].points[1][0] == 1.0


def test_history_is_bounded():
    from sve.undo import MAX_UNDO
    doc = doc_with_clip()
    for i in range(MAX_UNDO + 25):
        with doc.edit(f"edit {i}"):
            doc.project.clips[0].out_point = float(i)
    assert len(doc._undo) == MAX_UNDO
