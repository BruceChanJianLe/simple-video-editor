# Architecture

Read this before changing how the preview or the export works. Most of it
exists to explain decisions that look like limitations and are not.

## The one decision that matters: overlay, not compositing

Annotations are drawn **on top of** a stock media player. They never touch a
decoded video frame.

```
                 ┌───────────────────────────────┐
                 │  sve/render.py                │
                 │  draw_shape(painter, shape,   │
                 │             target_size)      │
                 └───────────────┬───────────────┘
                                 │  one function, two callers
              ┌──────────────────┴───────────────────┐
              │                                      │
    ┌─────────▼──────────┐              ┌────────────▼────────────┐
    │ PREVIEW            │              │ EXPORT                  │
    │ QPainter, at the   │              │ QPainter, onto a        │
    │ on-screen picture  │              │ transparent QImage at   │
    │ size               │              │ full video resolution   │
    │                    │              │           │             │
    │ QMediaPlayer plays │              │           ▼             │
    │ the source file    │              │ one PNG per shape       │
    │ underneath         │              │           │             │
    └────────────────────┘              │           ▼             │
                                        │ ffmpeg overlay=0:0      │
                                        │ enable='between(t,a,b)' │
                                        └─────────────────────────┘
```

There is **no frame decoder, no compositor and no playback loop in this
codebase.** If you find yourself writing one - managing presentation
timestamps, an audio clock, or keyframe seeking - stop. That path has been
considered and rejected. It is how this kind of project turns into a bad
video player instead of a good annotation tool.

This works *specifically because* annotations sit above the video and never
modify the pixels underneath. Do not generalise the design to support effects
that would (colour correction, transitions, speed changes, warps). They are out
of scope, and adding one would collapse the whole arrangement into a frame
pipeline.

## The shared renderer

`sve/render.py` holds the only code that knows what a shape looks like.

```python
draw_shape(painter, shape, target_size)
```

It is a pure function of the shape and the target size. The preview calls it at
the on-screen picture size; the exporter calls it against a transparent
`QImage` at the project's output resolution. Nothing else may draw a shape.

Two rules keep that possible:

* **Everything in a `Shape` is normalized.** Positions are 0..1 fractions of
  the frame. Stroke width and font size are fractions of frame *height*.
  Rendering at 640x360 and at 1920x1080 then differ only by a uniform scale.
* **Selection handles are not drawn by the renderer.** They belong to
  `sve/ui/overlay.py`, because they must never reach an export.

The alternative - reconstructing shapes with ffmpeg's `drawbox`/`drawtext`
filters - is rejected on purpose. Two renderers that must agree will eventually
stop agreeing, and the disagreement surfaces in an exported file after the
preview looked fine.

`tests/test_export_matches_preview.py` is the standing proof. It exports a
frame, independently composites the same shapes onto the un-annotated frame
with QPainter, and requires them to match: mean channel difference under 3/255,
annotation bounding boxes within 2px, and none of the drawn annotation missing.

## Coordinates

Shape geometry is stored **normalized 0.0-1.0 against the video frame**, never
in widget pixels. Conversion happens at the edges: widget → normalized on mouse
input, normalized → target pixels at draw time.

The subtle half is *which* rect to convert against. A video is letterboxed
inside the preview, so the picture rect is not the widget rect.
`VideoCanvas._relayout` computes the picture rect once with
`geometry.video_display_rect`, then **positions** the video item, the still
item and the annotation overlay into exactly that rect. The overlay's own
coordinate space is therefore the picture: `(0, 0)` is the top-left of the
frame, and converting a mouse position is a plain division with no offset term.

This replaced an arrangement that was subtly wrong, and the note is here so it
does not come back. Sizing `QGraphicsVideoItem` to the whole viewport and
letting it fit internally leaves it centring the picture inside that size while
reporting a `boundingRect` that is *already* the fitted size. Re-deriving the
picture rect from that bounding rect silently drops the centring offset, so
every annotation is drawn one letterbox bar out of place - 105px on a 782x650
preview of a 16:9 clip. Nothing looks stretched or obviously broken, which is
why it survived until a (0,0)-(1,1) shape was drawn over real video.

`tests/test_preview_alignment.py` guards it.

## Why QGraphicsVideoItem and not QVideoWidget

The natural arrangement - a translucent `QWidget` on top of a `QVideoWidget` -
**does not work on Qt 6.** `QVideoWidget` renders through its own surface,
which paints over child *and* sibling widgets; the overlay is simply absent.
Verified by screenshotting both arrangements against live video.

`QGraphicsVideoItem` is the same stock Qt Multimedia video sink, but it renders
inside a `QGraphicsScene`, so a sibling `QGraphicsItem` composites above it
through the ordinary QPainter path, alpha and all.

Nothing else about the architecture changes: playback is still `QMediaPlayer`
on the source file, annotation is still QPainter, export is still ffmpeg.

## Time

Two clocks, and the distinction is load-bearing.

* **`Shape.start` / `Shape.end` are in the source file's own timebase** - the
  same clock as `Clip.in_point` and `Clip.out_point`. They are not offsets from
  the in point and not global timeline times.
* **Global timeline time** exists only in the exporter.

Because the preview plays the source file directly and enforces the trim as
playback bounds, the player's position *is* source time, so the overlay
compares it against `Shape.start`/`end` with no conversion at all.

Conversion happens in exactly one place, `export.global_range`:

```
global_start = clip_offset + (shape.start - clip.in_point)
```

The payoff is that reordering clips or changing a trim point cannot
desynchronise annotations: a shape stays attached to the content it annotates.

