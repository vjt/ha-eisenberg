"""What the client does when Arlo's edge answers with something that isn't Arlo.

Issue #32 (natebrockert, 12 devices across 7 gateways): the ocapi host sits
behind a WAF that intermittently returns a Cloudflare-style HTML 403 instead
of a JSON body. `resp.json()` turns that into `aiohttp.ContentTypeError`:

    403, message='Attempt to decode JSON with unexpected mimetype:
    text/html; charset=utf-8', url='https://ocapi-app.arlo.com/api/auth'

which is neither an AuthenticationError nor a RateLimitedError, so every
caller that classifies on those two misses it entirely. The request never
reached the API, so the answer carries no verdict about the credentials —
it must not look like one. TransientAPIError says exactly that.
"""

from __future__ import annotations

import re

import pytest
from aioresponses import aioresponses

from eisenberg.client import EisenbergClient
from eisenberg.exceptions import (
    AuthenticationError,
    EisenbergError,
    RateLimitedError,
    TransientAPIError,
)

OCAPI = "https://ocapi-app.arlo.com"
MYAPI = "https://myapi.arlo.com"

_BLOCK_PAGE = (
    "<!DOCTYPE html><html><head><title>Access denied</title></head>"
    "<body><h1>Sorry, you have been blocked</h1></body></html>"
)


def make_client() -> EisenbergClient:
    return EisenbergClient(
        email="test@example.com",
        password="hunter2",
        device_id="test-device-uuid",
    )


class TestWafBlockPage:
    async def test_html_403_on_auth_raises_transient_not_content_type_error(self) -> None:
        async with make_client() as client:
            with aioresponses() as m:
                m.post(
                    f"{OCAPI}/api/auth",
                    status=403,
                    body=_BLOCK_PAGE,
                    content_type="text/html; charset=utf-8",
                )
                with pytest.raises(TransientAPIError):
                    await client.login()

    def test_transient_error_is_not_an_auth_error(self) -> None:
        """The whole point: callers classifying on auth must not match it.

        A block page says nothing about the credentials, so treating it as an
        auth failure would tear down a working config entry.
        """
        assert not issubclass(TransientAPIError, AuthenticationError)
        assert not issubclass(TransientAPIError, RateLimitedError)
        assert issubclass(TransientAPIError, EisenbergError)

    async def test_message_carries_status_endpoint_and_body_snippet(self) -> None:
        """A log line that names what came back beats one that names aiohttp."""
        async with make_client() as client:
            with aioresponses() as m:
                m.post(
                    f"{OCAPI}/api/auth",
                    status=403,
                    body=_BLOCK_PAGE,
                    content_type="text/html; charset=utf-8",
                )
                with pytest.raises(TransientAPIError) as excinfo:
                    await client.login()

        message = str(excinfo.value)
        assert "403" in message
        assert "/api/auth" in message
        assert "text/html" in message
        assert "blocked" in message

    async def test_json_content_type_with_garbage_body_also_transient(self) -> None:
        """A truncated or proxied body is the same class of failure."""
        async with make_client() as client:
            with aioresponses() as m:
                m.post(
                    f"{OCAPI}/api/auth",
                    status=200,
                    body="{ this is not json",
                    content_type="application/json",
                )
                with pytest.raises(TransientAPIError):
                    await client.login()

    async def test_myapi_block_page_is_transient_too(self) -> None:
        """The WAF fronts both hosts; get_devices must classify the same way."""
        async with make_client() as client:
            with aioresponses() as m:
                client.token = "a-token"
                m.get(
                    re.compile(rf"^{re.escape(MYAPI)}/hmsweb/v2/users/devices.*"),
                    status=403,
                    body=_BLOCK_PAGE,
                    content_type="text/html; charset=utf-8",
                )
                with pytest.raises(TransientAPIError):
                    await client.get_devices()
