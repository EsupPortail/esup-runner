"""Generate one ADMIN_USERS entry for the .env file.

Usage:
    uv run scripts/generate_password.py
"""

import getpass
import re

from app.core.passwords import BcryptPasswordContext

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


def ask_username() -> str:
    """Prompt for a username compatible with .env variable names."""
    while True:
        username = input("Admin username (letters, numbers, underscores): ").strip()
        if not username:
            print("Username cannot be empty.")
            continue
        if not USERNAME_PATTERN.fullmatch(username):
            print("Invalid username. Use only letters, numbers, and underscores.")
            continue
        return username


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
    username = ask_username()
    password = ask_password()

    pwd_context = BcryptPasswordContext()
    hashed_password = pwd_context.hash(password)

    print("\nAdd this line to your .env file:")
    print(f'ADMIN_USERS__{username}="{hashed_password}"')
