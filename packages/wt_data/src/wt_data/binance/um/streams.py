"""Binance USD-M WebSocket stream naming, routing and connection planning.

Since 2026 the USD-M market WebSocket is split into routed paths
(see docs/03-data-sources.md §1-2):

* ``/public`` — high-frequency public data (order book diffs, bookTicker);
* ``/market`` — regular market data (aggTrade, markPrice, kline, ...);
* ``/private`` — user data (listenKey).

The routing table below was verified against Binance's official Python SDK
(binance-connector-python, websocket_streams/streams/public_api.py and
market_api.py, 2026-11) and agrees with NautilusTrader's adapter. It stays
overridable from configuration in case Binance changes it again.

Connection limits: at most 200 streams per connection (we default to 150 for
headroom) and 10 incoming messages per second per connection. Streams are put in
the URL (combined-stream form), so no SUBSCRIBE messages are needed at all.
"""

from __future__ import annotations

import math
import re
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

MAX_STREAMS_PER_CONNECTION = 200  # hard limit documented by Binance


class Route(StrEnum):
    PUBLIC = "public"
    MARKET = "market"
    PRIVATE = "private"


DEFAULT_ROUTES: Mapping[str, Route] = {
    "depth": Route.PUBLIC,  # verified
    "bookTicker": Route.PUBLIC,  # verified
    "aggTrade": Route.MARKET,  # verified
    "markPrice": Route.MARKET,  # verified
    "kline": Route.MARKET,  # verified
    "continuousKline": Route.MARKET,
    "forceOrder": Route.MARKET,  # verified
    "ticker": Route.MARKET,  # verified
    "miniTicker": Route.MARKET,  # verified
    "contractInfo": Route.MARKET,  # verified
}

_KIND_RE = re.compile(r"^(?:!|[a-z0-9_]+@)([A-Za-z]+)")
_SAFE_STREAM_RE = re.compile(r"^[A-Za-z0-9_@!.-]+$")


def stream_kind(stream: str) -> str:
    """The stream type: ``btcusdt@depth@100ms`` -> ``depth``, ``!markPrice@arr@1s`` -> ``markPrice``."""
    match = _KIND_RE.match(stream)
    if match is None:
        raise ValueError(f"not a Binance stream name: {stream!r}")
    return match.group(1)


def stream_symbol(stream: str) -> str | None:
    """Upper-case symbol of a per-symbol stream, ``None`` for market-wide (``!...``) streams."""
    if stream.startswith("!"):
        return None
    symbol, sep, _ = stream.partition("@")
    if not sep:
        raise ValueError(f"not a Binance stream name: {stream!r}")
    return symbol.upper()


def symbol_stream(symbol: str, suffix: str) -> str:
    """``("BTCUSDT", "depth@100ms")`` -> ``"btcusdt@depth@100ms"`` (Binance wants lower case)."""
    return f"{symbol.lower()}@{suffix}"


@dataclass(frozen=True, slots=True)
class ConnectionSpec:
    """One WebSocket connection: a route and a fixed, sorted set of streams."""

    name: str
    route: Route
    streams: tuple[str, ...]

    def url(self, ws_base_url: str) -> str:
        return f"{ws_base_url.rstrip('/')}/{self.route.value}/stream?streams={'/'.join(self.streams)}"


@dataclass(frozen=True, slots=True)
class StreamGroup:
    """Streams that share one connection pool, e.g. all ``aggTrade`` streams."""

    label: str
    route: Route
    streams: tuple[str, ...]


def resolve_route(kind: str, routes: Mapping[str, Route]) -> Route:
    try:
        return routes[kind]
    except KeyError:
        raise ValueError(f"no WebSocket route configured for stream kind {kind!r}") from None


def build_groups(
    *,
    symbols: Iterable[str],
    focus_symbols: Iterable[str],
    per_symbol: Sequence[str],
    focus_streams: Sequence[str],
    market_wide: Sequence[str],
    routes: Mapping[str, Route],
) -> list[StreamGroup]:
    """Expand stream templates into concrete streams, grouped by template and route."""
    all_symbols = sorted(set(symbols))
    focus = sorted(set(focus_symbols))
    groups: list[StreamGroup] = []
    for suffix in per_symbol:
        route = resolve_route(stream_kind(f"x@{suffix}"), routes)
        groups.append(StreamGroup(suffix, route, tuple(symbol_stream(s, suffix) for s in all_symbols)))
    for suffix in focus_streams:
        route = resolve_route(stream_kind(f"x@{suffix}"), routes)
        groups.append(StreamGroup(f"focus-{suffix}", route, tuple(symbol_stream(s, suffix) for s in focus)))
    by_route: dict[Route, list[str]] = {}
    for stream in market_wide:
        if not stream.startswith("!"):
            raise ValueError(f"market-wide streams start with '!': {stream!r}")
        by_route.setdefault(resolve_route(stream_kind(stream), routes), []).append(stream)
    for route, streams in sorted(by_route.items()):
        groups.append(StreamGroup("market-wide", route, tuple(streams)))
    for group in groups:
        for stream in group.streams:
            if not _SAFE_STREAM_RE.match(stream):
                raise ValueError(f"unsafe characters in stream name {stream!r}")
    return [g for g in groups if g.streams]


def _label_slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")


def shard_group(
    group: StreamGroup,
    *,
    max_streams_per_connection: int,
    fill_factor: float = 0.8,
    max_url_length: int = 7000,
    ws_base_url: str = "wss://fstream.binance.com",
) -> list[ConnectionSpec]:
    """Split a group into connections with *stable* assignment.

    Streams are assigned by CRC32 hash, so adding or removing one symbol changes a
    single connection instead of reshuffling all of them (fewer reconnects when the
    universe changes). ``fill_factor`` leaves room for growth before the shard
    count — and therefore the assignment — has to change.
    """
    if not 1 <= max_streams_per_connection <= MAX_STREAMS_PER_CONNECTION:
        raise ValueError(f"max_streams_per_connection must be in [1, {MAX_STREAMS_PER_CONNECTION}]")
    if not 0.1 <= fill_factor <= 1.0:
        raise ValueError("fill_factor must be in [0.1, 1.0]")
    streams = sorted(set(group.streams))
    shards = max(1, math.ceil(len(streams) / (max_streams_per_connection * fill_factor)))
    while True:
        buckets: list[list[str]] = [[] for _ in range(shards)]
        for stream in streams:
            buckets[zlib.crc32(stream.encode()) % shards].append(stream)
        specs = [
            ConnectionSpec(f"{group.route.value}-{_label_slug(group.label)}-{i:02d}", group.route, tuple(b))
            for i, b in enumerate(buckets)
            if b
        ]
        too_big = any(
            len(s.streams) > max_streams_per_connection or len(s.url(ws_base_url)) > max_url_length
            for s in specs
        )
        if not too_big:
            return specs
        if shards >= len(streams):
            raise ValueError("cannot satisfy URL length limit even with one stream per connection")
        shards += 1


def plan_connections(
    groups: Iterable[StreamGroup],
    *,
    max_streams_per_connection: int,
    max_url_length: int = 7000,
    ws_base_url: str = "wss://fstream.binance.com",
) -> list[ConnectionSpec]:
    specs: list[ConnectionSpec] = []
    for group in groups:
        specs.extend(
            shard_group(
                group,
                max_streams_per_connection=max_streams_per_connection,
                max_url_length=max_url_length,
                ws_base_url=ws_base_url,
            )
        )
    names = [s.name for s in specs]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate connection names in plan: {names}")
    return specs
