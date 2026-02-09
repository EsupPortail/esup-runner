# utils/generate_tokens.py
"""
Use this script to generate API tokens useful for the .env file.

Usage:
    uv run scripts/generate_tokens.py
"""

import argparse
import secrets


def generate_token(length=32) -> str:
    """
    Generate a secure API token.
    """
    return secrets.token_urlsafe(length)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a secure API token.")
    parser.add_argument(
        "--length", type=int, default=32, help="Length of the generated token (default: 32)"
    )
    args = parser.parse_args()

    # List of AUTHORIZED_TOKENS (to be replaced with your own)
    authorized_tokens = {
        "runners_gpu_um": generate_token(args.length),
        "runners_cpu_um": generate_token(args.length),
        "app_pod_um": generate_token(args.length),
    }
    # Display tokens for .env
    for user, token in authorized_tokens.items():
        print(f"AUTHORIZED_TOKENS__{user}={token}")
