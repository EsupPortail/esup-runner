# Runner task type: `live`

This page documents the **`live`** Runner task type: what it does, its inputs,
the parameters dispatched by the Manager, and the playback output.

For installation, instance configuration and operations, follow
[MediaMTX setup](MEDIAMTX_SETUP.md) and [Live Runner operations](LIVE.md).
The client-facing API is documented in the [Manager Live guide](../../manager/docs/LIVE.md).

## Quick navigation

- [What it does](#what-it-does)
- [Input](#input)
- [Runner configuration](#runner-configuration)
- [Manager parameters](#manager-parameters)
- [Example client request to the Manager](#example-client-request-to-the-manager)
- [Full TaskRequest example](#full-taskrequest-example)
- [Outputs and status](#outputs-and-status)
- [Error handling](#error-handling)
- [Common pitfalls](#common-pitfalls)

## What it does

The `live` task pulls an incoming RTMP stream from the Manager's MediaMTX
instance and transcodes it into adaptive HLS with FFmpeg.

1. An encoder publishes RTMP to a source declared on the Manager. With
   `LIVE_AUTO_START=true` (default when Live is enabled), the Manager detects
   it and reserves an available Runner declaring `live` in its task types.
   No client start request is needed.
2. The Runner creates a temporary `live/<task_id>` relay path in its own
   MediaMTX instance, pointing to the discovered source on the Manager.
3. It waits for H.264 video and AAC audio, then starts FFmpeg.
4. FFmpeg produces a master playlist and the configured quality variants.
5. The Runner advertises playback readiness after every variant has recent,
   non-empty segments. The Manager exposes the resulting public `hls_url`.
6. On stop or failure, the Runner terminates FFmpeg, deletes its relay and HLS
   files, then reports completion and becomes available again.

One Live session occupies the entire Runner instance until cleanup completes.
For example, four simultaneous sessions require four Live-capable instances, which can be
declared as `[4x(live)]` in one `runner/.env`. They share one MediaMTX service;
each has its own API port, task state and session files.

Implementation:

- Task dispatch and control: [app/api/routes/task.py](../app/api/routes/task.py).
- Session lifecycle and recovery: [app/services/live_runtime.py](../app/services/live_runtime.py).
- FFmpeg command and HLS readiness: [app/services/live_media.py](../app/services/live_media.py).
- MediaMTX client: [app/services/mediamtx.py](../app/services/mediamtx.py).
- HLS serving and viewer estimate: [app/api/routes/live.py](../app/api/routes/live.py),
  [app/services/hls_viewers.py](../app/services/hls_viewers.py).

Live uses an asynchronous runtime supervised by the Runner service. Its FFmpeg
process is started by this runtime; there is no separate Live task CLI entrypoint.

## Input

- An encoder publishes an RTMP stream to the **Manager's MediaMTX instance**.
  The input must contain **H.264 video and AAC audio**.
- The Manager discovers accepted RTMP publications automatically; `LIVE_SOURCES`
  optionally declares sources before publication. The final RTMP path component
  is the public broadcaster name and is used as `source_id`, for example
  `live/amphi-a` becomes `amphi-a`. Full paths and credentials remain private.
- The Manager identifies the source from the RTMP publication and
  supplies its `source_path` to the Runner through its authenticated task API.
  In optional API-driven mode, the client selects `source_id` explicitly.
- The Runner combines `source_path` with its operator-configured
  `LIVE_UPSTREAM_RTMP_URL` to establish the relay.

`TaskRequest.source_url` must be an empty string. Live input is a continuous
stream, and the task does not download a source media file.

## Runner configuration

Declare Live explicitly in `RUNNER_TASK_TYPES`. For a mixed instance:

```dotenv
RUNNER_TASK_TYPES=[1x(encoding,studio,transcription,live)]
RUNNER_INSTANCES=1
ENCODING_TYPE=CPU
LIVE_TRANSCODER=cpu
```

This instance handles one task at a time, including during a Live session.
For independent VOD and Live processing from the same `.env`, use
`RUNNER_TASK_TYPES=[1x(encoding,studio),2x(live)]`. This creates three instances;
the two Lives normally listen on `RUNNER_BASE_PORT + 1` and `+ 2`. Direct HLS
URLs are derived from the Runner address and each actual port. Override
`LIVE_PUBLIC_HLS_BASE_URL` when needed, or use
`{instance_id}` in a reverse-proxy URL. `LIVE_HLS_DIRECTORY` also accepts
`{instance_id}`. Indices start at 0 and include VOD groups. The remaining Live
settings, including `LIVE_TRANSCODER`, `LIVE_NVDEC` and `LIVE_CPU_FALLBACK`,
apply to all instances. `[1x(live)]` remains valid for an exclusively Live service.

These values select the task types and backend; complete the MediaMTX URLs,
credentials, HLS directory and public URL using the
[instance configuration example](LIVE.md#instance-configuration).

`LIVE_TRANSCODER=cpu` (the default) selects libx264, and `LIVE_TRANSCODER=nvenc`
selects NVIDIA NVENC, independently of `ENCODING_TYPE`. The former `auto` value
is no longer accepted; see [the migration notes](LIVE.md#shared-settings-and-automatic-instance-values).
`LIVE_CPU_FALLBACK` controls fallback after a GPU failure. `LIVE_NVDEC` enables
optional CUDA decoding in NVENC mode and is ignored in CPU mode, including after
fallback. Without NVDEC, decoding uses CPU; scaling uses CPU in both modes.
See [FFmpeg setup](FFMPEG_SETUP.md) for the required FFmpeg build.

The operator can override the adaptive renditions and CPU/GPU selection.
Frame rate, keyframes, HLS timing, presets, timeouts and retries use fixed internal
defaults; they are no longer environment parameters. The full reference
is in [PARAMETERS.md](PARAMETERS.md#live-parameters); profile examples are in
[LIVE.md](LIVE.md#abr-profile-and-timeouts).

## Manager parameters

The Runner requires two entries and accepts one optional entry in `TaskRequest.parameters`:

| Parameter | Type | Required | Meaning |
| --- | --- | --- | --- |
| `source_id` | String | Yes | Broadcaster name from the final RTMP path component, such as `amphi-a` |
| `source_path` | String | Yes | Private MediaMTX path ending with the broadcaster name, such as `live/amphi-a` |
| `broadcaster_id` | String | No | Live source identifier matched against the Pod broadcaster's `source_id` when synchronization is enabled |

The source values are plain strings. The Runner validates non-empty values of at most
500 characters, consisting of letters, digits, underscores or hyphens, optionally
separated by `/`. The Manager additionally restricts logical source identifiers
to its public API format. A source path contains no scheme, host, credentials,
query string or `..` segment. `broadcaster_id`, when present, is an internal
1-200 character identifier made of letters, digits, `.`, `_` or `-`, derived by
the Manager from the final source-path component. Pod synchronization matches
it against the broadcaster's `source_id` field, introduced in Pod 4.5.0, using
`GET /rest/broadcasters/?source_id=<identifier>`. The Pod slug can differ; it is
used only for the final PATCH of the uniquely matched broadcaster.

Renditions, codecs, backend selection and FFmpeg arguments are instance settings;
additional request parameters are rejected. `event_id` belongs to the Manager's
Live API and is not a Runner parameter. When the Runner's `LIVE_POD_*` settings
are configured, `broadcaster_id` is required.

The surrounding task fields also have Live-specific requirements:

- `task_id` must be a UUID and is also the session's `live_id`.
- `task_type` must be `live`.
- `source_url` and `notify_url` must both be empty strings.
- `completion_callback` must be the Runner's configured `MANAGER_URL`, without
  a trailing slash, followed by `/task/completion`.

## Example client request to the Manager

This is optional: publishing alone starts a session by default. For automatic
sessions, discover the playback URL in the Manager administration or authenticated
`GET /live/sources/{source_id}` response (`live.hls_url`). See
[automatic RTMP operation](../../manager/docs/LIVE.md#automatic-rtmp-to-hls-operation).
The Manager and Runner services must already be running.

For client-controlled starts, set `LIVE_AUTO_START=false` on the Manager.
The client then starts a session with `POST /live/start` on the **Manager**, using
its client token. The Manager allocates the Runner and constructs the internal task:

```json
{
  "source_id": "amphi-a",
  "event_id": "pod:event:42:session:1",
  "equipment": "SMP351",
  "etab_name": "example-university",
  "app_name": "Esup-Pod"
}
```

`event_id` is an idempotency key scoped to the authenticated client token.
Repeating it for the same source retrieves the same session, including after
completion. Use a new event/session key for a new broadcast.

Poll `GET /live/{live_id}` on the Manager to obtain the state and `hls_url`.
Stop with `POST /live/{live_id}/stop`, using the same client token. See the
[Manager API examples](../../manager/docs/LIVE.md#integration-with-esup-pod)
for authentication, responses and source configuration.

## Full TaskRequest example

The **Manager** sends this payload to the selected Runner's `POST /task/run`,
authenticated with `Authorization: Bearer <RUNNER_TOKEN>`. It illustrates the
internal dispatch contract; client applications use the Manager API above.

```json
{
  "task_id": "d6e7aa69-d1f8-46c4-b23e-4f0202e14cd2",
  "etab_name": "example-university",
  "app_name": "Esup-Pod",
  "task_type": "live",
  "source_url": "",
  "notify_url": "",
  "parameters": {
    "source_id": "amphi-a",
    "source_path": "live/amphi-a",
    "broadcaster_id": "amphi-a"
  },
  "completion_callback": "http://manager.internal:8081/task/completion"
}
```

This example assumes the Runner has `MANAGER_URL=http://manager.internal:8081`.
The path shown is a placeholder; the Manager sends the actual configured path
over the internal control network. Keep that path out of public client responses.

The initial Runner response is `{"status":"started","task_id":"..."}`.
This acknowledges task acceptance; playback becomes available after HLS readiness
checks succeed. Redispatching an existing task UUID returns its recorded status.

## Outputs and status

FFmpeg writes the session output under `LIVE_HLS_DIRECTORY/<live_id>/`.
On Linux, use `/dev/shm/hls` to store playlists and TS fragments in memory; see
[HLS storage in memory](LIVE.md#hls-storage-in-memory-on-linux) for sizing, Docker
and recovery requirements. This works with both CPU and GPU transcoding.

Output layout:

```text
<live_id>/
├── master.m3u8
├── high/index.m3u8
├── high/segment_<sequence>.ts
├── mid/index.m3u8
├── mid/segment_<sequence>.ts
├── low/index.m3u8
└── low/segment_<sequence>.ts
```

The names above are the default variants; `LIVE_VARIANTS` defines their names,
dimensions and bitrates. Playlists form a sliding window and old segments are
deleted. Scaling preserves the source aspect ratio within each configured box.

The public master URL is
`<LIVE_PUBLIC_HLS_BASE_URL>/<live_id>/master.m3u8`. Clients must use the exact
`hls_url` returned by the Manager, which accounts for the configured public domain
and proxy prefix. The Runner's local serving route is
`/hls/<live_id>/<filename>`.

The general task status stays `running` while the Live session is active.
Detailed progress is recorded in the Runner's `live` metadata and exposed by the
Manager as `state`:

| Live state | Meaning |
| --- | --- |
| `pending` | Manager intent persisted; Runner reservation/dispatch in progress |
| `waiting_source` | Relay prepared; waiting for compatible input |
| `starting` | FFmpeg/HLS starting or recovering |
| `running` | All configured variants have readable, recent segments |
| `stopping` | Shutdown or cleanup in progress; Runner remains reserved |
| `completed` | Normal stop with cleanup complete |
| `failed` | Failure with cleanup complete |

`hls_url` is advertised only while playback is ready. It becomes null during
interruptions, stop or failure. The Runner sends the Manager's completion callback
after cleanup. Clients follow status through the Manager's Live API; this task
does not produce a VOD result manifest or an archived recording. Session HLS files
are removed at the end.

While a session is active, `hls_sessions` estimates concurrent viewers from the
distinct client addresses that requested a playlist or segment during the last
15 seconds. The value is kept in memory, is not persisted as a viewer history,
and can undercount clients behind a shared proxy or NAT. When a deployment uses
MediaMTX-native HLS, its HLS session count can be used instead; an unavailable
measurement is reported as `null` rather than as zero.

## Error handling

- The Runner rejects malformed Live requests with HTTP `400`, including extra
  parameters, non-empty `source_url` or `notify_url`, or an invalid Manager
  completion callback. Missing required request fields can return `422`.
- A new task sent to a busy Runner returns `400`; an unregistered Runner returns
  `503`. The Manager returns `503` when no Live-capable Runner is available.
- Missing sources time out after 120 seconds (internal default). Incompatible
  codecs fail the session. FFmpeg exits or stalled HLS trigger bounded restarts
  governed by the configured Live retry and timeout settings.
- While MediaMTX or filesystem cleanup is incomplete, the Runner remains busy
  with state `stopping`; availability is restored only after cleanup succeeds.
- After a Runner restart, interrupted sessions are cleaned up and normally
  reported as failed. A recorded stop request completes normally. Starting a new
  automatic broadcast requires the encoder to disconnect and republish; an API
  client can also request a new session explicitly. The same publication is not
  automatically restarted after a terminal failure or an explicit stop. The
  Manager's **Restart session** action can create a new HLS session while the
  encoder stays connected; it does not reuse the terminal task UUID.

For diagnostics and recovery procedures, see
[Live operations](LIVE.md#stop-and-recovery) and
[manual validation](LIVE.md#manual-validation).

## Common pitfalls

- Setting only `LIVE_*` values without declaring `live` in `RUNNER_TASK_TYPES`:
  the instance will not advertise or accept Live tasks.
- Expecting one mixed instance to run VOD and Live simultaneously: one running
  task occupies the entire instance; use separate groups for parallel work.
- Using the same static public HLS URL for `[4x(live)]`: use `{port}` or
  `{instance_id}` to route playback to the owning instance.
- Sending `rendition`, `cut`, `dressing` or arbitrary FFmpeg arguments in the
  payload: the accepted parameters are `source_id`, `source_path` and the
  optional internal `broadcaster_id` source identifier.
- Treating task acceptance as playback readiness: wait for `state=running`,
  `hls_ready=true` and a non-null `hls_url` from the Manager.
- Downloading `/task/result/{task_id}` as an encoding result: Live playback uses
  the advertised HLS URL, and its temporary media files are deleted on cleanup.
- Exposing the Runner's task API with the public HLS prefix: configure the
  [HTTPS frontend](LIVE.md#service-and-hls-exposure) to expose the intended media
  routes and enforce viewer access for private streams.
