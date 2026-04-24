#!/usr/bin/env python3
"""
Async task execution check: submit one task to the ESUP Runner Manager.

Goal of this script
------------------
This file is intentionally *simple* and *very* commented so that anyone
cloning the project can do a quick manual test.

Configuration source
--------------------
This script auto-loads:
1) manager URL from `app.core.config` (`MANAGER_URL`)
2) token from the first configured `AUTHORIZED_TOKENS__*` value in `.env`

Optional overrides are still available through:
- `RUNNER_API_TOKEN`
- `RUNNER_MANAGER_URL`

What this script does
---------------------
1) Calls `GET /api/version` to confirm authentication works.
2) By default, runs one `encoding` task.
3) With `--with-transcription-translation`, additionally runs:
   - one `transcription` task in French (`language=fr`)
   - one `transcription` task targeting English subtitles (`language=en`)
4) For each task, polls `GET /task/status/{task_id}` until terminal state,
   then fetches `GET /task/result/{task_id}`.

How to run
----------
    uv run scripts/check_pipeline_tasks.py
    uv run scripts/check_pipeline_tasks.py --with-transcription-translation
    uv run scripts/check_pipeline_tasks.py --with-transcription-translation --max-wait-seconds 1800

Or with optional environment overrides:
    RUNNER_API_TOKEN=... RUNNER_MANAGER_URL=http://manager-host:8081 uv run scripts/check_pipeline_tasks.py
    RUNNER_SOURCE_URL=... uv run scripts/check_pipeline_tasks.py --with-transcription-translation

Notes
-----
- The Manager API expects a token configured in the manager environment
  (`AUTHORIZED_TOKENS__…` variables).
- Auth header can be either:
    * `X-API-Token: <token>` (used in this script)
    * or `Authorization: Bearer <token>`
- `notify_url` is required by the current API schema. We use a public endpoint
  that returns HTTP 200 on POST to avoid leaving the task in a "warning" state
  when the manager tries to call the callback.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlsplit

import httpx

MANAGER_ROOT = Path(__file__).resolve().parents[1]
if str(MANAGER_ROOT) not in sys.path:
    sys.path.insert(0, str(MANAGER_ROOT))

from app.core._check_output import format_status

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# You typically do NOT need to change the values below for a quick manual test.

# The task type must be supported by at least one available runner.
# Common values: "encoding", "transcription", "studio" (depending on your runners).
TASK_TYPE_ENCODING = "encoding"
TASK_TYPE_TRANSCRIPTION = "transcription"

# A small public media file with spoken French.
# Important: the manager validates URLs and will reject private/loopback hosts.
#
# Override for local environments:
#   RUNNER_SOURCE_URL=https://your-host/path/file.webm
#   SOURCE_FILE=https://your-host/path/file.webm   (legacy alias)
#
# We keep multiple defaults to reduce brittleness when one remote URL is
# temporarily unavailable.
DEFAULT_SOURCE_URLS = [
    "https://video.umontpellier.fr/media/videos/test.mp4",
    "https://upload.wikimedia.org/wikipedia/commons/7/79/WIKITONGUES-_Clara_speaking_French.webm",
    (
        "https://upload.wikimedia.org/wikipedia/commons/transcoded/7/79/"
        "WIKITONGUES-_Clara_speaking_French.webm/"
        "WIKITONGUES-_Clara_speaking_French.webm.360p.vp9.webm"
    ),
]

# For encoding/studio tasks, nested params such as `rendition` must be sent as JSON strings.
ENCODING_SMOKE_RENDITION = json.dumps(
    {"360": {"resolution": "640x360", "encode_mp4": True}},
    separators=(",", ":"),
)

# Required by the API schema.
# The manager will POST to this URL when the task completes.
# If the callback returns non-200, the task can temporarily go into "warning".
NOTIFY_URL = "https://httpbin.org/status/200"

# Polling interval (seconds) for manual testing.
POLL_SECONDS = 2

# Safety timeout so the script doesn't run forever.
DEFAULT_MAX_WAIT_SECONDS = 300

# Optional: automatically download the first file listed in the manifest.
DOWNLOAD_FIRST_FILE = True

# Where to save the downloaded file (default: current directory).
OUTPUT_DIR = Path(".")


class SourceDownloadError(RuntimeError):
    """Raised when a task fails due to source media download/access issues."""


def _repo_root() -> Path:
    """Return the manager project root directory."""
    return MANAGER_ROOT


def _ensure_import_path() -> None:
    """Ensure manager root is available in ``sys.path`` for local imports."""
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def _load_config() -> Any:
    """Load the shared manager config instance (and .env) from app.core.config."""
    _ensure_import_path()
    from app.core.config import get_config  # type: ignore

    return get_config()


def _first_configured_token(config: Any) -> Optional[str]:
    """Return the first configured authorized token, if any."""
    tokens = getattr(config, "AUTHORIZED_TOKENS", {}) or {}
    for token in tokens.values():
        token_text = str(token or "").strip()
        if token_text:
            return token_text
    return None


def _load_runtime_settings() -> tuple[str, str]:
    """Resolve manager URL + token from config/.env, with optional env overrides."""
    config = _load_config()
    manager_url = str(getattr(config, "MANAGER_URL", "") or "").strip()
    token = _first_configured_token(config) or ""

    # Keep CLI-friendly overrides for ad-hoc testing.
    manager_url = (os.getenv("RUNNER_MANAGER_URL") or manager_url).strip()
    token = (os.getenv("RUNNER_API_TOKEN") or token).strip()

    if not manager_url:
        raise SystemExit(
            "MANAGER_URL is empty in configuration. Set MANAGER_PROTOCOL/MANAGER_HOST/MANAGER_PORT in manager/.env."
        )
    if not token:
        raise SystemExit(
            "No API token found. Configure at least one AUTHORIZED_TOKENS__* entry in manager/.env "
            "or set RUNNER_API_TOKEN."
        )
    return manager_url, token


def _normalize_base_url(base_url: str) -> str:
    """Normalize the base URL so joining paths is predictable."""
    return base_url.rstrip("/")


def _auth_headers(token: str) -> dict[str, str]:
    """Build auth headers for the Manager API.

    We use `X-API-Token` because it's straightforward and avoids the "Bearer" prefix.
    """
    return {
        "Accept": "application/json",
        "X-API-Token": token,
    }


def _safe_filename(file_path: str) -> str:
    """Convert a manifest `file_path` into a safe local filename.

    The manifest paths are *relative* paths, but they may contain subfolders.
    For a quick manual test, we flatten the path into a single filename.
    """
    # Replace slashes by underscores, then drop any weird characters.
    flattened = file_path.replace("/", "_").replace("\\", "_")
    flattened = re.sub(r"[^A-Za-z0-9._-]+", "_", flattened).strip("._")
    return flattened or "download.bin"


def _is_placeholder_token(token: str) -> bool:
    """Detect missing token values."""
    stripped = token.strip()
    return not stripped


def _connect_error_help(base_url: str) -> str:
    """Build a readable troubleshooting message for connection failures."""
    parts = urlsplit(base_url)
    host = parts.hostname or "<unknown-host>"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return (
        f"Cannot connect to manager at {base_url}\n"
        "Checks:\n"
        f"- If this script runs on another server, do not use 127.0.0.1; use the real manager host/IP (current host={host}).\n"
        f"- Ensure TCP port {port} is reachable from this machine (firewall, reverse-proxy, bind address).\n"
        f"- Quick test: curl -i {base_url}/api/version -H 'X-API-Token: <token>'"
    )


async def _raise_for_http_error(response: httpx.Response) -> None:
    """Provide a helpful error message instead of a generic exception."""
    if response.status_code < 400:
        return

    # Try to extract JSON `detail` if the API returns it.
    detail: str | None = None
    try:
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
            detail = payload["detail"]
    except Exception:
        detail = None

    msg = f"HTTP {response.status_code} calling {response.request.method} {response.request.url}"
    if detail:
        msg += f" -> {detail}"
    else:
        body = (response.text or "").strip()
        if body:
            msg += f" -> {body}"
    raise RuntimeError(msg)


async def check_auth(client: httpx.AsyncClient, base_url: str, token: str) -> dict[str, Any]:
    """Step 1: sanity-check that the manager is reachable and the token is valid."""
    url = f"{base_url}/api/version"
    try:
        resp = await client.get(url, headers=_auth_headers(token))
    except httpx.ConnectError as exc:
        raise SystemExit(_connect_error_help(base_url)) from exc
    await _raise_for_http_error(resp)
    return resp.json()


async def get_runners_overview(
    client: httpx.AsyncClient, base_url: str, token: str
) -> dict[str, Any]:
    """Fetch a lightweight runners overview to help with troubleshooting.

    This calls the manager endpoint:
      GET /api/runners
    """
    url = f"{base_url}/api/runners"
    resp = await client.get(url, headers=_auth_headers(token))
    await _raise_for_http_error(resp)
    payload = resp.json()
    return payload if isinstance(payload, dict) else {"runners": payload}


def _print_runners_overview(runners_payload: dict[str, Any]) -> None:
    """Pretty-print the `/api/runners` payload for quick debugging."""
    runners_list = runners_payload.get("runners")
    if isinstance(runners_list, list):
        print(f"Runners returned by manager: {len(runners_list)}")
        for r in runners_list:
            if isinstance(r, dict):
                rid = r.get("id")
                rurl = r.get("url")
                rstatus = r.get("status")
                age = r.get("age_seconds")
                print(f"- id={rid!r} status={rstatus!r} age_seconds={age!r} url={rurl!r}")
        return

    print(f"Runners payload: {runners_payload!r}")


def _is_no_runners_available_error(message: str) -> bool:
    """Heuristic for the common 503 error returned by the manager."""
    return "HTTP 503" in message and "No runners available" in message


def _resolve_source_urls() -> list[str]:
    """Resolve source URL candidates from env or defaults."""
    override = (os.getenv("RUNNER_SOURCE_URL") or os.getenv("SOURCE_FILE") or "").strip()
    if override:
        return [override]

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in DEFAULT_SOURCE_URLS:
        normalized = (candidate or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Run manager end-to-end task checks. "
            "Default: encoding only. Optional: also transcription + translation."
        )
    )
    parser.add_argument(
        "--with-transcription-translation",
        action="store_true",
        help=(
            "Also run transcription checks on the same source media: "
            "French subtitles then English subtitles."
        ),
    )
    parser.add_argument(
        "--max-wait-seconds",
        type=int,
        default=None,
        help=(
            "Override per-task timeout. "
            "Default: 300s for encoding-only mode, 1200s for transcription/translation mode."
        ),
    )
    return parser.parse_args()


def _build_task_request(
    task_type: str, source_url: str, parameters: dict[str, Any]
) -> dict[str, Any]:
    """Build one task payload for POST /task/execute."""
    return {
        "etab_name": "Quick manual test",
        "app_name": "check_pipeline_tasks.py",
        "app_version": "0",
        "task_type": task_type,
        "source_url": source_url,
        "affiliation": "manual-test",
        "parameters": parameters,
        "notify_url": NOTIFY_URL,
    }


def _build_task_plan(with_transcription_translation: bool, source_url: str) -> list[dict[str, Any]]:
    """Build ordered task checks to execute."""
    plan: list[dict[str, Any]] = [
        {
            "label": "Encoding check",
            "request": _build_task_request(
                TASK_TYPE_ENCODING,
                source_url,
                {
                    # Keep a minimal, valid encoding ladder for a quick smoke test.
                    "rendition": ENCODING_SMOKE_RENDITION,
                },
            ),
            "download_first_file": True,
        }
    ]

    if with_transcription_translation:
        plan.append(
            {
                "label": "Transcription FR check",
                "request": _build_task_request(
                    TASK_TYPE_TRANSCRIPTION,
                    source_url,
                    {"language": "fr", "format": "vtt"},
                ),
                "download_first_file": True,
            }
        )
        plan.append(
            {
                "label": "Translation EN check",
                "request": _build_task_request(
                    TASK_TYPE_TRANSCRIPTION,
                    source_url,
                    {"language": "en", "format": "vtt"},
                ),
                "download_first_file": True,
            }
        )

    return plan


async def submit_task_or_exit(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    task_request: dict[str, Any],
) -> str:
    """Submit a task, or exit with a friendly message (no traceback).

    This keeps `main()` simple and avoids flake8 C901 complexity.
    """
    task_type = str(task_request.get("task_type", "") or "").strip() or "<unknown>"
    try:
        return await submit_task(client, base_url, token, task_request)
    except RuntimeError as e:
        message = str(e)

        if _is_no_runners_available_error(message):
            print(
                format_status(
                    f"Task submission failed ({task_type}): No runners available", level="error"
                )
            )
            print(
                "What it usually means: no runner is online/registered/available, "
                "or no runner supports this task type."
            )
            print("Next steps:")
            print(f"- Check runners status: GET {base_url}/api/runners")
            print("- Ensure at least one runner is started and registered")
            print(f"- Ensure task_type={task_type!r} is supported by a runner")

            # Best-effort: show /api/runners output to help the user.
            try:
                runners_payload = await get_runners_overview(client, base_url, token)
                _print_runners_overview(runners_payload)
            except Exception as overview_error:
                print(
                    format_status(
                        f"Could not fetch /api/runners: {overview_error}", level="warning"
                    )
                )

            raise SystemExit(2)

        # Anything else: keep it readable (no full traceback).
        raise SystemExit(message)


async def submit_task(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    task_request: dict[str, Any],
) -> str:
    """Step 2: submit one task and return the `task_id`."""

    url = f"{base_url}/task/execute"
    resp = await client.post(url, json=task_request, headers=_auth_headers(token))
    await _raise_for_http_error(resp)

    payload = resp.json()
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError(f"Unexpected response shape: {payload!r}")
    return task_id


async def get_task_status(
    client: httpx.AsyncClient, base_url: str, token: str, task_id: str
) -> dict:
    """Read the current task status."""
    url = f"{base_url}/task/status/{task_id}"
    resp = await client.get(url, headers=_auth_headers(token))
    await _raise_for_http_error(resp)
    return resp.json()


async def wait_for_terminal_state(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    task_id: str,
    max_wait_seconds: int,
) -> dict:
    """Step 3: poll until the task is completed/failed.

    The manager can transiently set status to "warning" if the notify callback
    fails (and then it may retry). We print it and keep waiting.
    """

    deadline = asyncio.get_event_loop().time() + max_wait_seconds

    while True:
        status_payload = await get_task_status(client, base_url, token, task_id)
        status = status_payload.get("status")
        error = status_payload.get("error")

        # The common states are: running / completed / failed / warning
        # We treat "completed" and "failed" as terminal.
        if status == "completed":
            return status_payload
        if status == "failed":
            return status_payload

        # "warning" is not necessarily terminal: it usually means the job is done
        # but the notify_url callback did not return HTTP 200.
        if status == "warning" and error:
            print(
                format_status(f"Status=warning (notify callback issue): {error}", level="warning")
            )
        else:
            print(format_status(f"Status={status!r} (waiting…)", level="info"))

        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(
                f"Task did not reach a terminal state within {max_wait_seconds}s. "
                f"Last status={status!r}"
            )

        await asyncio.sleep(POLL_SECONDS)


async def get_result_manifest(
    client: httpx.AsyncClient, base_url: str, token: str, task_id: str
) -> dict[str, Any]:
    """Step 4: fetch the task result manifest.

    This endpoint returns HTTP 425 if the task is not completed yet.
    """
    url = f"{base_url}/task/result/{task_id}"
    resp = await client.get(url, headers=_auth_headers(token))
    await _raise_for_http_error(resp)
    return resp.json()


async def download_result_file(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    task_id: str,
    file_path: str,
    output_path: Path,
) -> None:
    """Download one result file to disk.

    Endpoint:
      GET /task/result/{task_id}/file/{file_path}

    Notes:
    - `file_path` must be URL-encoded because it can contain slashes.
    - We stream the response to avoid loading large files in memory.
    """
    encoded_path = quote(file_path, safe="")
    url = f"{base_url}/task/result/{task_id}/file/{encoded_path}"

    # Result files can be large. Increase read timeout.
    timeout = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)

    async with client.stream("GET", url, headers=_auth_headers(token), timeout=timeout) as resp:
        await _raise_for_http_error(resp)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as f:
            async for chunk in resp.aiter_bytes():
                f.write(chunk)


async def maybe_download_first_file(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    task_id: str,
    files: Any,
) -> None:
    """Optionally download the first file in the manifest.

    This is a convenience feature for manual tests.
    """
    if not DOWNLOAD_FIRST_FILE:
        print(format_status("Auto-download disabled", level="warning"))
        return

    if not isinstance(files, list) or not files:
        print(format_status("No files produced", level="warning"))
        return

    first = files[0]
    if not isinstance(first, str) or not first:
        print(
            format_status(
                "Manifest contains a non-string file entry; skipping download", level="warning"
            )
        )
        return

    local_name = _safe_filename(first)
    output_path = OUTPUT_DIR / local_name
    print(format_status(f"Downloading first file: {first!r} -> {str(output_path)!r}", level="info"))
    await download_result_file(client, base_url, token, task_id, first, output_path)
    print(format_status("Download OK", level="info"))


async def run_one_task_check(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    label: str,
    task_request: dict[str, Any],
    *,
    download_first_file: bool,
    max_wait_seconds: int,
) -> None:
    """Run one submit/wait/result cycle for a single task payload."""
    task_type = task_request.get("task_type")
    source_url = task_request.get("source_url")
    parameters = task_request.get("parameters")
    print()
    print("=" * 60)
    print(label)
    print("=" * 60)
    print(f"task_type={task_type!r}")
    print(f"source_url={source_url!r}")
    print(f"parameters={parameters!r}")

    task_id = await submit_task_or_exit(client, base_url, token, task_request)
    print(format_status(f"Task submitted. task_id={task_id}", level="info"))

    final_status = await wait_for_terminal_state(
        client, base_url, token, task_id, max_wait_seconds=max_wait_seconds
    )
    status = final_status.get("status")
    print(format_status(f"Final status: {status}", level="info"))
    if final_status.get("error"):
        print(format_status(f"Error: {final_status.get('error')}", level="warning"))

    if status != "completed":
        error_text = str(final_status.get("error") or "").strip()
        lowered = error_text.lower()
        if (
            "file was not found on the server" in lowered
            or "unable to download input" in lowered
            or "source url does not contain a valid filename" in lowered
            or "source url does not point to a valid" in lowered
            or "impossible to download" in lowered
        ):
            raise SourceDownloadError(
                f"{label} failed due to source download/access issue "
                f"(task_id={task_id}, source_url={source_url!r}, error={error_text!r})"
            )

        raise SystemExit(
            f"{label} failed (task_id={task_id}, task_type={task_type!r}, status={status!r})"
        )

    manifest = await get_result_manifest(client, base_url, token, task_id)
    files = manifest.get("files")
    print(format_status(f"Result manifest received. files={files!r}", level="info"))

    if download_first_file:
        await maybe_download_first_file(client, base_url, token, task_id, files)
    else:
        print(format_status("Auto-download disabled for this check", level="warning"))


async def main(args: argparse.Namespace):
    manager_url, token = _load_runtime_settings()
    if _is_placeholder_token(token):
        raise SystemExit("Please set a non-empty token in manager/.env or RUNNER_API_TOKEN.")

    base_url = _normalize_base_url(manager_url)

    # One shared async HTTP client for all calls.
    # Keep timeouts modest: this script is for quick interactive tests.
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # 1) Sanity check: auth + manager reachability.
        version_payload = await check_auth(client, base_url, token)
        print(format_status(f"Manager OK. Version: {version_payload.get('version')}", level="info"))
        mode = (
            "encoding + transcription/translation"
            if args.with_transcription_translation
            else "encoding only"
        )
        if args.max_wait_seconds is not None:
            max_wait_seconds = max(1, int(args.max_wait_seconds))
        else:
            max_wait_seconds = (
                1200 if args.with_transcription_translation else DEFAULT_MAX_WAIT_SECONDS
            )
        source_candidates = _resolve_source_urls()
        if not source_candidates:
            raise SystemExit(
                "No source URL configured. Set RUNNER_SOURCE_URL (or SOURCE_FILE) to a public media URL."
            )
        print(f"Check mode: {mode}")
        print(f"Per-task timeout: {max_wait_seconds}s")
        if len(source_candidates) > 1:
            print(
                format_status(
                    f"Source candidates: {len(source_candidates)} (automatic fallback enabled)",
                    level="info",
                )
            )

        for index, source_url in enumerate(source_candidates, start=1):
            print()
            print(
                format_status(
                    f"Using source candidate {index}/{len(source_candidates)}: {source_url}",
                    level="info",
                )
            )
            plan = _build_task_plan(args.with_transcription_translation, source_url=source_url)
            try:
                for step in plan:
                    await run_one_task_check(
                        client,
                        base_url,
                        token,
                        str(step["label"]),
                        dict(step["request"]),
                        download_first_file=bool(step["download_first_file"]),
                        max_wait_seconds=max_wait_seconds,
                    )
                print(format_status("Pipeline checks passed.", level="info"))
                return
            except SourceDownloadError as exc:
                if index < len(source_candidates):
                    print(format_status(str(exc), level="warning"))
                    print(format_status("Trying next source candidate…", level="warning"))
                    continue
                raise SystemExit(
                    f"{exc}\nAll source candidates failed. "
                    "Set RUNNER_SOURCE_URL to a media URL reachable by runner instances."
                )


if __name__ == "__main__":
    cli_args = _parse_args()
    asyncio.run(main(cli_args))
