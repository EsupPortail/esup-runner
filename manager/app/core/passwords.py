"""Password hashing helpers backed directly by bcrypt."""

from __future__ import annotations

from typing import Union

import bcrypt


class BcryptPasswordContext:
    """Minimal hash/verify interface compatible with current config usage."""

    def __init__(self, rounds: int = 12) -> None:
        self._rounds = rounds

    @staticmethod
    def _to_bytes(value: Union[str, bytes]) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8")
        raise TypeError("Password values must be str or bytes")

    def hash(self, password: Union[str, bytes]) -> str:
        password_bytes = self._to_bytes(password)
        hashed = bcrypt.hashpw(password_bytes, bcrypt.gensalt(rounds=self._rounds))
        return hashed.decode("utf-8")

    def verify(self, password: Union[str, bytes], hashed_password: Union[str, bytes]) -> bool:
        try:
            password_bytes = self._to_bytes(password)
            hashed_bytes = self._to_bytes(hashed_password)
            return bcrypt.checkpw(password_bytes, hashed_bytes)
        except (TypeError, ValueError):
            # Invalid hash format or invalid input type -> authentication failure.
            return False
