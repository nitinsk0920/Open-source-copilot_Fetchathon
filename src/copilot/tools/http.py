"""HTTP helper: timeouts are set on the httpx client; this adds retry/backoff and rate-limit handling.

Error messages never include request headers, so tokens cannot leak through them.
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int | None, message: str):
        super().__init__(f"{message}" if status is None else f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class RateLimitError(ApiError):
    """Rate limited and the wait was too long (or retries ran out)."""

    def __init__(self, status: int | None, message: str, retry_after: float | None = None):
        super().__init__(status, message)
        self.retry_after = retry_after


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])[:300]
    except ValueError:
        pass
    return (resp.reason_phrase or "error")[:300]


def rate_limit_wait(resp: httpx.Response, now: float | None = None) -> float | None:
    """Seconds to wait if this response is a rate limit (primary or secondary), else None."""
    if resp.status_code not in (403, 429):
        return None
    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            return max(float(retry_after), 1.0)
        except ValueError:
            pass
    if resp.headers.get("x-ratelimit-remaining") == "0":
        try:
            reset = float(resp.headers.get("x-ratelimit-reset", ""))
            return max(reset - (now if now is not None else time.time()), 1.0) + 1.0
        except ValueError:
            return 60.0
    if resp.status_code == 429 or "rate limit" in _error_message(resp).lower():
        return 60.0
    return None


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    json: Any = None,
    retries: int = 3,
    base_delay: float = 0.5,
    max_wait: float = 20.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> httpx.Response:
    """Retry network errors, 5xx and short rate limits with backoff. Other 4xx raise immediately."""
    last: ApiError | None = None
    for attempt in range(retries + 1):
        delay = base_delay * 2**attempt
        try:
            resp = await client.request(method, url, params=params, headers=headers, json=json)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = ApiError(None, f"network error ({type(exc).__name__})")
        else:
            if resp.status_code < 400:
                return resp
            wait = rate_limit_wait(resp)
            if wait is not None:
                last = RateLimitError(resp.status_code, f"rate limited: {_error_message(resp)}", wait)
                if wait > max_wait:
                    raise last
                delay = wait
            elif resp.status_code >= 500:
                last = ApiError(resp.status_code, _error_message(resp))
            else:
                raise ApiError(resp.status_code, _error_message(resp))
        if attempt < retries:
            logger.warning("%s %s failed (%s), retry %d/%d in %.1fs", method, url, last, attempt + 1, retries, delay)
            await sleep(delay)
    assert last is not None
    raise last


class TTLCache:
    """Tiny in-memory cache so repeated demo runs don't burn rate limit."""

    def __init__(self, ttl: float, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._data: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, value = hit
        if self._clock() >= expires:
            del self._data[key]
            return None
        return value

    def set(self, key: Any, value: Any) -> None:
        self._data[key] = (self._clock() + self._ttl, value)
