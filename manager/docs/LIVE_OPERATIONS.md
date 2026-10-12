# Live operations runbook

This runbook is for operators who already have the Manager, Runner and their
MediaMTX services installed. It focuses on the actions that matter during a
broadcast: start, observe, stop, restart, inspect logs and recover from a
failure.

For installation, use [Live installation](LIVE_INSTALLATION.md). For the full
API and configuration reference, use [Manager Live](LIVE.md) and
[Runner Live](../../runner/docs/LIVE.md).

## The normal operating picture

1. An encoder publishes authenticated H.264/AAC RTMP to the **Manager
   MediaMTX**.
2. The Manager discovers the publication on its next scan, normally within
   five seconds, and reserves an available Runner instance.
3. The Runner creates a temporary relay in the **Runner MediaMTX** and starts
   FFmpeg.
4. The Runner reports `running` only after the master playlist and every
   configured variant have recent segments.
5. The Manager administration and API expose the exact `hls_url` to the player.
6. When publishing stops, cleanup removes FFmpeg, the relay and the temporary
   HLS directory before the Runner becomes available again.

A Live session occupies one complete Runner instance. If all Live instances
are busy, the encoder may remain connected while the Manager retries allocation;
the stream is not buffered while it waits.

## Daily procedure

### Start a broadcast

1. Confirm the four relevant services are healthy: Manager MediaMTX, Runner
   MediaMTX, Manager application and Runner application.
2. Start the encoder with its Manager RTMP URL and `publisher` credentials.
3. Open **Live** in the Manager administration.
4. Find the source and wait for `running`, `hls_ready` and a playback link.
5. Give the player the exact displayed URL. Do not derive a URL from the Runner
   name, port or source key.

