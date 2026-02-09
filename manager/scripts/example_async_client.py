#!/usr/bin/env python3
"""
Example (async) client: submit one task to the ESUP Runner Manager.

Goal of this script
------------------
This file is intentionally *simple* and *very* commented so that anyone
cloning the project can do a quick manual test.

What you should have to change
------------------------------
Only the TOKEN below.

What this script does
---------------------
1) Calls `GET /api/version` to confirm authentication works.
2) Submits a task with `POST /task/execute` and receives a `task_id`.
3) Polls `GET /task/status/{task_id}` until the task reaches a terminal state.
4) Fetches the result manifest with `GET /task/result/{task_id}`.

How to run
----------
    uv run scripts/example_async_client.py

Notes
-----
- The Manager API expects a token configured in the manager environment
  (`AUTHORIZED_TOKENS__...` variables).
- Auth header can be either:
    * `X-API-Token: <token>` (used in this script)
    * or `Authorization: Bearer <token>`
- `notify_url` is required by the current API schema. We use a public endpoint
  that returns HTTP 200 on POST to avoid leaving the task in a "warning" state
  when the manager tries to call the callback.
"""

import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# CHANGE THIS.
# This must match one of the manager's configured AUTHORIZED_TOKENS values.
TOKEN = "CHANGE_ME"

# If your manager runs elsewhere, update this URL.
MANAGER_URL = "http://127.0.0.1:8081"

# You typically do NOT need to change the values below for a quick manual test.

# The task type must be supported by at least one available runner.
# Common values: "encoding", "transcription", "studio" (depending on your runners).
TASK_TYPE = "encoding"

# A small public media file for quick tests.
# Important: the manager validates URLs and will reject private/loopback hosts.
SOURCE_URL = "https://samplelib.com/lib/preview/mp4/sample-5s.mp4"

# Required by the API schema.
# The manager will POST to this URL when the task completes.
# If the callback returns non-200, the task can temporarily go into "warning".
NOTIFY_URL = "https://httpbin.org/status/200"

# Polling interval (seconds) for manual testing.
POLL_SECONDS = 2

# Safety timeout so the script doesn't run forever.
MAX_WAIT_SECONDS = 300

# Optional: automatically download the first file listed in the manifest.
DOWNLOAD_FIRST_FILE = True

# Where to save the downloaded file (default: current directory).
OUTPUT_DIR = Path(".")


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
    resp = await client.get(url, headers=_auth_headers(token))
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


async def submit_task_or_exit(client: httpx.AsyncClient, base_url: str, token: str) -> str:
    """Submit a task, or exit with a friendly message (no traceback).

    This keeps `main()` simple and avoids flake8 C901 complexity.
    """
    try:
        return await submit_task(client, base_url, token)
    except RuntimeError as e:
        message = str(e)

        if _is_no_runners_available_error(message):
            print("Task submission failed: No runners available")
            print(
                "What it usually means: no runner is online/registered/available, "
                "or no runner supports TASK_TYPE."
            )
            print("Next steps:")
            print(f"- Check runners status: GET {base_url}/api/runners")
            print("- Ensure at least one runner is started and registered")
            print(f"- Ensure TASK_TYPE={TASK_TYPE!r} is supported by a runner")

            # Best-effort: show /api/runners output to help the user.
            try:
                runners_payload = await get_runners_overview(client, base_url, token)
                _print_runners_overview(runners_payload)
            except Exception as overview_error:
                print(f"Could not fetch /api/runners: {overview_error}")

            raise SystemExit(2)

        # Anything else: keep it readable (no full traceback).
        raise SystemExit(message)


async def submit_task(client: httpx.AsyncClient, base_url: str, token: str) -> str:
    """Step 2: submit one task and return the `task_id`."""

    # This is the API payload described in manager/docs/README.md.
    # Keep it small and easy to understand.
    task_request: dict[str, Any] = {
        "etab_name": "Quick manual test",
        "app_name": "example_async_client.py",
        "app_version": "0",
        "task_type": TASK_TYPE,
        "source_url": SOURCE_URL,
        "affiliation": "manual-test",
        "parameters": {
            # Task-specific parameters. Many runners accept an empty dict.
            # For "encoding", some runners accept a rendition map; keep it minimal.
            "rendition": {"360": "640x360"}
        },
        "notify_url": NOTIFY_URL,
    }

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
) -> dict:
    """Step 3: poll until the task is completed/failed.

    The manager can transiently set status to "warning" if the notify callback
    fails (and then it may retry). We print it and keep waiting.
    """

    deadline = asyncio.get_event_loop().time() + MAX_WAIT_SECONDS

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
            print(f"Status=warning (notify callback issue): {error}")
        else:
            print(f"Status={status!r} (waiting...) ")

        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(
                f"Task did not reach a terminal state within {MAX_WAIT_SECONDS}s. "
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
        print("Auto-download disabled")
        return

    if not isinstance(files, list) or not files:
        print("No files produced")
        return

    first = files[0]
    if not isinstance(first, str) or not first:
        print("Manifest contains a non-string file entry; skipping download")
        return

    local_name = _safe_filename(first)
    output_path = OUTPUT_DIR / local_name
    print(f"Downloading first file: {first!r} -> {str(output_path)!r}")
    await download_result_file(client, base_url, token, task_id, first, output_path)
    print("Download OK")


async def main():
    # Fail fast: we do not want people to accidentally run with a committed token.
    if TOKEN.strip() == "CHANGE_ME":
        raise SystemExit("Please set TOKEN at the top of this file.")

    base_url = _normalize_base_url(MANAGER_URL)

    # One shared async HTTP client for all calls.
    # Keep timeouts modest: this script is for quick interactive tests.
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # 1) Sanity check: auth + manager reachability.
        version_payload = await check_auth(client, base_url, TOKEN)
        print(f"Manager OK. Version: {version_payload.get('version')}")

        # 2) Submit one task.
        task_id = await submit_task_or_exit(client, base_url, TOKEN)

        print(f"Task submitted. task_id={task_id}")

        # 3) Poll until completion.
        final_status = await wait_for_terminal_state(client, base_url, TOKEN, task_id)
        print(f"Final status: {final_status.get('status')}")
        if final_status.get("error"):
            print(f"Error: {final_status.get('error')}")

        # 4) Fetch result manifest (list of produced files).
        # If your runner produced files, you'll see them here.
        manifest = await get_result_manifest(client, base_url, TOKEN, task_id)
        files = manifest.get("files")
        print(f"Result manifest received. files={files!r}")

        # 5) Optional: download the first produced file.
        # This is a nice end-to-end check that result streaming works.
        await maybe_download_first_file(client, base_url, TOKEN, task_id, files)


if __name__ == "__main__":
    asyncio.run(main())
