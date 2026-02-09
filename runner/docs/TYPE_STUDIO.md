# Runner task type: `studio`

This page documents the **`studio`** runner task type: what it does and which **parameters** the Manager can pass.

## What it does
The `studio` task is a **two-stage pipeline**:

1) **Studio generation stage**: build a single base MP4 (`studio_base.mp4`) from an OpenCast Mediapackage XML.
   - Script: [app/task_handlers/studio/scripts/studio.py](../app/task_handlers/studio/scripts/studio.py)
2) **Encoding stage**: run the standard encoding pipeline on the generated MP4 (renditions, thumbnails, overview, audio, metadata).
   - Script: [app/task_handlers/encoding/scripts/encoding.py](../app/task_handlers/encoding/scripts/encoding.py)

Orchestration is implemented in:
- [app/task_handlers/studio/studio_handler.py](../app/task_handlers/studio/studio_handler.py)

## Input
- `source_url`: must be the URL of an OpenCast Mediapackage XML.

The studio script extracts:
- presentation/source track
- presenter/source track
- optional SMIL cutting catalog (`smil/cutting`)

## Manager parameters
Parameters are sent in `TaskRequest.parameters`.

### Studio generation parameters
These parameters control **stage 1** (studio base video generation):

- `presenter` (string, optional): override presenter layout. Expected values: `mid`, `piph`, `pipb`.
- `force_cpu` (bool or string, optional): if true, forces CPU pipeline for studio generation even when the runner is configured for GPU.
- `studio_crf` (string/int, optional): CRF used for libx264/NVENC (default comes from runner env `STUDIO_DEFAULT_CRF`).
- `studio_preset` (string, optional): preset for x264/NVENC (default from `STUDIO_DEFAULT_PRESET`).
- `studio_audio_bitrate` (string, optional): audio bitrate (e.g., `"128k"`, default from `STUDIO_DEFAULT_AUDIO_BITRATE`).
- `studio_allow_nvenc` (bool or string, optional): allow NVENC in studio generation even for WebM/VP8/VP9/AV1 inputs.

**Resilience behavior**
- If the runner is configured for GPU and the studio generation fails, the handler retries on CPU unless `force_cpu` is already set.

### Encoding stage parameters (same as `encoding` type)
These parameters are passed to stage 2 (final encoding):

- `cut` (JSON string): see [docs/TYPE_ENCODING.md](TYPE_ENCODING.md)
- `rendition` (JSON string): see [docs/TYPE_ENCODING.md](TYPE_ENCODING.md)
- `dressing` (JSON string): supported and passed through to `encoding.py`.

> Important: `cut`, `rendition`, and `dressing` must be provided as JSON strings (see the note in [docs/TYPE_ENCODING.md](TYPE_ENCODING.md)).

### Strictness / compatibility note
The `studio` handler passes additional parameters to `encoding.py` as `--<key> <value>`.

If the Manager sends a parameter name that is **not supported by `encoding.py`** (currently only `--rendition`, `--cut`, `--dressing`), the encoding stage may fail with an argparse error.

Recommended: for `studio` tasks, only send the parameters documented on this page.

## Example Manager payload
```json
{
  "task_id": "<uuid>",
  "etab_name": "example",
  "app_name": "manager",
  "task_type": "studio",
  "source_url": "https://opencast.example.org/mediapackage.xml",
  "notify_url": "https://manager.example.org/callback",
  "parameters": {
    "presenter": "piph",
    "force_cpu": false,
    "studio_crf": "23",
    "studio_preset": "medium",
    "studio_audio_bitrate": "128k",
    "cut": "{\"start\":\"00:00:10\",\"end\":\"00:01:00\"}",
    "rendition": "{\"360\":{\"encode_mp4\":true},\"720\":{\"encode_mp4\":true},\"1080\":{\"encode_mp4\":false}}",
    "dressing": "{\"watermark\":\"https://example.org/wm.png\",\"watermark_position_orig\":\"top_right\",\"watermark_opacity\":\"80\"}"
  }
}
```

## Full TaskRequest example
This example includes optional fields (`app_version`, `affiliation`, `completion_callback`) and shows both studio-generation params and encoding-stage params.

```json
{
  "task_id": "e1c11b6a-1111-4f3f-aaaa-222222222222",
  "etab_name": "example-university",
  "app_name": "esup-runner-manager",
  "app_version": "1.0.0",
  "task_type": "studio",
  "source_url": "https://opencast.example.org/mediapackage.xml",
  "affiliation": "employee",
  "parameters": {
    "presenter": "piph",
    "force_cpu": false,
    "studio_crf": "23",
    "studio_preset": "medium",
    "studio_audio_bitrate": "128k",
    "studio_allow_nvenc": false,

    "cut": "{\"start\":\"00:00:10\",\"end\":\"00:01:00\"}",
    "rendition": "{\"360\":{\"encode_mp4\":true},\"720\":{\"encode_mp4\":true},\"1080\":{\"encode_mp4\":false}}",
    "dressing": "{\"watermark\":\"https://example.org/assets/watermark.png\",\"watermark_position_orig\":\"top_right\",\"watermark_opacity\":\"80\"}"
  },
  "notify_url": "https://manager.example.org/api/tasks/callback",
  "completion_callback": "https://manager.example.org/api/tasks/completion-callback"
}
```
