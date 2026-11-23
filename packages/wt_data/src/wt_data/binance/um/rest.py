"""Async client for Binance USD-M *public* REST endpoints with rate-limit discipline.

Rules (docs/03-data-sources.md §3, docs/08-execution.md §6):

* limits are per IP and shared with every other process on the host — including
  the future trading process — so data collection only uses a configurable
  fraction of the per-minute REQUEST_WEIGHT budget;
* the server's ``X-MBX-USED-WEIGHT-1M`` header is authoritative and corrects our
  local accounting after every response;
* HTTP 429 / 418 stop *all* requests until ``Retry-After`` has passed (repeated
  violations escalate to IP bans of up to 3 days);
* ``/futures/data/*`` endpoints have their own request-count limit and get a
  separate rolling-window budget.

This client never signs requests and has no notion of API keys.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Self

import httpx

from wt_core.clock import NS_PER_MS, NS_PER_S, Clock, Stamp, SystemClock

log = logging.getLogger(__name__)

DEPTH_LIMIT_WEIGHTS: Mapping[int, int] = {5: 2, 10: 2, 20: 2, 50: 2, 100: 5, 500: 10, 1000: 20}
WEIGHT_HEADER = "x-mbx-used-weight-1m"


class RestError(Exception):
    def __init__(self, message: str, *, status: int | None = None, body: bytes = b"") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class TransientError(RestError):
    """Network failure, timeout or 5xx — safe to retry later."""


class RateLimitedError(RestError):
    """429 or 418 — every request is paused until ``retry_after_s`` has passed."""

    def __init__(self, message: str, *, status: int, retry_after_s: float, body: bytes = b"") -> None:
        super().__init__(message, status=status, body=body)
        self.retry_after_s = retry_after_s


@dataclass(frozen=True, slots=True)
class RestResponse:
    path: str
    params: Mapping[str, str | int]
    status: int
    content: bytes
    sent: Stamp
    received: Stamp
    used_weight_1m: int | None

    @property
    def latency_ms(self) -> float:
        return (self.received.mono_ns - self.sent.mono_ns) / NS_PER_MS

    @property
    def origin(self) -> str:
        query = "&".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"GET:{self.path}?{query}" if query else f"GET:{self.path}"


class MinuteWeightBudget:
    """Client-side REQUEST_WEIGHT accounting for the current minute window."""

    def __init__(self, limit_per_minute: int, fraction: float) -> None:
        if not 0 < fraction <= 1:
            raise ValueError("fraction must be in (0, 1]")
        self.limit_per_minute = limit_per_minute
        self.fraction = fraction
        self._minute = -1
        self.used = 0

    @property
    def cap(self) -> int:
        return max(1, int(self.limit_per_minute * self.fraction))

    def delay_for(self, weight: int, wall_s: float) -> float:
        """0 if ``weight`` fits now (and reserves it), else seconds until the next window."""
        minute = int(wall_s // 60)
        if minute != self._minute:
            self._minute, self.used = minute, 0
        if self.used + weight <= self.cap or self.used == 0:
            self.used += weight
            return 0.0
        return (minute + 1) * 60 - wall_s + 0.05

    def observe(self, used_header: int, wall_s: float) -> None:
        if int(wall_s // 60) == self._minute:
            self.used = max(self.used, used_header)


class WindowCountBudget:
    """At most ``limit`` requests per rolling ``window_s`` seconds."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = window_s
        self._sent: deque[float] = deque()

    def delay_for(self, mono_s: float) -> float:
        while self._sent and mono_s - self._sent[0] >= self.window_s:
            self._sent.popleft()
        if len(self._sent) < self.limit:
            self._sent.append(mono_s)
            return 0.0
        return self._sent[0] + self.window_s - mono_s + 0.01


