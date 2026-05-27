"""Logging and metadata helpers for the encoding runtime.

Handles append-safe writes to `encoding.log` and `info_video.json`.
Provides small runtime wrappers shared by orchestration and flow helpers.
"""

from __future__ import annotations

import json
from json.decoder import JSONDecodeError
from typing import Any


def encode_log(msg: str, *, debug: bool, videos_output_dir: str) -> None:
    """Write message to `encoding.log` and optionally print to stdout."""
    if debug:
        print(msg)

    with open(videos_output_dir + "/encoding.log", "a") as f:
        f.write("\n")
        f.write(msg)
        f.write("\n")


def add_info_video(
    key: str,
    value: Any,
    *,
    append: bool,
    videos_output_dir: str,
) -> None:
    """Add encoding metadata to `info_video.json`."""
    data: dict[str, Any] = {}

    try:
        with open(videos_output_dir + "/info_video.json") as json_file:
            data = json.load(json_file)
    except (FileNotFoundError, JSONDecodeError):
        pass

    if data.get(key) and append:
        existing_val = data[key]
        if isinstance(existing_val, list):
            existing_val.append(value)
        else:
            data[key] = [existing_val, value]
    else:
        data[key] = [value] if append else value

    with open(videos_output_dir + "/info_video.json", "w") as outfile:
        json.dump(data, outfile, indent=2)
