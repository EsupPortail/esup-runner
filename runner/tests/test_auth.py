import pytest
from fastapi import HTTPException

from app.core import auth
from app.core.config import config


@pytest.mark.asyncio
async def test_verify_token_with_header():
    token = config.RUNNER_TOKEN
    result = await auth.verify_token(api_token=token, credentials=None)
    assert result == token


@pytest.mark.asyncio
async def test_verify_token_with_bearer():
    token = config.RUNNER_TOKEN

    class Cred:
        def __init__(self, credentials):
            self.credentials = credentials

    result = await auth.verify_token(api_token=None, credentials=Cred(token))
    assert result == token


@pytest.mark.asyncio
async def test_verify_token_missing_raises():
    with pytest.raises(HTTPException) as exc:
        await auth.verify_token(api_token=None, credentials=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_token_invalid_raises():
    with pytest.raises(HTTPException) as exc:
        await auth.verify_token(api_token="wrong", credentials=None)
    assert exc.value.status_code == 401


def test_get_current_manager_returns_token():
    assert auth.get_current_manager("tok") == "tok"
