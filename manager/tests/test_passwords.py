"""Unit tests for password hashing helpers."""

"""Validates bcrypt password hashing and verification with bytes and string inputs."""

import pytest

from app.core.passwords import BcryptPasswordContext


def test_password_context_supports_bytes_for_hash_and_verify():
    """Validate Password context supports bytes for hash and verify."""
    context = BcryptPasswordContext(rounds=4)
    hashed = context.hash(b"secret-password")

    assert isinstance(hashed, str)
    assert context.verify(b"secret-password", hashed.encode("utf-8"))


def test_password_context_hash_rejects_invalid_type():
    """Validate Password context hash rejects invalid type."""
    context = BcryptPasswordContext()

    with pytest.raises(TypeError):
        context.hash(123)  # type: ignore[arg-type]


def test_password_context_verify_handles_invalid_inputs():
    """Validate Password context verify handles invalid inputs."""
    context = BcryptPasswordContext(rounds=4)
    valid_hash = context.hash("secret-password")

    assert context.verify("secret-password", "not-a-valid-bcrypt-hash") is False
    assert context.verify(123, valid_hash) is False  # type: ignore[arg-type]
