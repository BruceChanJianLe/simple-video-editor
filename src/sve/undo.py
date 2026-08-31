"""Snapshot-based undo/redo.

A project here is at most a few clips and a few dozen shapes, so a full deep
copy of the document per edit costs microseconds and a few kilobytes. That buys
something a command pattern does not: an undo stack that is structurally
incapable of drifting out of sync with the real state, because it *is* the
state. Do not replace this with inverse commands without a measured reason.

Interactive gestures (dragging a shape, scrubbing a time field) must produce
exactly one undo entry, not one per mouse-move. Wrap them::

    with doc.edit("Move shape"):
        shape.points[0] = [nx, ny]

or, for a gesture spanning several events, ``doc.begin("Move shape")`` on press
and ``doc.commit()`` on release.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from .model import Project

MAX_UNDO = 100


class Document:
    """Owns the live :class:`Project` and its undo history."""

    def __init__(self, project: Project | None = None) -> None:
        self.project: Project = project if project is not None else Project()
        self.path: str | None = None
        self._undo: list[tuple[str, Project]] = []
        self._redo: list[tuple[str, Project]] = []
        self._pending: tuple[str, Project] | None = None
        self._clean_marker: str | None = self._fingerprint()
        self._listeners: list[Callable[[], None]] = []

    # -- change notification -------------------------------------------------

    def add_listener(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def notify(self) -> None:
        for fn in list(self._listeners):
            fn()

    # -- editing -------------------------------------------------------------

    def begin(self, label: str) -> None:
        """Open an edit transaction. Nested calls join the outermost one."""
        if self._pending is None:
            self._pending = (label, self.project.copy())

    def commit(self, *, notify: bool = True) -> None:
        """Close the open transaction, pushing its before-state onto undo."""
        if self._pending is None:
            return
        self._undo.append(self._pending)
        del self._undo[:-MAX_UNDO]
        self._redo.clear()
        self._pending = None
        if notify:
            self.notify()

    def rollback(self) -> None:
        """Abandon the open transaction, restoring the before-state."""
        if self._pending is None:
            return
        self.project = self._pending[1]
        self._pending = None
        self.notify()

    @contextmanager
    def edit(self, label: str) -> Iterator[Project]:
        outermost = self._pending is None
        self.begin(label)
        try:
            yield self.project
        except Exception:
            if outermost:
                self.rollback()
            raise
        if outermost:
            self.commit()

    # -- history -------------------------------------------------------------

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> str | None:
        return self._undo[-1][0] if self._undo else None

    @property
    def redo_label(self) -> str | None:
        return self._redo[-1][0] if self._redo else None

    def undo(self) -> bool:
        if not self._undo:
            return False
        label, before = self._undo.pop()
        self._redo.append((label, self.project))
        self.project = before
        self.notify()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        label, after = self._redo.pop()
        self._undo.append((label, self.project))
        self.project = after
        self.notify()
        return True

    # -- save state ----------------------------------------------------------

    def _fingerprint(self) -> str:
        return self.project.to_json()

    @property
    def dirty(self) -> bool:
        return self._fingerprint() != self._clean_marker

    def mark_clean(self) -> None:
        self._clean_marker = self._fingerprint()

    def save(self, path: str | None = None) -> None:
        target = path or self.path
        if target is None:
            raise ValueError("no path to save to")
        self.project.save(target)
        self.path = target
        self.mark_clean()
        self.notify()

    def load(self, path: str) -> None:
        self.project = Project.load(path)
        self.path = path
        self._undo.clear()
        self._redo.clear()
        self._pending = None
        self.mark_clean()
        self.notify()

    def reset(self) -> None:
        self.project = Project()
        self.path = None
        self._undo.clear()
        self._redo.clear()
        self._pending = None
        self.mark_clean()
        self.notify()


__all__ = ["MAX_UNDO", "Document"]
