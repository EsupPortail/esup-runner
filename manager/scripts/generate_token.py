"""Generate one AUTHORIZED_TOKENS entry for the .env file.

Usage:
    uv run scripts/generate_token.py
"""

import argparse
import re
import secrets

LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


def generate_token(length: int = 32) -> str:
    """Generate a secure API token."""
    return secrets.token_urlsafe(length)


def ask_token_label() -> str:
    """Prompt for a token label compatible with .env variable names."""
    while True:
        label = input("Token label (letters, numbers, underscores): ").strip()
        if not label:
            print("Label cannot be empty.")
            continue
        if not LABEL_PATTERN.fullmatch(label):
            print("Invalid label. Use only letters, numbers, and underscores.")
            continue
        return label


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate one secure API token.")
    parser.add_argument(
        "--length", type=int, default=32, help="Token size passed to secrets.token_urlsafe()."
    )
    args = parser.parse_args()

    label = ask_token_label()
    token = generate_token(args.length)

    print("\nAdd this line to your .env file:")
    print(f"AUTHORIZED_TOKENS__{label}={token}")
