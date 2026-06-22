import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_get_access_token():
    import mpesa
    mpesa._token_cache.clear()

    token_resp = AsyncMock()
    token_resp.json = lambda: {"access_token": "test-token", "expires_in": "3599"}
    token_resp.raise_for_status = lambda: None

    with patch("httpx.AsyncClient") as Mock:
        inst = AsyncMock()
        inst.__aenter__ = AsyncMock(return_value=inst)
        inst.__aexit__ = AsyncMock(return_value=False)
        inst.get = AsyncMock(return_value=token_resp)
        Mock.return_value = inst

        token = await mpesa.get_access_token()

    assert token == "test-token"
    assert mpesa._token_cache["token"] == "test-token"


@pytest.mark.asyncio
async def test_get_access_token_uses_cache():
    import mpesa, time
    mpesa._token_cache["token"] = "cached-token"
    mpesa._token_cache["expires_at"] = time.time() + 3000

    with patch("httpx.AsyncClient") as Mock:
        token = await mpesa.get_access_token()

    Mock.assert_not_called()
    assert token == "cached-token"


@pytest.mark.asyncio
async def test_initiate_stk_push():
    import mpesa, time
    mpesa._token_cache["token"] = "cached-token"
    mpesa._token_cache["expires_at"] = time.time() + 3000

    stk_resp = AsyncMock()
    stk_resp.json = lambda: {
        "CheckoutRequestID": "ws_CO_TEST_123",
        "ResponseCode": "0",
        "CustomerMessage": "Success",
    }
    stk_resp.raise_for_status = lambda: None

    with patch("httpx.AsyncClient") as Mock:
        inst = AsyncMock()
        inst.__aenter__ = AsyncMock(return_value=inst)
        inst.__aexit__ = AsyncMock(return_value=False)
        inst.post = AsyncMock(return_value=stk_resp)
        Mock.return_value = inst

        result = await mpesa.initiate_stk_push(
            phone="254712345678",
            amount=Decimal("1500"),
            account_ref="acme-sub",
            callback_url="https://example.com/webhook/mpesa",
        )

    assert result["CheckoutRequestID"] == "ws_CO_TEST_123"
    assert result["ResponseCode"] == "0"
    call_payload = inst.post.call_args[1]["json"]
    assert call_payload["PhoneNumber"] == "254712345678"
    assert call_payload["Amount"] == 1500
