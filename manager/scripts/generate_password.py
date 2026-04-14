"""Generate one ADMIN_USERS entry for the .env file.

Usage:
    uv run scripts/generate_password.py
"""

import getpass
import re

from app.core.passwords import BcryptPasswordContext

LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]+$")


def ask_label() -> str:
    """Prompt for an admin label compatible with ADMIN_USERS__* keys."""
    while True:
        label = input("Admin label (letters, numbers, underscores, ., -, @): ").strip()
        if not label:
            print("Label cannot be empty.")
            continue
        if not LABEL_PATTERN.fullmatch(label):
            print("Invalid label. Use only letters, numbers, underscores, ., -, and @.")
            continue
        return label


def ask_password() -> str:
    """Prompt for and confirm a non-empty password."""
    while True:
        password = getpass.getpass("Password: ")
        if not password:
            print("Password cannot be empty.")
            continue

        password_confirmation = getpass.getpass("Confirm password: ")
        if password != password_confirmation:
            print("Passwords do not match.")
            continue

        return password


if __name__ == "__main__":
    label = ask_label()
    password = ask_password()

    pwd_context = BcryptPasswordContext()
    hashed_password = pwd_context.hash(password)

    print("\nAdd this line to your .env file:")
    print(f'ADMIN_USERS__{label}="{hashed_password}"')
