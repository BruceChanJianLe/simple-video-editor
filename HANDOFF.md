# Handoff: verifying on macOS

Everything below was built and verified on **Linux**. The macOS path is
unverified. This file is the checklist for closing that gap.

## 1. What this is

A cross-platform desktop app for annotating and assembling short videos:
arrows/boxes/ellipses/text with hard-cut start and end times, over a timeline of
clips and stills, exported to one mp4 via ffmpeg.

- `README.md` - build, run, usage, licensing
- `ARCHITECTURE.md` - **read this before changing preview or export**
- `src/sve/render.py` - the shared renderer; the one place a shape is drawn
- `tests/test_export_matches_preview.py` - the correctness gate
- `tests/test_preview_alignment.py` - the letterbox-coordinate gate

Stack: Python 3.12, PySide6 (Qt 6.10), `QMediaPlayer` + `QGraphicsVideoItem`
for preview, `QPainter` for shapes, bundled ffmpeg 7.1 (GPL, libx264) as a
subprocess. All pinned by `flake.lock` (nixpkgs `a7fc11be66bd`).

## 2. Verified on Linux

- 168 tests pass, `ruff check` clean.
- Export matches the preview renderer: mean channel difference < 3/255,
  annotation bounding boxes within 2px, nothing drawn missing.
- Five-clip mixed timeline (video + still + no-audio 4:3 + rotated portrait +
  VFR) concatenates with correct durations, aspect padding and global shape
  timing.
- `nix build .#simple-video-editor` runs standalone with an empty environment.
- AppImage builds (1.15 GB) but was **not executed** - needs user namespaces the
  build container denies. Irrelevant on macOS anyway; AppImage is Linux-only.

## 3. Run it on the Mac

```sh
git clone git@github.com:BruceChanJianLe/simple-video-editor.git
cd simple-video-editor
nix run .                                  # GUI
nix develop -c python -m pytest            # the suite
nix develop -c ruff check .                # lint
```

You do **not** need the AppImage or the `.app` bundle to run it yourself.
Those exist only for shipping to people without Nix.

Expect Nix to fetch a large closure (~3.7 GB into `/nix/store`). Much of that is
clang/llvm that nixpkgs' `shiboken6` drags in as a runtime dependency of
PySide6 - see README "AppImage" for the trace.

## 4. What is most likely to break, in order

1. **Qt Multimedia backend.** On Linux this uses the ffmpeg backend, which is
   what I verified. macOS defaults to **AVFoundation**. This is the highest-risk
   area and it is concentrated in the preview.
   - Check: does video actually appear, and does the scrubber move?
   - `QT_MEDIA_BACKEND=ffmpeg nix run .` forces the backend I tested, as an
     A/B if the default misbehaves.
   - Relevant code: `src/sve/ui/preview.py`, `PreviewPane.load_clip` and
     `_on_media_status`. Two Linux-specific hazards already fixed there and
     worth re-checking on macOS: pausing *before* `setSource` leaves the player
     Stopped so no frame is ever presented, and `setPosition` re-emits
     `LoadedMedia` synchronously (guarded by `_needs_initial_seek`).

2. **Overlay alignment.** Run `pytest tests/test_preview_alignment.py` first -
   it is pure arithmetic and needs no decoder, so it should pass regardless.
   Then eyeball it: put a shape at the frame corners and confirm it hugs the
   picture, not the widget. On a Retina display, watch for a
   `devicePixelRatio` factor - all tests ran at DPR 1.0 and that is genuinely
   untested.

3. **`nix flake check`** explicitly skipped darwin: *"omitted these
   incompatible systems: aarch64-darwin, x86_64-darwin"*. Run
   `nix flake check` on the Mac to find evaluation problems before runtime ones.
   `pkgs.xorg.xorgserver` is already guarded behind `isLinux` in the devShell,
   but nothing else in the flake has been evaluated on darwin.

4. **`tools/with-xvfb.sh` will not work** - it is X11. Screenshot-based checks
   should just run against the real display on macOS; the rest of the suite is
   headless via Qt's `offscreen` platform and is unaffected.

## 5. If you want the distributable .app

`packaging/macos/simple-video-editor.spec` is written but **never executed**.
Build and sign per README "macOS". The parts most likely to need work:

- PyInstaller is not pinned by the flake (`pip install pyinstaller` into a venv).
- Nested binaries (`ffmpeg`, `ffprobe`) must be signed **before** the bundle
  that contains them.
- `packaging/macos/entitlements.plist` disables library validation, which
  PyInstaller and Qt plugin loading need under the hardened runtime.

## 6. Two spec decisions worth knowing

1. **Shape times are in source-file time**, not offsets from `in_point`. The
   build prompt's model comment said the latter, but its own export formula and
   its "retrimming must not desynchronise annotations" requirement both need the
   former. Documented in ARCHITECTURE.md under "Time".

2. **`QVideoWidget` + a transparent child widget does not work on Qt 6** - the
   overlay is simply absent, as a child *and* as a raised sibling. Switched to
   `QGraphicsVideoItem` in a `QGraphicsScene`, which is the same stock Qt
   Multimedia sink but composites a sibling item above it correctly. Everything
   else in the specified architecture is unchanged.

## 7. Known limitations

Per-clip preview only (whole-timeline preview renders the real export pipeline
at half resolution). No fades/transitions/motion, by design. Projects reference
media by absolute path. Above 40 shapes the exporter flattens overlays into
per-interval composites.
