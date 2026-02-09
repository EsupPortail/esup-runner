# Runner task type: `encoding`

This page documents the **`encoding`** runner task type: what it does and which **JSON parameters** the Manager must send.

> Important: in this runner implementation, `TaskRequest.parameters` values are converted to CLI arguments using `str(...)` and then parsed with `json.loads(...)` inside the encoding script.
>
> That means nested parameters such as `cut`, `rendition`, `dressing` **must be provided as JSON strings** (not as nested JSON objects), otherwise they may be serialized with single quotes and fail JSON parsing.

## Quick navigation
- [What it does](#what-it-does)
- [Input](#input)
- [Manager parameters](#manager-parameters-json-strings)
  - [rendition](#rendition)
  - [cut](#cut)
  - [dressing](#dressing)
- [Example Manager payload](#example-manager-payload)
- [Full TaskRequest example](#full-taskrequest-example)
- [Error handling](#error-handling)
- [Common pitfalls](#common-pitfalls)

## What it does
The `encoding` task downloads a media file from `source_url` into a per-task workspace and runs the FFmpeg pipeline implemented in:
- [app/task_handlers/encoding/encoding_handler.py](../app/task_handlers/encoding/encoding_handler.py)
- [app/task_handlers/encoding/scripts/encoding.py](../app/task_handlers/encoding/scripts/encoding.py)

Outputs typically include:
- HLS playlists (`*.m3u8`) and/or MP4 renditions
- Audio derivatives (`.mp3`, `.m4a` depending on input)
- Thumbnails (`*_0.png`, `*_1.png`, `*_2.png`)
- Overview sprite (`overview.png`) + VTT (`overview.vtt`)
- Metadata (`info_video.json`) and logs (`encoding.log`)

## Input
- `source_url`: must point to a downloadable media file (mp4, mkv, webm, …). The runner validates the filename extension.

## Manager parameters (JSON strings)
All parameters are optional.

### `rendition`
Rendition configuration used to enable/disable MP4 outputs per resolution.

**Format** (JSON string):
```json
{
  "360": {"resolution": "640x360", "encode_mp4": true},
  "720": {"resolution": "1280x720", "encode_mp4": true},
  "1080": {"resolution": "1920x1080", "encode_mp4": false}
}
```

**Fields**
- Keys: typically `"360"`, `"720"`, `"1080"`.
- `encode_mp4` (bool): whether MP4 is produced for that rendition.
- `resolution` (string): informational/compatibility field (the pipeline currently scales based on fixed heights).

### `cut`
Trim a segment of the input before encoding.

**Format** (JSON string):
```json
{
  "start": "00:00:07",
  "end": "00:00:17",
  "initial_duration": "00:17:00"
}
```

**Fields**
- `start` (required): `HH:MM:SS`
- `end` (required): `HH:MM:SS`
- `initial_duration` (optional): if provided and `end` exceeds it, the script clamps the end time.

**Behavior notes**
- When valid, the script sets a global `SUBTIME` to `-ss <start> -to <end>`.
- Effective duration (after cut) is used for thumbnails/overview timestamps.

More details: see the `cut` section on this page.

### `dressing`
Apply “dressing” transformations **before** the final encoding:
- watermark overlay
- opening/ending credits concatenation

**Format** (JSON string):
```json
{
  "watermark": "https://example.org/assets/watermark.png",
  "watermark_position_orig": "top_right",
  "watermark_opacity": "80",
  "opening_credits_video": "https://example.org/assets/opening.mp4",
  "opening_credits_video_duration": "00:00:05",
  "ending_credits_video": "https://example.org/assets/ending.mp4",
  "ending_credits_video_duration": "00:00:08"
}
```

**Fields**
- `watermark` (optional): URL of an image (downloaded by the script).
- `watermark_position_orig` (optional): one of `top_left|top_right|bottom_left|bottom_right`.
  - `watermark_position` is also accepted for backward compatibility (can be French labels).
- `watermark_opacity` (optional): string percentage `"0"`..`"100"`.
- `opening_credits_video` / `ending_credits_video` (optional): URLs of videos to prepend/append.
- `opening_credits_video_duration` / `ending_credits_video_duration` (optional): duration hint (seconds or `HH:MM:SS`).

**Execution order**
1) If opening/ending credits are used *and* `cut` is present, the script first applies the cut to the **main video only**, then disables `SUBTIME` for the final encode.
2) Watermarking is applied to the (possibly cut) main video.
3) Credits are concatenated around the main video.
4) Final encoding runs on the resulting intermediate file.

## Example Manager payload
Example of a task request (showing the important parts):

```json
{
  "task_id": "<uuid>",
  "etab_name": "example",
  "app_name": "manager",
  "task_type": "encoding",
  "source_url": "https://example.org/media/video.webm",
  "notify_url": "https://manager.example.org/callback",
  "parameters": {
    "cut": "{\"start\":\"00:00:10\",\"end\":\"00:00:40\",\"initial_duration\":\"00:17:00\"}",
    "rendition": "{\"360\":{\"encode_mp4\":true},\"720\":{\"encode_mp4\":true},\"1080\":{\"encode_mp4\":false}}",
    "dressing": "{\"watermark\":\"https://example.org/wm.png\",\"watermark_position_orig\":\"top_right\",\"watermark_opacity\":\"80\"}"
  }
}
```

## Full TaskRequest example
This example includes optional fields (`app_version`, `affiliation`, `completion_callback`).

```json
{
  "task_id": "2ddc2c0e-9a8e-4aa1-8b4a-2f2c2f2c2f2c",
  "etab_name": "example-university",
  "app_name": "esup-runner-manager",
  "app_version": "1.0.0",
  "task_type": "encoding",
  "source_url": "https://example.org/media/video.webm",
  "affiliation": "employee",
  "parameters": {
    "cut": "{\"start\":\"00:00:10\",\"end\":\"00:00:40\",\"initial_duration\":\"00:10:00\"}",
    "rendition": "{\"360\":{\"resolution\":\"640x360\",\"encode_mp4\":true},\"720\":{\"resolution\":\"1280x720\",\"encode_mp4\":true},\"1080\":{\"resolution\":\"1920x1080\",\"encode_mp4\":false}}",
    "dressing": "{\"watermark\":\"https://example.org/assets/watermark.png\",\"watermark_position_orig\":\"top_right\",\"watermark_opacity\":\"80\"}"
  },
  "notify_url": "https://manager.example.org/api/tasks/callback",
  "completion_callback": "https://manager.example.org/api/tasks/completion-callback"
}
```

## Error handling
- Invalid JSON in `rendition`, `cut`, or `dressing` logs a warning and the feature is ignored.
- Missing `start`/`end` logs a warning and cut is not applied.
- Missing external tools (FFmpeg, ImageMagick `convert` for some fallbacks) may degrade outputs and is logged.

## Common pitfalls

### 1) Sending nested JSON objects instead of JSON strings
The runner converts `parameters[...]` to CLI args with `str(...)`, and the encoding script does `json.loads(...)`.

If you send `parameters.cut` as an object, it may become a Python-style string with single quotes (invalid JSON) and will fail parsing.

Recommended: send `cut`, `rendition`, and `dressing` as **JSON strings**.

### 2) Escaping JSON correctly in a JSON payload
Because the outer request is JSON and inner values are JSON strings, you must escape double-quotes inside.

Example:
```json
{
  "parameters": {
    "cut": "{\"start\":\"00:00:10\",\"end\":\"00:00:40\"}"
  }
}
```

### 3) `watermark_opacity` is treated as a percentage string
The script accepts any string and attempts `float(value)/100`. Keep it simple:
- good: `"80"` (80%)
- good: `"100"` (opaque)
- avoid: `80` (may still work depending on serialization, but keep it as a string for consistency)

### 4) `cut.initial_duration` vs `cut.duration`
Current implementation uses `initial_duration` to clamp the end timestamp. `duration` is not used.

### 5) Encoding-stage tool availability
Some features depend on external binaries:
- FFmpeg is required.
- ImageMagick `convert` is used only for some fallbacks (sprite creation / conversions).

When a tool is missing, the script logs a warning and may skip a fallback.