class BinanceUmRest:
    def __init__(
        self,
        base_url: str = "https://fapi.binance.com",
        *,
        weight_limit_per_minute: int = 2400,
        weight_fraction: float = 0.5,
        futures_data_limit: int = 600,
        futures_data_window_s: float = 300.0,
        timeout_s: float = 10.0,
        verify: ssl.SSLContext | bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.weight_budget = MinuteWeightBudget(weight_limit_per_minute, weight_fraction)
        self.futures_data_budget = WindowCountBudget(futures_data_limit, futures_data_window_s)
        self._clock = clock or SystemClock()
        self._blocked_until_mono_s = 0.0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            verify=verify,
            transport=transport,
            headers={"User-Agent": "wt-collector/0.1 (read-only market data)"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def set_weight_limit(self, limit_per_minute: int) -> None:
        self.weight_budget.limit_per_minute = limit_per_minute

    @property
    def blocked_for_s(self) -> float:
        return max(0.0, self._blocked_until_mono_s - time.monotonic())

    async def _acquire(self, path: str, weight: int) -> None:
        async with self._lock:  # serialise budget decisions; requests themselves run concurrently
            while True:
                pause = self.blocked_for_s
                if pause <= 0:
                    pause = self.weight_budget.delay_for(weight, time.time())
                if pause <= 0 and path.startswith("/futures/data/"):
                    pause = self.futures_data_budget.delay_for(time.monotonic())
                if pause <= 0:
                    return
                await asyncio.sleep(pause)

    async def get(
        self, path: str, params: Mapping[str, str | int] | None = None, *, weight: int = 1
    ) -> RestResponse:
        query = dict(params or {})
        await self._acquire(path, weight)
        sent = self._clock.now()
        try:
            response = await self._client.get(path, params=query)
        except httpx.TimeoutException as exc:
            raise TransientError(f"timeout on {path}") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"transport error on {path}: {exc!r}") from exc
        received = self._clock.now()
        used = _int_header(response.headers.get(WEIGHT_HEADER))
        if used is not None:
            self.weight_budget.observe(used, received.wall_ns / NS_PER_S)
        status = response.status_code
        if status in (418, 429):
            retry_after = float(_int_header(response.headers.get("retry-after")) or 60)
            self._blocked_until_mono_s = max(self._blocked_until_mono_s, time.monotonic() + retry_after)
            log.error(
                "rate limited (HTTP %d) on %s; pausing all REST calls for %.0fs", status, path, retry_after
            )
            raise RateLimitedError(
                f"HTTP {status} on {path}", status=status, retry_after_s=retry_after, body=response.content
            )
        if status >= 500:
            raise TransientError(f"HTTP {status} on {path}", status=status, body=response.content)
        if status >= 400:
            raise RestError(f"HTTP {status} on {path}", status=status, body=response.content)
        return RestResponse(path, query, status, response.content, sent, received, used)

    # ------------------------------------------------------------------ endpoints (public only)
    async def exchange_info(self) -> RestResponse:
        return await self.get("/fapi/v1/exchangeInfo", weight=1)

    async def tickers_24h(self) -> RestResponse:
        return await self.get("/fapi/v1/ticker/24hr", weight=40)

    async def depth(self, symbol: str, limit: int = 1000) -> RestResponse:
        if limit not in DEPTH_LIMIT_WEIGHTS:
            raise ValueError(f"unsupported depth limit {limit}")
        return await self.get(
            "/fapi/v1/depth", {"symbol": symbol, "limit": limit}, weight=DEPTH_LIMIT_WEIGHTS[limit]
        )

    async def open_interest(self, symbol: str) -> RestResponse:
        return await self.get("/fapi/v1/openInterest", {"symbol": symbol}, weight=1)

    async def futures_data(self, endpoint: str, symbol: str, period: str, limit: int) -> RestResponse:
        return await self.get(
            f"/futures/data/{endpoint}", {"symbol": symbol, "period": period, "limit": limit}, weight=1
        )

    async def server_time(self) -> RestResponse:
        return await self.get("/fapi/v1/time", weight=1)

    async def funding_info(self) -> RestResponse:
        return await self.get("/fapi/v1/fundingInfo", weight=1)


def _int_header(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None