> **Spec note.** The build prompt's data model comment said shape times were
> "relative to this clip's in_point", but its export formula
> (`clip_offset + (shape.start - clip.in_point)`) and its stated requirement
> that retrimming must not desynchronise annotations both require source-file
> time. Two of the three agree, and source time is the only reading under which
> retrimming behaves as specified, so that is what is implemented. For image
> clips, where `in_point` is always 0, the two readings coincide.

### Timing model: hard cut

A shape is invisible, then fully visible at `start`, then invisible at `end`.
The interval is half-open, `[start, end)`, so two back-to-back shapes never
both appear on one frame.

There are no fades, no easing, no keyframes, no motion and no object tracking,
and there is deliberately no interpolation abstraction "for later". Adding one
would require the renderer to become a function of time as well as geometry,
which is the first step back toward a frame pipeline.

## Export

One ffmpeg invocation, one encode pass, one filter graph:

```
normalized clips → trim → concat → overlay shapes → encode
```

Each shape is rasterized to a **full-frame** transparent PNG, so its position
is baked into the image and every overlay is at offset `0:0`. The filter graph
carries no coordinate arithmetic; positioning correctness lives entirely in the
shared renderer.

Overlay comes **after** concat because `enable` is evaluated against the output
timeline's clock. Gating per clip, before concatenation, would put every
annotation after the first clip at the wrong moment.

Two ffmpeg details that are easy to get wrong and are pinned deliberately:

* The overlay PNG is a single frame, so it hits EOF immediately.
  `repeatlast=1` and `eof_action=repeat` hold it available for the whole
  timeline. Without them a shape composites onto frame 0 only and is absent for
  the rest of its window.
* `-progress` reports `out_time_ms` in **microseconds**, identical to
  `out_time_us`, despite the name. Read as milliseconds it makes the progress
  bar advance at a thousandth of the real rate, which looks like a hang.

Above `MAX_OVERLAY_INPUTS` (40) shapes, overlays are flattened into one PNG per
interval between shape boundaries, so the input count depends on how many
distinct moments there are rather than how many shapes.

## Normalization

Every clip is re-encoded to the project's output spec before concatenation.

The ffmpeg `concat` demuxer with `-c copy` only works when every input matches
exactly in codec, resolution, pixel format, frame rate, timebase and audio
parameters. Real inputs never do, and the failure mode is the dangerous kind:
it appears to succeed, then produces output that is corrupt or progressively
desynced after the first join. **Do not add a stream-copy fast path.**

Normalization happens **on import** rather than at export, because import is
where a user expects to wait. Intermediates are cached under the platform cache
directory, keyed by source path, mtime and output spec.

* Video → mp4 at the output resolution, frame rate, pixel format and audio
  layout. The *whole* source, untrimmed, so changing a trim never invalidates
  the cache. `fps=` resamples variable frame rate to constant; screen
  recordings are routinely VFR and concatenating VFR segments drifts.
* Stills → PNG at exactly the output resolution. Duration stays a clip
  property, applied at export with `-loop 1 -t`, so retiming a still is free.
* Clips with no audio get `anullsrc` silence, so concat always sees a
  consistent stream count.

Scaling preserves aspect and pads; it never stretches:

```
scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2,setsar=1
```

Colour is stated explicitly (`bt709`, `-color_range tv`) rather than inferred,
because a source tagged full-range and an untagged one otherwise converge on
different matrices and the export looks washed out next to the preview.

## Preview of the whole timeline

Per-clip preview is the default: the player shows one clip at a time. Gapless
playback across several files is genuinely hard and buys nothing here, since
annotation is a per-clip activity.

"Preview full timeline" runs the **real export pipeline** to a temp file at
reduced resolution and plays the result. It is honest by construction - it
cannot disagree with the export, because it *is* the export.

## Undo

The project is one serializable document, and undo is a stack of deep copies of
it. At this scale - a five minute video, a handful of clips - a copy costs
microseconds and a few kilobytes, and it buys an undo stack that is
structurally incapable of drifting out of sync with the real state, because it
*is* the state.

Interactive gestures must produce one undo entry, not one per mouse-move:
`Document.begin()` on press and `Document.commit()` on release, or the
`document.edit(label)` context manager.

## Module map

| Module | Responsibility |
| --- | --- |
| `model.py` | The document: `Project`, `Clip`, `Shape`, JSON round-trip |
| `undo.py` | `Document` - live project plus snapshot history |
| `geometry.py` | Letterbox arithmetic; widget ↔ normalized conversion |
| `render.py` | **The shared renderer.** The only code that draws a shape |
| `probe.py` | ffprobe wrapper; rotation and VFR handling |
| `importer.py` | Files → clips; first-import output defaults |
| `normalize.py` | Cached per-clip intermediates at the output spec |
| `export.py` | Filter graph, global time conversion, encode settings |
| `ffmpeg_run.py` | Subprocess, progress parsing, cancellation |
| `binaries.py` | Locating bundled ffmpeg/ffprobe |
| `cache.py` | Cache locations |
| `thumbnails.py` | Timeline thumbnails on a worker pool |
| `ui/state.py` | `EditorState` - shared observable editor state |
| `ui/overlay.py` | The annotation overlay item and its editing gestures |
| `ui/preview.py` | Canvas, video/still items, transport controls |
| `ui/timeline.py` | Clip list, reordering, trim controls |
| `ui/inspector.py` | Tool palette and shape properties |
| `ui/shape_list.py` | Shape table for the current clip |
| `ui/export_dialog.py` | Threaded export with progress and cancel |
| `ui/main_window.py` | Menus, layout, actions |

## Out of scope

Screen recording, transitions, audio editing or mixing, multi-track timelines,
colour correction, filters and effects, speed changes, GPU acceleration, proxy
media, cloud anything, plugins.

These are not "not yet". Several of them would require the frame pipeline this
design exists to avoid.
