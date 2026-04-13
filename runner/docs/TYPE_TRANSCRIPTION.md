# Runner task type: `transcription`

This page documents the **`transcription`** runner task type: what it does and which parameters the Manager can pass.

## What it does
The `transcription` task downloads a media file and generates subtitles using Whisper.

Behavior depends on the requested `language`:
- `language=auto`: subtitles stay in the detected spoken language
- `language=<code>` and detected source language matches that code: normal transcription
- `language=<code>` and detected source language differs: the runner transcribes in the source language first, then translates the VTT while preserving timestamps

Implementation:
- Handler: [app/task_handlers/transcription/transcription_handler.py](../app/task_handlers/transcription/transcription_handler.py)
- Script: [app/task_handlers/transcription/scripts/transcription.py](../app/task_handlers/transcription/scripts/transcription.py)

## Installation profile (CPU vs GPU)
- CPU-only server: `make sync-transcription-cpu` (installs a CPU-only torch profile on Linux x86_64 to avoid CUDA runtime packages).
- GPU server: `make sync-transcription-gpu`.
- Current transcription dependency support:
  - `transcription-cpu`: supported on Linux x86_64 and macOS Apple Silicon (`arm64`).
  - `transcription-gpu`: supported on Linux x86_64 GPU/CUDA hosts.
  - macOS Intel (`x86_64`) is not supported for transcription with the current `torch` stack because upstream wheels are no longer published for that platform.

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

Semantics:
- `auto`: keep the detected spoken language
- explicit code such as `fr` or `en`: request the final subtitle language

Current translation support:
- `fr -> en`
- `en -> fr`

When translation happens, the translated subtitles remain the main `<stem>.vtt` output and the source-language subtitles are also kept as a sidecar `<stem>.source-<lang>.webvtt.txt`.
The sidecar intentionally does not use the `.vtt` extension, so client applications that pick the first VTT file only see the final deliverable subtitles.
The runner also records runtime metadata in `info_video.json`, including the detected source language, final subtitle language, and the translation model that was actually used.
The local translation models used by this task are cached under `CACHE_DIR/huggingface`
(or under `HUGGINGFACE_MODELS_DIR` when explicitly overridden).

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

## Translation behavior
- The transcription pass always starts in source-language auto-detection mode.
- Translation is only triggered after Whisper has detected the spoken language and only when the final requested subtitle language differs.
- The runner currently uses internal local FR<->EN translation models.
- CPU runners use lighter `Helsinki-NLP/opus-mt-en-fr` / `Helsinki-NLP/opus-mt-fr-en` models.
- GPU runners use larger `Helsinki-NLP/opus-mt-tc-big-en-fr` / `Helsinki-NLP/opus-mt-tc-big-fr-en` models.
- Because of that hardware-aware selection, translation quality can vary slightly across deployments. GPU runners usually have a little more headroom for higher-quality translation models.
- For target languages outside the dedicated local `fr <-> en` pipeline, the runner falls back to the historical Whisper-only behavior and re-runs Whisper with the requested subtitle language.
- That Whisper fallback is only a compatibility path. It can still be useful for broader language coverage (`de`, `es`, ...), but its quality is less predictable than the dedicated local FR/EN translation pipeline.
- `info_video.json` explicitly indicates whether a task used `local_translation`, `whisper_legacy_fallback`, or no translation at all.
- If the media contains no speech and Whisper produces an empty VTT, the runner keeps that empty VTT as a valid result and skips translation instead of failing.

## Transcription exit codes
The external script returns `0` on success. The most common non-zero exit codes are:

| Code | Meaning | Typical cause |
| --- | --- | --- |
| `2` | Input file not found | The downloaded media file is missing from the task workspace |
| `5` | Expected VTT output not found | Whisper did not produce the subtitle file the runner expected |
| `6` | VTT finalization failed | Rename/post-processing of the generated VTT failed |
| `7` | VTT validation failed | The output VTT looks unreadable or clearly truncated |
| `10` | Whisper model load failed | Whisper could not start the requested model on the selected runtime |
| `20` | Whisper returned no usable result | The Python API returned no structured transcription result |
| `21` | VTT writing failed | Whisper result could not be serialized to WebVTT |
| `22` | Chunk extraction failed | `ffmpeg` failed while cutting one temporary audio chunk |
| `30` | Unsupported local translation pair | A dedicated local translation model is not available for the requested pair |
| `31` | Translation backend unavailable | `transformers` / `sentencepiece` / translation model loading failed |
| `32` | Translation failed | The subtitle translation step failed while processing cues |
| `33` | Translation decision failed | A target subtitle language was requested, but the runner could not determine a spoken source language |
| `124` | Timeout | `ffmpeg` or Whisper CLI exceeded the allowed runtime |

Notes:
- The dedicated local translation path currently only covers `fr <-> en`.
- For other target languages, the runner falls back to Whisper's historical multilingual behavior instead of returning `30` in normal operation.
- Some non-zero codes can still come directly from external tools (`ffmpeg`, Whisper CLI) when the runner intentionally propagates the underlying process failure.

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
