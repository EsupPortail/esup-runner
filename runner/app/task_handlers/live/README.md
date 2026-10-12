# Live task type

This package makes the fourth Runner task type visible next to `encoding`,
`studio` and `transcription`.

Live is not registered as a `BaseTaskHandler`. A Live session is a long-running
MediaMTX and FFmpeg lifecycle, so its implementation remains in
`app/services/live_runtime.py`, with media helpers in `app/services/live_media.py`
and HLS serving in `app/api/routes/live.py`.
