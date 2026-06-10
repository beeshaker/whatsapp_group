import pytest
from unittest.mock import MagicMock
from auth import hash_password, verify_password


def test_hash_and_verify_password():
    hashed = hash_password("mypassword")
    assert hashed != "mypassword"
    assert verify_password("mypassword", hashed)
    assert not verify_password("wrongpassword", hashed)


def test_different_hashes_for_same_password():
    h1 = hash_password("same")
    h2 = hash_password("same")
    assert h1 != h2  # bcrypt includes random salt


async def test_require_login_returns_username_when_session_has_username():
    from auth import require_login
    mock_request = MagicMock()
    mock_request.session = {"username": "alice"}
    result = await require_login(mock_request)
    assert result == "alice"


async def test_require_login_raises_302_when_no_session():
    from fastapi import HTTPException
    from auth import require_login
    mock_request = MagicMock()
    mock_request.session = {}
    with pytest.raises(HTTPException) as exc_info:
        await require_login(mock_request)
    assert exc_info.value.status_code == 302
    assert exc_info.value.headers["Location"] == "/login"
