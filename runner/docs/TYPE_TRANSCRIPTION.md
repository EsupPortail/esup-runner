# Runner task type: `transcription`

This page documents the **`transcription`** runner task type: what it does and which parameters the Manager can pass.

## What it does
The `transcription` task downloads a media file and generates subtitles using the OpenAI Whisper CLI.

Implementation:
- Handler: [app/task_handlers/transcription/transcription_handler.py](../app/task_handlers/transcription/transcription_handler.py)
- Script: [app/task_handlers/transcription/scripts/transcription.py](../app/task_handlers/transcription/scripts/transcription.py)

## Installation profile (CPU vs GPU)
- CPU-only server: `make sync-transcription-cpu` (uses the CPU-focused torch source on Linux x86_64 to avoid CUDA runtime packages).
- GPU server: `make sync-transcription-gpu`.

Outputs typically include:
- `subtitles.vtt` (WebVTT)
- logs and task metadata in the task output directory

## Input
- `source_url`: must point to a downloadable media file with an audio stream.

## Manager parameters
Parameters are sent in `TaskRequest.parameters`.

### `language`
- Type: string
- Default: runner env `WHISPER_LANGUAGE` (usually `auto`)
- Examples: `"auto"`, `"fr"`, `"en"`

### `model`
- Type: string
- Default: runner env `WHISPER_MODEL`
- Examples: `"small"`, `"medium"`, `"large"`, `"turbo"`

Note: the script maps some aliases (e.g. `large` -> `large-v3`).

### Compatibility metadata
- `model_type`: optional compatibility field accepted from Manager payloads (ignored by the transcription logic).
- `duration`: optional compatibility field accepted from Manager payloads (ignored by the transcription logic).

### `normalize`
- Type: bool
- Default: `false`
- If true, the script tries to normalize the extracted MP3 with `ffmpeg-normalize` before transcription.

### `format` (restricted)
- Type: string
- Default: `vtt`

Current limitation: the transcription script only accepts `vtt`. Sending `format=srt` will fail argument validation.

### Video identification metadata
Optional parameters used for identification/tracking only:

- `video_id`
- `video_slug`
- `video_title`

These values are:
- accepted by the transcription handler,
- forwarded to the transcription script,
- written to `info_video.json` when present,
- not used to alter transcription behavior.

## GPU behavior
GPU usage is controlled by runner configuration (`ENCODING_TYPE=GPU`) and is not selected per-task by the Manager.

## Example Manager payload
```json
{
  "task_id": "<uuid>",
  "etab_name": "example",
  "app_name": "manager",
  "task_type": "transcription",
  "source_url": "https://example.org/media/video.mp4",
  "notify_url": "https://manager.example.org/callback",
  "parameters": {
    "language": "auto",
    "duration": 17.0,
    "model": "turbo",
    "model_type": "WHISPER",
    "normalize": false,
    "format": "vtt",
    "video_id": "12345",
    "video_slug": "intro-to-python-2026",
    "video_title": "Intro to Python (2026)"
  }
}
```

## Full TaskRequest example
This example includes optional fields (`app_version`, `affiliation`, `completion_callback`).

```json
{
  "task_id": "7b1d5a5b-3333-4b3b-bbbb-444444444444",
  "etab_name": "example-university",
  "app_name": "esup-runner-manager",
  "app_version": "1.0.0",
  "task_type": "transcription",
  "source_url": "https://example.org/media/video.mp4",
  "affiliation": "student",
  "parameters": {
    "language": "auto",
    "duration": 17.0,
    "model": "turbo",
    "model_type": "WHISPER",
    "normalize": false,
    "format": "vtt",
    "video_id": "12345",
    "video_slug": "intro-to-python-2026",
    "video_title": "Intro to Python (2026)"
  },
  "notify_url": "https://manager.example.org/api/tasks/callback",
  "completion_callback": "https://manager.example.org/api/tasks/completion-callback"
}
```
