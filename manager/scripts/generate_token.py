"""Generate one AUTHORIZED_TOKENS entry for the .env file.

Usage:
    uv run scripts/generate_token.py
"""

import argparse
import secrets

from app.core.token_roles import token_label_role


def generate_token(length: int = 32) -> str:
    """Generate a secure API token."""
    return secrets.token_urlsafe(length)


def ask_token_label() -> str:
    """Prompt for an application or Runner token label."""
    while True:
        label = input("Token label (app, runners, or a name starting with either): ").strip()
        if token_label_role(label) is None:
            print("Start with app or runners and use only letters, digits, and underscores.")
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