With the default `LIVE_AUTO_START=true`, no API call is needed. The optional
client-controlled workflow is documented in [Manager Live API](LIVE.md#start-a-session).

### Read the Live administration page

Open `/admin/live` with an administrator account. Use the search and state
filters to find a broadcaster, source, event, session, Runner or establishment.
Broadcaster search accepts partial names and is case-insensitive. Expand a
session for its timestamps, source observations, bitrate, codecs, HLS status,
Runner and error message.

The first session column shows the Broadcaster; source/event identifiers are
available in **Session details**, without a duplicate Broadcaster field. Codecs
and resolutions share one line.
The **Start / end** column after **Runner** shows session creation and confirmed
termination in `YYYY-MM-DD HH:MM:SS` format. These include waiting and cleanup;
active sessions show **Ongoing**, and missing timestamps show **Unknown**.

Codecs and resolution are the last known non-empty values for that session.
Temporary missing tracks and terminal cleanup do not erase them. Signal presence
and counters still follow the latest report; retained track metadata does not
confirm an active source. A new session starts with unknown tracks until observed.

The page refreshes automatically. Pause or focus the details while investigating
an incident. Reports older than the freshness window are marked stale and their
playback link is hidden; the last report remains useful for diagnosis.

**Active and recent sources** shows current RTMP publishers, sources with
retained sessions and declared broadcasters. Historical sources disappear when
their last session is removed according to `CLEANUP_TASK_FILES_DAYS` (or deleted
manually). The private source registry and publication checkpoints remain
unchanged. If RTMP publications cannot be verified, all known sources are
temporarily displayed with a notice; this does not confirm that they are active.

Each source row separates current RTMP signal from the latest retained session
state and date. Establishment/equipment values describe that session and are
marked historical when it has ended. Missing values are shown as **Not provided**.
Use **View sessions** to reset filters and show that source's retained sessions,
or copy the directly displayed broadcaster name. There is no separate full
source ID panel. If clipboard access fails, the visible name is selected for
manual copying and refresh is paused until the user resumes it.
The first **Broadcaster** column is the source ID, taken from the final RTMP path
component, even without a retained session. A second authenticated publisher on
the same path replaces the first. The row shows the replacement's latest session
after the previous automatic session finishes cleanup; both sessions remain in
history. Existing MediaMTX configurations need `overridePublisher: true` on
publisher paths, as in the Manager template. See [Live configuration](LIVE.md#manager-configuration).

Available actions:

| Action | Use it when | Result |
| --- | --- | --- |
| Stop | The broadcast must end while the encoder is still connected | Persists a stop intent, removes the HLS URL and cleans up the session. |
| Restart session | The previous session is terminal but the same encoder is still publishing | Creates a new `live_id` and HLS URL without asking the encoder to reconnect. |
| Live diagnostics | The source is rejected, missing or failing authentication | Shows recognized, sanitized MediaMTX and Manager events. |

Do not mark a Runner available by hand while a session is `stopping`. The
reservation protects cleanup and prevents two FFmpeg processes from using the
same instance.

### End a broadcast

Normally, stop the encoder and wait for the automatic session to become
`completed`. The Manager observes the RTMP disconnection and requests cleanup.
If an immediate stop is required, use **Stop** in the Live page. A stopped
automatic session stays stopped for the same RTMP connection until the encoder
disconnects and republishes or an operator uses **Restart session**.

The old HLS URL is not reused. A republish or restart always has a new
`live_id` and a new HLS URL.

## Understand the states

| State | Meaning and operator action |
| --- | --- |
| `pending` | The Manager saved the intent and is reserving a Runner. Check capacity and storage if it persists. |
| `waiting_source` | The relay exists but no compatible H.264/AAC source is available. Check encoder publishing and MediaMTX permissions. |
| `starting` | FFmpeg is starting or recovering. Check the Runner logs and HLS directory if it persists. |
| `running` | All HLS variants are ready. Use the displayed `hls_url`. |
| `stopping` | Stop or cleanup is still in progress. Keep the Runner reserved and restore MediaMTX/filesystem access. |
| `completed` | Normal cleanup finished and the Runner is reusable. |
| `failed` | Cleanup finished with an error. Read the reason, fix the cause, then republish or restart. |

The general task status stays `running` for all active Live states. A Live
session is not a VOD result and does not create an archive or a result manifest.

## Logs and diagnostics

### Administration logs

Select **Live diagnostics** in the Live page or the general Logs page. The
projection recognizes safe markers such as `RTMP_AUTH_FAILED`,
`SOURCE_PUBLISHED`, `SOURCE_DISCOVERY_FAILED`, `RTMP_CONNECTION_FAILED` and
`MEDIA_LOG_UNAVAILABLE`. It removes stream keys, credentials and raw media
paths. It is a recent diagnostic view, not a replacement for system logs.

For native MediaMTX authentication failures, the Manager must be configured
with the actual user unit, for example:

```dotenv
LIVE_MEDIAMTX_LOG_UNIT=esup-runner-mediamtx@manager.service
```

MediaMTX must use `logLevel: info`; otherwise refused RTMP publications may not
be visible. Apply this configuration between broadcasts because changing the
MediaMTX logging configuration reloads its listeners.

### Native services

Use the exact instance name configured on the host:

```bash
systemctl --user --no-pager status esup-runner-manager.service
systemctl --user --no-pager status esup-runner-runner.service
systemctl --user --no-pager status esup-runner-mediamtx@manager.service
systemctl --user --no-pager status esup-runner-mediamtx@runner.service

journalctl --user -u esup-runner-manager.service -n 100 --no-pager
journalctl --user -u esup-runner-runner.service -n 100 --no-pager
journalctl --user -u esup-runner-mediamtx@manager.service -n 100 --no-pager
journalctl --user -u esup-runner-mediamtx@runner.service -n 100 --no-pager
```

For one session, search the Manager and Runner logs by its `live_id`. Search
the Manager MediaMTX log by the time and source identifier, never by copying a
password or a complete RTMP URL into a ticket.

### Docker Compose

```bash
cd /opt/esup-runner/deploy/live
docker compose -p live-a ps
docker compose -p live-a logs --tail=100 manager runner
docker compose -p live-a logs --tail=100 manager-media runner-media
```

The Compose stack mounts the Manager MediaMTX log read-only into the Manager.
For a custom container deployment, set `LIVE_MEDIAMTX_LOG_FILE` to a regular
readable file and rotate it outside the application.

## Restart and outage behavior

Plan intentional restarts between broadcasts whenever possible. The following
table describes the supported behavior:

| Component restarted | What happens to an active Live | What to do afterwards |
| --- | --- | --- |
| Manager application | Tasks, reservations and publication checkpoints are reloaded. The Runner media process is not directly killed. The Manager resumes source reconciliation after startup. | Wait for `/admin/live` to refresh. Check that the source is still publishing and that there is no duplicate session. |
| Runner application | The active FFmpeg session is stopped and cleaned up. The session becomes `failed` with `Runner stopped`, or `Runner restarted; session stopped safely` after forced restart recovery. The Runner stays reserved until cleanup finishes, including while MediaMTX is unavailable. | If the same automatic publication is still active and no explicit stop was requested, the Manager creates one replacement with a new `live_id` and URL after cleanup. Otherwise republish or use **Restart session**. |
| Manager MediaMTX | Encoder connections can drop and reconnect. While its Control API is unavailable, the Manager treats observations as uncertain and does not infer a disconnection from that error alone. | Check the MediaMTX unit, credentials and ports, then reconnect the encoder if it did not reconnect itself. |
| Runner MediaMTX | A transient Control API outage stops FFmpeg, removes stale HLS files and keeps the session reserved in `waiting_source`. When control returns, the Runner recreates the relay if needed and resumes under the same `live_id` and HLS URL. Configuration/authentication errors can fail the session; an explicit stop keeps cleanup pending until control returns. | Restore the service and wait for `running` before reusing the returned playback URL. If a stop was requested or the session failed, wait for terminal cleanup instead. Do not release the Runner manually. |
| Host reboot | HLS files in `/dev/shm` disappear. Persisted task state survives; Runner recovery terminates orphan work and reconciles the session. | Confirm all services start at boot. Automatic sessions interrupted specifically by Runner shutdown may be replaced if the publication is still active. |

An automatic replacement always has a new `live_id` and HLS URL. Update Pod or
the player from the new status; do not keep polling the old playlist.

## Troubleshooting by symptom

### The encoder cannot publish

Check, in this order:

1. The Manager MediaMTX service and its RTMP bind address.
2. Firewall and routing from the encoder to the Manager host.
3. The `publisher` username/password and the allowed `live/<path>` pattern.
4. The encoder codec: H.264 video and AAC audio are required.
5. **Live diagnostics** and the Manager MediaMTX journal.

An authentication error is handled inside MediaMTX; the Manager cannot create a
Live session until MediaMTX accepts the publication.

### The encoder publishes but no session appears

Check `LIVE_ENABLED=true`, `LIVE_AUTO_START=true`, the Manager Control API
credentials, the source path format and the Manager data directory. Then check
that at least one registered Runner advertises `live` and is not busy with
another task. If the source was stopped or failed earlier, disconnect and
republish after correcting the cause.

### The session is `waiting_source`

The Runner has a relay but cannot see a ready compatible source. Verify that
the Runner can reach the Manager RTMP address, that the Manager `relay` account
is allowed to read the path, and that the source contains H.264/AAC. Check the
Runner MediaMTX Control API and the relay path; never expose the relay URL in a
public report.

### The session is `starting` or the player returns 404

Check the Runner application log for FFmpeg start, the selected CPU/GPU backend,
free `/dev/shm` space and HLS directory permissions. Confirm that the frontend
routes the exact `hls_url` to the correct Runner instance and does not cache
playlists. HLS is advertised only after all variants are ready.

### The session is `stopping` for too long

Check the Runner MediaMTX API, the HLS directory and the Runner log. Restore
access and wait for cleanup retries. A persistent `stopping` state means the
Runner is intentionally still reserved; do not delete status files or restart
the source repeatedly.

### The session failed after a Runner restart

This is expected for the interrupted session. If the source is still publishing
and the failure says `Runner stopped` or `Runner restarted; session stopped safely`,
wait for cleanup and the Manager's automatic replacement. An explicit stop stays
effective. Otherwise republish the encoder, or wait for terminal cleanup and
use **Restart session**. Always replace the player's old HLS URL.

### The Manager or Runner cannot connect to MediaMTX

Run the local Control API check from the installation guide, then verify the
selected port, listener address, username/password and `ips` permissions in the
correct instance YAML. In Compose, use `manager-media` and `runner-media`, not
`127.0.0.1`, from inside application containers.

### HLS works but the Pod broadcaster status is incorrect

Check the Runner log for Pod activation/deactivation failures, the configured Pod
credentials and establishment filter. Failed status updates are checkpointed in
the Runner's compact task state and retried with a ten-second pause between scans,
including after restart and after the media session has ended. Pending Pod updates
do not keep the Runner occupied once media cleanup is complete.

The retry checks the Manager's current source/session before changing Pod, so
restore Manager and MediaMTX Control API access too if observation is unavailable.
Preserve the Runner status file; the internal `pod_status_pending` field is visible
through the authenticated Runner `/task/status/<live_id>` endpoint. A successful
retry logs `Live Pod status synchronized` and clears that field.

## Concrete incident: "the auditorium is publishing, but the player is black"

Use this short sequence during a live event:

1. In **Live**, record the source, `live_id`, state and last error.
2. If no source is visible, inspect `SOURCE_PUBLISHED`,
   `RTMP_AUTH_FAILED` and the Manager MediaMTX journal.
3. If the state is `waiting_source`, test Runner-to-Manager reachability and
   the `relay` permission.
4. If the state is `starting`, inspect FFmpeg/backend errors, H.264/AAC input,
   `/dev/shm` capacity and the HLS route.
5. If the state is `running` but playback is black, request the exact HLS URL
   with `curl -fsS`, then test it with `ffprobe`; a 404 is a routing/cache
   problem, while an invalid playlist points to the Runner/HLS path.
6. If the session is terminal and the encoder remains connected, use **Restart
   session**. Publishers do not need to restart for this action.
7. If a Runner is unavailable, keep the encoder connected while checking the
   Runner service and capacity. The Manager retries allocation; it does not
   buffer the stream.

Record the final state, the replacement `live_id` if any, the first failing
component and the relevant log timestamps. Do not include RTMP credentials.
