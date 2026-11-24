"""Naming and matching rules for Manager API tokens."""

import hmac
import re
from typing import Literal

TokenRole = Literal["runners", "app"]
TOKEN_LABEL_PATTERN = r"(runners|app)[A-Za-z0-9_]*"


def token_label_role(label: str) -> TokenRole | None:
    """Return the role encoded in a valid API token label."""
    match = re.fullmatch(TOKEN_LABEL_PATTERN, label)
    if match is None:
        return None
    return "runners" if match[1] == "runners" else "app"


def token_matches(token: str, expected: str | None) -> bool:
    """Compare a presented token with a configured or persisted secret."""
    return bool(expected and hmac.compare_digest(token.encode(), expected.encode()))
