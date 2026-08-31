# Simple Video Editor

Annotate and assemble short videos. Think **Flameshot, but for video**: you
drop in a few clips and stills, draw arrows and boxes and labels that appear
and disappear at set times, and export one mp4.

It is not a general-purpose NLE, and it deliberately does not try to become
one. See [ARCHITECTURE.md](ARCHITECTURE.md).

Target use case: videos up to about five minutes, assembled from a handful of
video clips and still images.

## What it does

- **Shapes** - arrow, box, ellipse, text label. Each has a stroke colour,
  stroke width, fill (or font size), and a start and end time.
- **Timing** - hard cut only. A shape is invisible, then fully visible at its
  start time, then invisible at its end time. No fades, no keyframes, no
  motion.
- **Timeline** - import video and stills, reorder by dragging, trim video clips
  with in/out points, give stills a duration, delete clips.
- **Export** - the whole timeline to one mp4 (H.264 + AAC, yuv420p), with a
  real progress bar and a working Cancel.
- **Projects** - saved as readable JSON, with undo/redo.

## Requirements

[Nix](https://nixos.org/download) with flakes enabled. Everything else -
Python, Qt, ffmpeg, the fonts used to draw text labels - is pinned by
`flake.lock` and fetched by Nix. There is nothing to install system-wide and no
dependence on a system ffmpeg.

If flakes are not enabled, add to `~/.config/nix/nix.conf`:

```
experimental-features = nix-command flakes
```

## Run it

```sh
nix run github:BruceChanJianLe/simple-video-editor      # from anywhere
nix run .                                               # from a clone
```

Or open a project directly:

```sh
nix run . -- ~/projects/demo.sve.json
```

## Develop

```sh
nix develop                 # Python, Qt, ffmpeg, test tooling
python -m sve.app           # run the GUI from source
python -m pytest            # run the test suite
ruff check .                # lint
```

The dev shell exports `SVE_FFMPEG`, `SVE_FFPROBE` and `SVE_FONT_DIR`, so the
app uses the pinned binaries rather than whatever is on `PATH`.

### Command line

Useful without the GUI, and used by the milestone checks:

```sh
sve probe FILE...            # print ffprobe results (rotation, VFR, audio)
sve probe -v FILE            # ...with every recorded field
sve export PROJECT OUT.mp4   # render a project headlessly
sve plan PROJECT             # print the export filter graph without running it
```

`sve plan` is the first thing to reach for when an export fails: it shows the
inputs and the filter graph that would be handed to ffmpeg.

## Build

### Linux

```sh
nix build .#simple-video-editor
./result/bin/simple-video-editor
```

The result is a wrapper with `QT_PLUGIN_PATH`, `SVE_FFMPEG`, `SVE_FFPROBE` and
`SVE_FONT_DIR` baked in as absolute store paths, so it runs with an empty
environment and never falls back to a system ffmpeg.

**AppImage**, for machines without Nix:

```sh
nix bundle --bundler github:ralismark/nix-appimage .#simple-video-editor
```

This produces a single self-contained executable with ffmpeg, Qt and the Qt
multimedia plugin inside.

Two caveats, both measured rather than assumed:

- **It is about 1.15 GB.** That is much larger than the application warrants.
  The runtime closure is 3.7 GB, and roughly 1.6 GB of it is clang, llvm and
  gcc, pulled in because nixpkgs' `shiboken6` - PySide6's *build-time* binding
  generator - is a runtime dependency of `pyside6` and links libclang.
  `qtwebengine` adds another 430 MB that this app never loads. Confirm the
  chain yourself with:

  ```sh
  nix why-depends .#simple-video-editor \
    $(nix path-info -r .#simple-video-editor | grep -m1 clang-.*-lib)
  ```

  Fixing it properly means splitting `shiboken6` into generator and runtime
  outputs upstream in nixpkgs, or trimming the propagated Qt module set. Both
  are nixpkgs changes rather than changes to this project, so neither is done
  here. Bundling the headless ffmpeg variant instead of the full one is done,
  and removes SDL, GTK4, zenity and gst-plugins-bad from the closure.

- **It was built but not executed** during development: AppImages need user
  namespaces to mount their squashfs, which the container this was built in
  does not permit (`cannot write uid_map`). The `nix build` path and the
  packaged binary from `result/bin` *were* run and screenshotted end to end.

### macOS

`nix build .#simple-video-editor` works on `aarch64-darwin` and
`x86_64-darwin` and gives you a runnable binary. For a distributable `.app`
bundle, use PyInstaller from inside the dev shell:

```sh
nix develop
pip install pyinstaller           # into a venv; not pinned by the flake
pyinstaller packaging/macos/simple-video-editor.spec
```

The spec bundles `ffmpeg` and `ffprobe` into `Contents/Resources/bin`, which is
one of the locations `sve/binaries.py` searches, so the app finds them without
`PATH`.

Signing, if you are distributing it:

```sh
# Nested binaries must be signed individually and *before* the bundle.
codesign --force --options runtime --timestamp \
  --sign "Developer ID Application: YOUR NAME (TEAMID)" \
  dist/Simple\ Video\ Editor.app/Contents/Resources/bin/ffmpeg \
  dist/Simple\ Video\ Editor.app/Contents/Resources/bin/ffprobe

codesign --force --options runtime --timestamp --deep \
  --entitlements packaging/macos/entitlements.plist \
  --sign "Developer ID Application: YOUR NAME (TEAMID)" \
  dist/Simple\ Video\ Editor.app

xcrun notarytool submit dist/SimpleVideoEditor.zip \
  --apple-id you@example.com --team-id TEAMID --wait
xcrun stapler staple dist/Simple\ Video\ Editor.app
```

> **Honestly reported:** the macOS packaging path was written but **not
> executed** - this was developed and tested on Linux, and codesigning and
> notarization need Apple credentials and a Mac. The Linux path, the Nix
> package and the whole test suite were run. Treat the `.app` spec as a
> starting point, not as verified.

## Using it

1. **Add media** (`Ctrl+I`). The first file imported sets the project's output
   resolution and frame rate; a still first leaves it at 1920x1080 @ 30fps.
2. **Select a clip** in the timeline. The player shows one clip at a time -
   annotation is a per-clip activity.
3. **Trim** with the in/out fields, or "Set in/out to playhead".
4. **Draw** with a tool from the palette. New shapes start at the playhead and
   run to the end of the clip; adjust in the properties panel or with "Set
   start/end to playhead".
5. **Preview full timeline** (`Ctrl+Shift+P`) renders the real export pipeline
   at half resolution and opens it, so what you see cannot disagree with what
   you will get.
6. **Export** (`Ctrl+E`).

### Keyboard

| Key | Action |
| --- | --- |
| `Space` | Play / pause |
| `←` `→` | Step one frame |
| `V` `A` `R` `E` `T` | Select, Arrow, Box, Ellipse, Text |
| `Delete` / `Backspace` | Delete the selected shape (or clip) |
| `Ctrl/Cmd+Z` / `Ctrl/Cmd+Shift+Z` | Undo / redo |
| `Ctrl/Cmd+S` | Save project |
| `Ctrl/Cmd+I` | Add media |
| `Ctrl/Cmd+E` | Export |

### Notes on behaviour that is intentional

- **Unfilled shapes are selected by their outline**, not their interior, so a
  large empty box does not swallow clicks meant for what is behind it. Give a
  shape a fill and its whole interior becomes grabbable.
- **Shapes outside the playhead's time range are not clickable** on the video,
  because they are not visible there. Select them from the shape list at the
  bottom, which moves the playhead into range.
- **Imports are normalized in the background** to the project's output spec and
  cached. The first export after adding media does that work if it has not
  happened yet; later exports of the same media are much faster.
  *Tools → Clear media cache* discards the intermediates; projects are
  unaffected.

## Tests

```sh
nix develop -c python -m pytest             # ~165 tests
nix develop -c python -m pytest -q tests/test_export_matches_preview.py
```

Media fixtures are generated on first run (see `tests/conftest.py`) rather than
committed, so the exact properties each test depends on - rotation metadata,
variable frame rate, a missing audio track - are visible in the source.

The suite runs headless via Qt's `offscreen` platform. Two checks are worth
knowing about:

- **`test_export_matches_preview.py`** is the correctness gate for the whole
  architecture: it proves an exported frame matches what the preview renderer
  draws, to under 3/255 mean channel difference, with annotation bounding boxes
  within 2px.
- **`test_preview_alignment.py`** guards the letterbox coordinate mapping,
  which is the bug that is easiest to ship and hardest to notice.

`tools/with-xvfb.sh` runs a command against a throwaway X server, for the
screenshot-based checks that need real compositing rather than `offscreen`:

```sh
nix develop -c tools/with-xvfb.sh -- python -m sve.app
```

## Licensing

**The bundled ffmpeg is the GPL build** (`withGPL = true`, with libx264 and
libx265).

The reason is that the delivery encoder settings - `-c:v libx264 -crf 20
-preset medium` - require libx264, which is GPL-licensed. The LGPL alternative
is available in nixpkgs and works (`withGPL = false` with `libopenh264`), but
openh264 supports neither `-crf` nor `-preset`, so quality targeting would have
to fall back to fixed bitrates.

This application invokes ffmpeg **as a separate process over a command line**;
it does not link against libav*. The app's own source is therefore not a
derivative work of ffmpeg, which is the same arrangement HandBrake, OBS and
Shotcut use. What the GPL does require is that anyone redistributing the
bundled binary also offers its corresponding source:

```sh
nix build .#ffmpeg-source     # the exact source of the bundled ffmpeg
```

The exact build is pinned by `flake.lock`, so the source that command produces
is provably the source of the binary that ships.

To build an LGPL variant instead, change the `ffmpeg` override in `flake.nix`
to `withGPL = false; withX264 = false; withX265 = false;` and adjust
`FINAL_ENCODE` in `src/sve/export.py` to use `libopenh264` with `-b:v`.

Fonts: DejaVu, under its own permissive licence, bundled so that text labels
render identically on every machine rather than depending on the host's
fontconfig.

## Known limitations

- Per-clip preview only. There is no gapless multi-file playback; "Preview full
  timeline" renders the real pipeline instead.
- No fades, transitions, motion or object tracking. This is a design decision,
  not a backlog item - see ARCHITECTURE.md.
- Above 40 shapes the exporter flattens overlays into per-interval composites.
  Correct, but the filter graph stops being readable.
- Projects reference media by absolute path. Moving a source file breaks the
  project until it is restored; the app warns on open and refuses to export.
- macOS `.app` packaging is written but unverified (see above).
