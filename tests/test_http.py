import httpx
import pytest
import respx

from copilot.tools.http import ApiError, RateLimitError, TTLCache, rate_limit_wait, request_with_retry
from tests.conftest import no_sleep

URL = "https://api.github.com/x"


async def call(**kw):
    async with httpx.AsyncClient() as client:
        return await request_with_retry(client, "GET", URL, sleep=no_sleep, **kw)


@respx.mock
async def test_retries_5xx_then_succeeds():
    route = respx.get(URL).mock(side_effect=[httpx.Response(502), httpx.Response(503), httpx.Response(200, json={"ok": 1})])
    resp = await call()
    assert resp.json() == {"ok": 1}
    assert route.call_count == 3


@respx.mock
async def test_retries_network_error():
    respx.get(URL).mock(side_effect=[httpx.ConnectTimeout("t"), httpx.Response(200, json={})])
    assert (await call()).status_code == 200


@respx.mock
async def test_gives_up_after_retries():
    route = respx.get(URL).mock(return_value=httpx.Response(500, json={"message": "boom"}))
    with pytest.raises(ApiError) as exc:
        await call(retries=2)
    assert exc.value.status == 500
    assert route.call_count == 3


@respx.mock
async def test_4xx_not_retried():
    route = respx.get(URL).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(ApiError) as exc:
        await call()
    assert exc.value.status == 404 and "Not Found" in exc.value.message
    assert route.call_count == 1


@respx.mock
async def test_short_rate_limit_waits_and_retries():
    limited = httpx.Response(429, headers={"retry-after": "2"}, json={"message": "slow down"})
    respx.get(URL).mock(side_effect=[limited, httpx.Response(200, json={})])
    assert (await call()).status_code == 200


@respx.mock
async def test_long_rate_limit_raises_immediately():
    limited = httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"}, json={"message": "API rate limit exceeded"})
    route = respx.get(URL).mock(return_value=limited)
    with pytest.raises(RateLimitError):
        await call()
    assert route.call_count == 1


def test_plain_403_is_not_a_rate_limit():
    assert rate_limit_wait(httpx.Response(403, json={"message": "Resource not accessible"})) is None


def test_secondary_rate_limit_detected_by_message():
    resp = httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"})
    assert rate_limit_wait(resp) == 60.0


def test_ttl_cache_expires():
    now = [0.0]
    cache = TTLCache(10, clock=lambda: now[0])
    cache.set("k", 1)
    assert cache.get("k") == 1
    now[0] = 11
    assert cache.get("k") is None
