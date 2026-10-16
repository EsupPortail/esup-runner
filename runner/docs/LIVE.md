# Live Runner: media, operations and validation

> For a new deployment, follow the Manager's [short Live installation path](../../manager/docs/LIVE_INSTALLATION.md).
> This page is the Runner-side configuration, exposure, recovery and validation reference.

For the task contract, Manager parameters, request examples and outputs, see
[TYPE_LIVE.md](TYPE_LIVE.md). This guide covers instance configuration and operations.

Each Runner instance declaring `live` handles a single adaptive transcode at a
time. Instances started from the same `.env` share one Runner MediaMTX service. It registers and receives tasks through the existing APIs.
The [Manager](../../manager/docs/LIVE.md) reserves the instance and provides Pod
with the complete HLS URL. There are no multiple slots or passthrough mode.

## Contents

- [Architecture and media relay](#architecture-and-media-relay)
- [Instance configuration](#instance-configuration)
- [ABR profile and timeouts](#abr-profile-and-timeouts)
- [Service and HLS exposure](#service-and-hls-exposure)
- [Stop and recovery](#stop-and-recovery)
- [Manual validation](#manual-validation)

## Architecture and media relay

The Manager discovers the encoder's publication through its own MediaMTX
Control API, then dispatches a Live task to a reserved Runner instance. See
[Manager publication discovery](../../manager/docs/LIVE.md#architecture-and-design-choices)
for that first step. The Runner configures a native pull relay in its local
MediaMTX and supervises FFmpeg, which reads the relayed stream and produces HLS.

```mermaid
sequenceDiagram
    participant M as Manager
    participant U as Manager MediaMTX
    participant R as Live Runner
    participant X as Runner MediaMTX
    participant F as FFmpeg
    participant H as HLS files

    M->>R: POST /task/run (live_id, source_path)
    R->>X: POST /v3/config/paths/add/{path} — port 10997
    X->>U: Open RTMP read connection — port 1935
    U-->>X: Original H.264/AAC stream
    R->>X: GET /v3/paths/get/{path}
    X-->>R: Source availability, codecs and counters
    R->>F: Start and supervise CPU / NVENC transcode
    F->>X: Read live/{live_id} over RTMP — port 2935
    X-->>F: Relayed H.264/AAC stream
    F->>H: Write master playlist and rendition segments
    R->>H: Check playlist and segment readiness
    M->>R: GET /task/status/{live_id}
    R-->>M: Live state, metrics and hls_url when ready
```

The diagram uses the native installation's default media ports. Docker and
custom deployments use the addresses configured for their own network.
The connections have separate roles:

| Runner setting | Connection and purpose |
| --- | --- |
| `LIVE_UPSTREAM_RTMP_URL` | Manager MediaMTX RTMP endpoint, normally port `1935`, with read credentials; the Runner appends the source path supplied by the Manager |
| `LIVE_MEDIAMTX_API_URL` | Runner MediaMTX HTTP Control API, default `http://127.0.0.1:10997`; authenticated with `LIVE_MEDIAMTX_API_USER` and `LIVE_MEDIAMTX_API_PASSWORD` |
| `LIVE_LOCAL_RTMP_URL` | Runner MediaMTX RTMP endpoint, normally port `2935`, with local read credentials; FFmpeg reads the session's relay path here |
| `LIVE_PUBLIC_HLS_BASE_URL` | Public Runner HTTP or reverse-proxy address from which players fetch the generated HLS files |

The relay lifecycle works as follows:

1. The Manager sends the session UUID and private source path through the
   authenticated Runner task API. The Runner builds the upstream RTMP URL from
   its own `LIVE_UPSTREAM_RTMP_URL` configuration.
2. Through the local MediaMTX Control API, the Runner creates a temporary
   `live/<live_id>` path with that upstream URL as its `source` and
   `sourceOnDemand: false`. This starts the relay before FFmpeg connects.
3. **Runner MediaMTX opens the RTMP connection to Manager MediaMTX.** The media
   flows from Manager MediaMTX to Runner MediaMTX over that connection. The
   upstream read account needs permission to read the encoder's source path.
4. The Runner polls its local relay's runtime path information to check source
   availability, H.264/AAC codecs and byte counters. It starts FFmpeg when the
   source is compatible, then continues monitoring the relay and HLS output;
   the internal monitoring interval is two seconds.
5. FFmpeg reads `live/<live_id>` from Runner MediaMTX and writes the adaptive
   HLS playlists and segments into the session's output directory. The Runner
   serves these files through `/hls/<live_id>/...`, directly or behind a reverse
   proxy. MediaMTX provides the RTMP relay; FFmpeg produces the HLS output.
6. The Manager polls the Runner's task status. A playable `hls_url` is reported
   only after the source is available and all HLS renditions are ready. On
   termination, the Runner stops FFmpeg, deletes its relay through
   `DELETE /v3/config/paths/delete/{path}` and removes the session's HLS files.
   The Runner becomes reusable only after cleanup is confirmed.

Each session owns a separate `live/<live_id>` relay path, so multiple Runner
instances can share one MediaMTX service without sharing session paths. Removing
one relay leaves the original encoder publication on Manager MediaMTX available.
The Runner controls its own MediaMTX API; the Manager communicates with the
Runner through the task API. The Manager does not need direct access to Runner
MediaMTX's Control API.

## Instance configuration

Declare Live explicitly in `RUNNER_TASK_TYPES` in the Runner's `.env`, alongside
the `LIVE_*` settings. These forms are supported:

| Declaration | Processes and capacity |
| --- | --- |
| `[1x(encoding,studio,transcription,live)]` | One mixed instance; one task of any declared type at a time |
| `[1x(encoding,studio),1x(live)]` | Two instances; a VOD task and a Live session can run concurrently |
| `[1x(live)]` | One instance used exclusively for Live |
| `[1x(encoding,studio),2x(live)]` | Three instances; one VOD task and two Live sessions can run concurrently |
| `[4x(encoding,studio,transcription,live)]` | Four mixed instances; up to four tasks in any combination, including four Lives |

The normal Runner launcher reads the declaration and starts every expanded
instance. Grouped syntax computes `RUNNER_INSTANCES`; omit it or set it to the
computed total to avoid a mismatch warning. Instance indices start at **0** and
include all VOD and Live groups. With `RUNNER_BASE_PORT=8082` and
`[1x(encoding,studio),2x(live)]`:

| Instance index | Task types | Planned API port | Direct HLS base URL |
| --- | --- | --- | --- |
| 0 | encoding, studio | 8082 | No HLS |
| 1 | live | 8083 | `http://runner.example.org:8083/hls` |
| 2 | live | 8084 | `http://runner.example.org:8084/hls` |

The Manager reserves each instance independently. A mixed instance stays busy
for all task types until its current task and cleanup finish. Each additional
Live needs its own FFmpeg transcode and sufficient CPU/GPU capacity.

### Shared settings and automatic instance values

Run `uv run scripts/configure_live.py` from `runner/` after
installing MediaMTX. It fills the three connection secrets in `runner/.env`;
see [connection setup](../../manager/docs/MEDIAMTX_SETUP.md#6-connect-the-applications-and-verify)
for another host, custom ports or instance names. No per-worker environment file
is created. Keep only non-default operator choices in this same `runner/.env`:

| Settings | Scope |
| --- | --- |
| `LIVE_TRANSCODER`, `LIVE_NVDEC`, `LIVE_CPU_FALLBACK`, `LIVE_VARIANTS` | Shared by all Live instances |
| `LIVE_MEDIAMTX_API_*`, `LIVE_LOCAL_RTMP_URL` | The single MediaMTX service shared by this Runner's instances |
| `LIVE_UPSTREAM_RTMP_URL` | Shared Manager RTMP endpoint and relay credentials |
| `LIVE_POD_*` | Optional Esup-Pod broadcaster API URL and Basic Auth credentials |
| `LIVE_PUBLIC_HLS_BASE_URL` | Template accepting `{port}` and `{instance_id}` |
| `LIVE_HLS_DIRECTORY` | Absolute path accepting `{instance_id}`; each session adds its UUID |
| Relay path and persisted task status | Automatically isolated by session UUID and owning Runner instance |

For direct HTTP playback, the normal Runner addressing is sufficient:

```dotenv
RUNNER_TASK_TYPES="[1x(encoding,studio),2x(live)]"
RUNNER_BASE_PORT=8082
RUNNER_HOST=runner.example.org
```

By default, HLS uses `RUNNER_PROTOCOL://RUNNER_HOST:{port}/hls` and the
`/dev/shm/hls` directory. An explicit `LIVE_PUBLIC_HLS_BASE_URL` takes precedence.
`{port}` is the actual port chosen by the launcher, including its fallback when
a planned port is occupied. For a reverse proxy, use a stable path such as
`https://live.univ.fr/runner-{instance_id}/hls` and route the prefix to that
instance's API. The application computes the URL; it does not create proxy
rules. See the HAProxy example below for a single calculated route across
instances on one host.
Only these literal placeholders are supported, without Python expressions or
format modifiers.

Multiple Live instances must have distinct resolved public base URLs. An
ambiguous static URL fails validation before the launcher starts workers.
A static `LIVE_HLS_DIRECTORY` remains valid: sessions use distinct UUID
subdirectories and each instance serves only its own sessions. `{port}` is not
accepted in filesystem paths so changing the selected port cannot move HLS
files. For new installations, `{instance_id}` makes the layout easier to inspect.

One MediaMTX process supports all these instances through separate
`live/<live_id>` paths. Stopping one Live removes only its path and HLS
files. The Manager retains its own separate MediaMTX. Adding Live groups needs
no additional MediaMTX service, YAML or application `.env` file. An outage of
the shared Runner MediaMTX affects all its Live instances.

The following complete example uses one exclusively Live instance. Replace its
task declaration and count with a form above as needed:

```dotenv
RUNNER_TASK_TYPES=[1x(live)]
RUNNER_INSTANCES=1
RUNNER_BASE_NAME=live-01
RUNNER_HOST=127.0.0.1
RUNNER_BASE_PORT=8082
RUNNER_TOKEN=secret-shared-with-manager
MANAGER_URL=http://manager.internal:8081
STORAGE_DIR=/home/esup-runner/.local/share/esup-runner/live-01
RUNNER_TASK_STATUS_FILE=/home/esup-runner/.local/share/esup-runner/live-01/task-statuses.json
LOG_DIR=/home/esup-runner/.local/state/esup-runner/live-01

LIVE_MEDIAMTX_API_PASSWORD=local-control-secret
LIVE_UPSTREAM_RTMP_URL=rtmp://relay:read-secret@manager.internal:1935
LIVE_LOCAL_RTMP_URL=rtmp://transcoder:local-secret@127.0.0.1:2935
LIVE_PUBLIC_HLS_BASE_URL=https://live.univ.fr/runner-{instance_id}/hls
# Optional broadcaster synchronization with Esup-Pod:
# LIVE_POD_URL=https://pod.univ.fr
# LIVE_POD_USERNAME=pod-api-user
# LIVE_POD_PASSWORD=pod-api-password
ENCODING_TYPE=CPU
```

These absolute paths assume the service account's home is `/home/esup-runner`.
Substitute its real home when different. Write absolute paths in `runner/.env`
so the application and systemd resolve the same locations. The MediaMTX directory
contains only its own `mediamtx.yml` and `mediamtx.env`; it does not need a
`runner.env` file.

`LIVE_TRANSCODER=cpu` (the default) selects libx264; `LIVE_TRANSCODER=nvenc`
selects NVIDIA NVENC. This Live selection is independent of `ENCODING_TYPE`,
which continues to select the backend for other task types. When CPU fallback
is enabled, the first observed NVENC failure switches subsequent attempts to libx264; set
`LIVE_CPU_FALLBACK=false` to keep retrying NVENC instead. The restart budget
continues to bound failures after the initial attempt. NVDEC is optional
(`LIVE_NVDEC=true`) and applies only to `nvenc` mode; it is ignored in `cpu`
mode and after CPU fallback. Without NVDEC, decoding stays on CPU. Scaling
filters run on CPU in both modes; enabling NVDEC keeps decoded frames accessible
to those filters.
GPU selection and `GPU_HWACCEL_DEVICE` use the existing mechanisms. Install an
FFmpeg build with libx264/AAC and, for NVIDIA, h264_nvenc/CUDA. Standard CPU
images do not guarantee NVENC support: see the [GPU guides](gpu/README.md).

When upgrading, replace `LIVE_TRANSCODER=auto` with `cpu` or `nvenc`; `auto` is
no longer accepted. Deployments that previously selected Live NVENC through
`ENCODING_TYPE=GPU` must explicitly set `LIVE_TRANSCODER=nvenc` to retain that
behavior, including when `LIVE_TRANSCODER` was previously omitted.

When changing an existing deployment, finish its sessions before changing HLS
paths, reordering groups or reducing instance counts. Keep the existing status
files until cleanup completes. Increasing the count of a trailing Live group
keeps earlier instance indices unchanged. No migration of active sessions is
performed. Publish to distinct RTMP paths, which the Manager discovers as separate sources, for concurrent
streams; one source can belong to only one active Live session.

Age-based storage cleanup (`MAX_FILE_AGE_DAYS`) is disabled for all groups in an
environment containing Live, since their storage and recovery files may be
shared. Live still removes its HLS files when a session ends. Manage retained
VOD outputs through normal result deletion or a separate VOD environment if
automatic age cleanup is required.

The complete playback path is always
`<LIVE_PUBLIC_HLS_BASE_URL>/<live_id>/master.m3u8`. The public base URL must use
HTTP(S), with no credentials, query string or fragment. It contains no secrets
and can be entirely different from the Runner's internal address. Encode reserved
characters in RTMP URL credentials. The Runner converts the `user:password@`
portion of its two RTMP URL settings into `user`/`pass` query parameters, as
required by MediaMTX for RTMP read connections.

### Optional Esup-Pod broadcaster synchronization

Set `LIVE_POD_URL`, `LIVE_POD_USERNAME` and `LIVE_POD_PASSWORD` together on the
Runner when Esup-Runner should control the matching Esup-Pod broadcaster. The
integration requires Pod 4.5.0's broadcaster `source_id` field ("Live source
identifier") and its REST filter. The configured account must be allowed to GET
`/rest/broadcasters/?source_id=<identifier>` and PATCH the matching broadcaster
at `/rest/broadcasters/<resolved-slug>/`. Credentials stay on the Runner and are
never sent through the Manager task payload.

For a Runner shared by multiple establishments, set
`LIVE_POD_ESTABLISHMENT_NAME` to the exact establishment value received in the
Live task, for example `Université de Montpellier`. The Runner only contacts
this Pod when `task.etab_name` matches that value exactly. A Live for another
establishment does not create a Pod client and sends no request to the
configured Pod. Leave the parameter empty or unset only for the legacy
single-establishment deployment where this Pod account applies to every Live.

The Runner checks every page of the filtered lookup for exactly one broadcaster
whose `source_id` equals the requested identifier. A missing or ambiguous match
causes no PATCH; there is no fallback to matching the Pod slug. Lookup and PATCH
share a five-second timeout, and redirects and response URLs are never followed.
When the source is detected, the Runner sends `{"status":true}`; when the Live
session stops, fails or is recovered after a Runner restart, it sends
`{"status":false}`. A Pod API outage is reported in the Runner logs but does not
hold MediaMTX cleanup or the Runner reservation. The desired status is checkpointed
in the session's existing compact recovery record before delivery. A Live worker
retries pending updates with a ten-second pause between scans, including terminal
sessions and after Runner restart. Each lookup/PATCH keeps its five-second timeout; a retry cycle
performs at most two PATCHes.

Deferred updates consult the Manager's latest session before delivery and again
after acknowledgement. A replaced session's deferred stop is reconciled to the
new session's desired status; establishment filtering also applies to replacements.
If the Manager cannot confirm the current session, the update remains pending.
No Pod credentials or extra queue files are stored in task state. Preserve the
Runner's compact status file until pending updates have been acknowledged.

The Manager forwards the internal `broadcaster_id` as the Live source identifier.
It derives this identifier from the final component of the RTMP source path and
uses that same name as `source_id`. Set the matching broadcaster's `source_id`
in Pod to this value; the Pod slug can be different. With automatic RTMP
discovery, use the optional `etablishment` publication parameter for the
establishment recorded in task statistics and `equipment` for the encoder
identifier; neither is the Pod source identifier. For example:

```text
rtmp://manager.univ.fr:1935/live/lecture-hall-a?user=publisher&pass=...&etablishment=Universit%C3%A9%20de%20Montpellier&equipment=SMP351
```

If `etablishment` is absent or invalid, the Manager uses `unknown` by default
or the configured `LIVE_AUTO_ETAB_NAME` fallback. The Runner compares this
result with `LIVE_POD_ESTABLISHMENT_NAME` when the Pod filter is configured.

The same source path is selected by the Manager's explicit `POST /live/start`
API through `source_id`. Do not configure the integration with a path whose
final component is not a valid Live source identifier: the Runner rejects such
a task when the Pod settings are enabled.

### HLS storage in memory on Linux

HLS output defaults to `/dev/shm/hls` for CPU and GPU workers alike; no
`LIVE_HLS_DIRECTORY` assignment is needed unless you choose another location.
Linux normally mounts `/dev/shm` as tmpfs: playlists and `.ts` segments stay in
memory, reducing disk writes. A shared root is safe for multiple Live workers:
each session owns `/dev/shm/hls/<live_id>/`. If needed, use
`/dev/shm/hls/instance-{instance_id}` to separate worker directories.
The Runner creates missing directories when a stream starts and removes only
that session's directory during cleanup. Do not manually remove a running
session's files or change this path while a Live session is active.

Check the mount and available memory before enabling Live:

```bash
findmnt /dev/shm
df -h /dev/shm
```

Keep `STORAGE_DIR` and `RUNNER_TASK_STATUS_FILE` outside `/dev/shm`: these contain
recovery records. After a machine reboot, HLS files are gone; the Runner uses
its persisted status to clean the interrupted session's relay and mark it
failed. For an automatic session ended specifically by a Runner shutdown, the
Manager can create a replacement when the same RTMP publication is still
active. Otherwise disconnect and republish the encoder, or have an API client
request a new session explicitly. HLS output is not an archive.

Capacity depends on simultaneous sessions, all rendition bitrates, segment
length and playlist size. Allow room beyond the playlist window for segments
being written, deletion overlap and other users of `/dev/shm`. Monitor free
space during a representative workload. tmpfs pages can be swapped if the
host enables swap, so this is not an absolute guarantee of zero disk I/O.
See the [Linux tmpfs reference](https://docs.kernel.org/filesystems/tmpfs.html).

The Live Compose example sets [`shm_size: "256mb"`](https://docs.docker.com/reference/compose-file/services/#shm_size) on the **Runner** container.
For `docker run`, use `--shm-size=256m`. Increase this allocation for your
workload; do not mount a disk-backed volume over `/dev/shm/hls`. Recovery state
still belongs on the persistent Runner data volume.

## ABR profile and timeouts

The three renditions below are built in; set `LIVE_VARIANTS` only to change their
names, dimensions or bitrates. Technical settings use fixed defaults: H.264/AAC,
25 fps, a 50-frame GOP, 2-second segments and six segments per playlist. CPU uses
`veryfast` and NVENC uses `p4`. Source/startup/stall/stop timeouts are 120/60/20/10 s,
monitoring runs every 2 s, the restart budget is three retries after the initial
attempt, and MediaMTX requests time out after 5 s. An enabled CPU fallback is still available after
the first observed NVENC failure; the remaining retry budget applies afterward.
See the [internal defaults and migration](PARAMETERS.md#internal-live-defaults).

```dotenv
LIVE_VARIANTS=[{"name":"high","width":1280,"height":720,"video_bitrate":1100000,"audio_bitrate":128000},{"name":"mid","width":720,"height":406,"video_bitrate":560000,"audio_bitrate":96000},{"name":"low","width":480,"height":270,"video_bitrate":280000,"audio_bitrate":64000}]
```

Bitrates are in bit/s and time values are in seconds. The number of variants is
the length of `LIVE_VARIANTS` (2 to 16, a profile validation limit unrelated to
the number of sessions). Configured dimensions must be even; FFmpeg scales within
these bounds while preserving the aspect ratio. MID `720x406` therefore defines
a bounding box rather than stretching every source to those dimensions. Input
must contain H.264 video and AAC audio. Codec values are validated against this
contract; HEVC, missing audio and passthrough are not supported.

The GOP must not exceed `FPS × segment duration`. Aligned keyframes are forced at
segment boundaries. FFmpeg creates playlists and segments atomically, maintains
a sliding window and deletes old segments. Restarts use increasing sequence
numbers and a discontinuity marker. The master playlist keeps the same URL,
although a brief playback interruption remains possible.

The Live state becomes `running` only when the source is available, FFmpeg is
alive, the master playlist exists, and every variant contains at least two
non-empty segments with a recent playlist. A stalled playlist, missing files or
source loss removes `hls_url` from the status and triggers a limited number of
restart attempts. General task states remain compatible with existing recovery
and callbacks.

## Service and HLS exposure

Follow [MediaMTX setup](MEDIAMTX_SETUP.md) for a native or Docker installation
of version >= 1.21.1, shared with the Manager. Reuse an existing installation on the
same machine, with one configuration and service/container shared by this
Runner deployment’s Live instances, using [`production/mediamtx-live.yml`](../production/mediamtx-live.yml). Paths are
created on demand and deleted at the end of the session. Only the associated
Runner needs access to the MediaMTX API; it does not need to be exposed to the
Manager.

### Use the main Runner service with a mixed or grouped `.env`

Set `RUNNER_TASK_TYPES` and `LIVE_*` in `/opt/esup-runner/runner/.env`. The existing
`esup-runner-runner.service` then starts all declared groups; a separate Live
application service is unnecessary. MediaMTX remains a separate service named
`esup-runner-mediamtx@runner.service`. Its instance name identifies the media
configuration and does not determine the application's task types or port.

For this deployment, add a drop-in with `systemctl --user edit esup-runner-runner.service`:

```ini
[Unit]
After=esup-runner-mediamtx@runner.service
Wants=esup-runner-mediamtx@runner.service

[Service]
KillMode=control-group
TimeoutStopSec=45
```

Adapt the MediaMTX name if needed; omit its dependency when MediaMTX runs in
Docker. `KillMode=control-group` stops all workers and FFmpeg processes when the
service is stopped. Once active tasks have finished, reload and restart:

```bash
systemctl --user daemon-reload
systemctl --user enable --now esup-runner-mediamtx@runner.service
systemctl --user restart esup-runner-runner.service
```

If a separate `esup-runner-live@live-01.service` was previously enabled, finish
its sessions and stop/disable it before switching the same port, HLS root and
MediaMTX to the grouped service. Preserve its recovery files until cleanup is
complete. On the Manager, update the code for selection of mixed task types too.

Create the configured HLS directory with permissions for the service account.
Follow the shared guide's [user service instructions](../../manager/docs/MEDIAMTX_SETUP.md#4-start-the-native-systemd-service)
for the MediaMTX template and boot/logout persistence with lingering. When
MediaMTX runs in Docker, start its container before the Runner application.

Application recovery can identify surviving FFmpeg processes after a crash of
the API process alone. On restart, cleanup takes priority, followed by an
explicit `failed` state. The Manager may replace an automatic session after a
Runner shutdown or forced restart once cleanup completes and its RTMP source
is still active. An explicit stop remains effective. Otherwise republish the
RTMP source for a new automatic session, or have the client request one explicitly.

The Runner serves files through `/hls/<live_id>/...`. Expose only this prefix on
the public frontend and keep `/task`, `/runner` and `/docs` on the internal
network. Use a readable, stable public prefix containing the Runner's instance
index, for example
`https://live.univ.fr/runner-3/hls/<live_id>/master.m3u8`. The index is the
Runner instance index, not its position among Live instances; it starts at 0
and includes VOD instances. Pod uses the exact `hls_url` returned by the Manager.

Nginx example, with the upstream and public prefix adjusted as needed:

```nginx
location /runner-1/hls/ {
    proxy_pass http://127.0.0.1:8083/hls/;
    proxy_buffering off;
    proxy_cache off;
    access_log off;
    add_header Access-Control-Allow-Origin "https://pod.univ.fr" always;
}
```

For the second Live in the three-instance example, add an equivalent
`/runner-2/hls/` location targeting port `8084`.

### HAProxy with one rule for multiple instances

When all Runner instances are on the same host and use consecutive ports,
HAProxy can derive the destination port from the instance index. This avoids
one frontend/backend routing rule per Live instance. For example, with
`RUNNER_BASE_PORT=8082`, `/runner-2/hls/<live_id>/...` is rewritten to
`/hls/<live_id>/...` and sent to port `8084`.

When using HAProxy, explicitly set `FORWARDED_ALLOW_IPS` in `runner/.env`, and
in `manager/.env` if the Manager is also proxied:

```dotenv
# Replace with the HAProxy source IP as seen by the application.
FORWARDED_ALLOW_IPS=10.0.0.10
```

This is a native Uvicorn/Gunicorn setting. The launchers load it from `.env`
before starting the servers. List the actual proxy source IPs, comma-separated
for redundant proxies; avoid `*`. The loopback-only default does not trust a
remote HAProxy. Restart the affected services completely after any change.
See [proxy configuration](CONFIGURATION.md#haproxy-and-other-reverse-proxies)
for address selection and the role of the forwarding headers.

The following is the relevant part of an HTTP-mode frontend. Merge it into the
existing HTTPS frontend and replace `be_application` with the deployment's
normal backend. This example assumes HAProxy terminates HTTPS and receives
client connections directly:

```haproxy
frontend https
    bind :443 ssl crt /etc/haproxy/certs/live.pem
    mode http

    # Example for 12 configured instances (indices 0 through 11).
    # Restrict this ACL to the actual instance indices in this deployment.
    acl live_hls path_reg ^/runner-(0|[1-9]|1[01])/hls/
    use_backend be_runner_hls if live_hls
    default_backend be_application

backend be_runner_hls
    mode http
    # Set forwarding headers from this trusted HTTPS proxy.
    http-request set-header X-Forwarded-Proto https
    http-request set-header X-Forwarded-For %[src]
    # Fix the destination IP to this Runner host.
    http-request set-dst ipv4(127.0.0.1)
    # Port = RUNNER_BASE_PORT + instance index.
    http-request set-var(txn.runner_port) path,field(2,/),field(2,-),add(8082)
    http-request set-dst-port var(txn.runner_port)
    # Keep the Runner's original /hls/... route.
    http-request set-path %[path,regsub(^/runner-[0-9]+,)]
    server runner 0.0.0.0:0
```

Validate the merged configuration before reloading HAProxy:

```bash
haproxy -c -f /etc/haproxy/haproxy.cfg
```

Change `8082` in `add(8082)` to the Runner's `RUNNER_BASE_PORT`. Keep
`server runner 0.0.0.0:0` so HAProxy uses the destination IP and port set by
`set-dst` and `set-dst-port`, as described in the
[HAProxy configuration manual](https://docs.haproxy.org/3.2/configuration.html#http-request%20set-dst-port).
Restrict the ACL to instance indices that really exist;
the example allows 0–11. These indices include VOD instances, and each instance
listens on `RUNNER_BASE_PORT + instance_id`. Keep the Runner ports reachable
only from HAProxy. If HAProxy is on another host, replace `127.0.0.1` in
`set-dst ipv4(127.0.0.1)` with the Runner host's private IPv4 address and apply
equivalent network filtering.

This arithmetic mapping assumes all instances are on one host, the ports are
consecutive, and the configured ports are available. If instances use different
hosts or non-consecutive ports, use a single backend with a maintained
instance-to-address/port map instead. Do not route non-HLS paths through this
backend. Keep HLS playlist caching disabled and configure CORS for Pod's origin
at the Runner; avoid adding a second conflicting CORS header at HAProxy.

The server returns HLS/TS MIME types and `no-store` playlists. Do not enable
persistent playlist caching. Adjust CORS on the Runner and frontend to avoid
duplicate headers. The HAProxy example above provides the same prefix routing
without per-instance locations. The frontend must enforce viewer access control
for private streams; a session UUID is not an authorization mechanism.
Successful HLS requests are filtered out of the Uvicorn access log; errors
remain visible. Application logs track transitions rather than individual
segments.

## Stop and recovery

Stopping is persistent and idempotent. It interrupts FFmpeg with SIGINT, then
SIGKILL if the timeout expires; the MediaMTX relay and session directory are then
removed. Only after cleanup is confirmed does the Runner report availability and
notify `/task/completion`. A MediaMTX API or filesystem failure retains the
reservation and retries cleanup.

If the Runner's MediaMTX Control API becomes temporarily unavailable during an
active session, the Runner stops FFmpeg, removes stale HLS output, and keeps the
same session reserved while it waits for the API to recover. It then recreates a
missing relay path and resumes transcoding under the same `live_id` and HLS URL.
This automatic recovery applies to transient connection and server errors; an
authentication, ownership or other configuration error still fails the session.
An explicit stop remains in effect and cleanup is retried when MediaMTX returns.

On a mixed instance, stop requests use the persisted task type to select Live
cleanup or VOD process termination. Pending Live cleanup after a restart also
defers automatic retries of older failed VOD tasks; it cannot free an instance
still occupied by another running task.

The Manager reconciles lost callbacks through `/task/status`. The compact Live
status file retains the terminal state needed after a restart. Do not delete
this file during a session or share it between instances. HLS paths are isolated
by UUID; directory traversal and symlinks pointing outside the session directory
are rejected.

## Manual validation

Configure the `amphi-a` source on the Manager, then publish:

```bash
ffmpeg -re -f lavfi -i 'testsrc2=size=1280x720:rate=25' \
  -f lavfi -i 'sine=frequency=1000:sample_rate=48000' \
  -c:v libx264 -preset ultrafast -tune zerolatency -g 50 \
  -c:a aac -f flv "$PUBLISH_RTMP_URL"
```

`PUBLISH_RTMP_URL` contains the **Manager** address/port, path and credentials
matching the MediaMTX configuration. Keep this variable out of shared logs.
When Pod synchronization is enabled, include `equipment=<equipment-name>` and
make the final RTMP path component equal the Pod broadcaster's `source_id`.
With `LIVE_ENABLED=true` and `LIVE_AUTO_START=true` on the Manager, publication
alone starts HLS. All services must already be running. Open **Live** in the
Manager administration, or use an authenticated
`GET /live/sources/amphi-a` to discover the `live` object and its playback URL.
No `POST /live/start` is needed. See
[automatic operation](../../manager/docs/LIVE.md#automatic-rtmp-to-hls-operation).
Once `live.state=running`, use the returned `live.hls_url` as `HLS_URL`, then run:

```bash
curl -fsS "$HLS_URL"
ffprobe -v error -read_intervals '%+2' -show_streams "$HLS_URL"
vlc "$HLS_URL"
```

With Pod's hls.js player, check all three quality levels, quality switching,
CORS and HTTPS. Stop the synthetic source and check that its automatic session
ends and releases the Runner. Republish to get a new session and HLS URL.
An explicit stop in administration while the publisher remains connected must
stay effective until republication or **Restart session**. After cleanup, use
that action to verify that HLS returns under a new session URL without stopping
the encoder. Kill the Runner's transcoding FFmpeg process
to test bounded restarts. Temporarily make the
MediaMTX API unavailable, then request a stop: the Runner must remain busy until
control is restored and cleanup completes.

Repeat with four distinct Runners and five configured sources: the first four
Live sessions must use different Runners; the fifth published source waits for
capacity and starts automatically when one becomes free. For the optional
API-driven mode (`LIVE_AUTO_START=false`), an extra start request receives `503`.
After a session stops, its URL is no longer advertised and the Runner becomes reusable.
To automate the complete CPU flow, use
[`scripts/live_smoke.py`](../../scripts/live_smoke.py), described in the Manager
guide. Add `--automatic` to test publication-triggered starts, disconnect,
republication and Manager restart. Add `--crash-recovery` to kill a Runner during a Live session, restart it
with the same state, and check that the orphan FFmpeg process is terminated and
the reservation is released.

The pytest tests mock the GPU, FFmpeg and MediaMTX. A real CPU test does not
validate NVENC/NVDEC: run the same procedure with `LIVE_TRANSCODER=nvenc`, enable
`LIVE_NVDEC=true` separately to validate hardware decoding, then test fallback
by disabling the GPU, before production use.
