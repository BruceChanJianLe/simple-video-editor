"""Command line entry points used for development and for the milestone checks.

``python -m sve.cli probe FILE...``      print ffprobe results
``python -m sve.cli export PROJ OUT``    render a project without the GUI
``python -m sve.cli frame PROJ T PNG``   rasterize one exported frame
"""

from __future__ import annotations

import argparse
import sys

from .ffmpeg_run import Cancelled, FFmpegError


def cmd_probe(args: argparse.Namespace) -> int:
    from .probe import ProbeError, format_probe, probe

    status = 0
    for path in args.files:
        try:
            result = probe(path)
        except ProbeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            status = 1
            continue
        print(format_probe(path, result))
        if args.verbose:
            i = result.info
            for key, value in i.to_dict().items():
                print(f"    {key:14} {value}")
    return status


def _ensure_qt_app():
    """A QGuiApplication is required to rasterize shapes with QPainter.

    Headless by default so that `sve export` works over SSH and in CI; an
    existing DISPLAY is left alone in case someone is debugging visually.
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.instance() or QGuiApplication([])


def cmd_export(args: argparse.Namespace) -> int:
    from .export import export_project
    from .model import Project

    _ensure_qt_app()
    project = Project.load(args.project)

    last = -1

    def progress(fraction: float) -> None:
        nonlocal last
        percent = int(fraction * 100)
        if percent != last:
            last = percent
            print(f"\r{percent:3d}%", end="", flush=True)

    export_project(
        project,
        args.output,
        on_progress=progress,
        on_status=lambda m: print(f"\r{m:<40}", end="", flush=True),
    )
    print(f"\rWrote {args.output}" + " " * 30)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Print the filter graph without running it. Useful when a graph is
    rejected and the message alone is not enough to see why."""
    import tempfile
    from pathlib import Path

    from .export import build_plan, plan_to_args
    from .model import Project

    _ensure_qt_app()
    project = Project.load(args.project)
    with tempfile.TemporaryDirectory() as work:
        plan = build_plan(project, Path("out.mp4"), Path(work))
        print("inputs:")
        for entry in plan.inputs:
            print("   ", " ".join(entry))
        print("\nfilter graph:")
        for step in plan.filter_graph.split(";"):
            print("   ", step)
        print(f"\ntotal duration: {plan.total_duration:.3f}s")
        print(f"overlays: {len(plan.overlays)}")
        if args.command_line:
            print("\nffmpeg " + " ".join(plan_to_args(plan)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sve")
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="print ffprobe results for media files")
    p_probe.add_argument("files", nargs="+")
    p_probe.add_argument("-v", "--verbose", action="store_true")
    p_probe.set_defaults(func=cmd_probe)

    p_export = sub.add_parser("export", help="render a project to mp4 without the GUI")
    p_export.add_argument("project")
    p_export.add_argument("output")
    p_export.set_defaults(func=cmd_export)

    p_plan = sub.add_parser("plan", help="print the export filter graph")
    p_plan.add_argument("project")
    p_plan.add_argument("--command-line", action="store_true")
    p_plan.set_defaults(func=cmd_plan)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FFmpegError as exc:
        # The tail of ffmpeg's stderr is the only thing that says *why*; never
        # let a bare exit status be the whole error message.
        print(f"\nerror: {exc}", file=sys.stderr)
        if exc.log:
            print(exc.log, file=sys.stderr)
        print("\ncommand: ffmpeg " + " ".join(exc.command[1:]), file=sys.stderr)
        return 1
    except Cancelled:
        print("\ncancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
