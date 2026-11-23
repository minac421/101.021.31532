"""A deterministic fake Binance USD-M exchange for integration tests.

* WebSocket server with the routed paths ``/public`` and ``/market``; it
  **enforces** the routing (a stream requested on the wrong path gets no data,
  like the real exchange after the 2026 change) and records every subscription.
* A synthetic market: per-symbol depth diffs linked by ``pu`` (with id skips),
  contiguous aggregate-trade ids, bookTicker and a market-wide mark-price array.
  Every subscriber receives the same events in the same order, so overlapping
  sockets (rotation) produce realistic duplicates.
* REST endpoints via ``httpx.MockTransport``; depth snapshots are consistent
  with the diff stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from websockets.asyncio.server import ServerConnection, serve

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
ROUTE_OF_KIND = {"depth": "public", "bookTicker": "public", "aggTrade": "market", "markPrice": "market"}


@dataclass
class _Book:
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    last_u: int = 10_000


class FakeMarket:
    def __init__(self, symbols: list[str], seed: int = 7) -> None:
        self.rng = random.Random(seed)
        self.symbols = symbols
        self.books = {s: _Book() for s in symbols}
        self.agg_id = dict.fromkeys(symbols, 50_000)
        for book in self.books.values():
            for i in range(10):
                book.bids[Decimal(90 + i)] = Decimal(1 + i % 3)
                book.asks[Decimal(101 + i)] = Decimal(1 + i % 4)
        self.ticks = 0

    def tick(self) -> list[tuple[str, dict[str, Any]]]:
        now = int(time.time() * 1000)
        out: list[tuple[str, dict[str, Any]]] = []
        self.ticks += 1
        for s in self.symbols:
            book = self.books[s]
            changes = self.rng.randint(1, 3)
            first = book.last_u + 1 + self.rng.randint(0, 2)
            last = first + changes - 1
            bids, asks = [], []
            for _ in range(changes):
                qty = Decimal(self.rng.randint(0, 5))
                if self.rng.random() < 0.5:
                    price = Decimal(90 + self.rng.randint(0, 9))
                    bids.append([str(price), str(qty)])
                    book.bids.pop(price, None) if qty == 0 else book.bids.__setitem__(price, qty)
                else:
                    price = Decimal(101 + self.rng.randint(0, 9))
                    asks.append([str(price), str(qty)])
                    book.asks.pop(price, None) if qty == 0 else book.asks.__setitem__(price, qty)
            out.append(
                (
                    f"{s.lower()}@depth@100ms",
                    {
                        "e": "depthUpdate",
                        "E": now,
                        "T": now,
                        "s": s,
                        "U": first,
                        "u": last,
                        "pu": book.last_u,
                        "b": bids,
                        "a": asks,
                    },
                )
            )
            book.last_u = last
            self.agg_id[s] += 1
            out.append(
                (
                    f"{s.lower()}@aggTrade",
                    {
                        "e": "aggTrade",
                        "E": now,
                        "s": s,
                        "a": self.agg_id[s],
                        "p": "100.0",
                        "q": "0.5",
                        "f": self.agg_id[s] * 2,
                        "l": self.agg_id[s] * 2 + 1,
                        "T": now,
                        "m": self.rng.random() < 0.5,
                    },
                )
            )
            out.append(
                (
                    f"{s.lower()}@bookTicker",
                    {
                        "e": "bookTicker",
                        "u": last,
                        "E": now,
                        "T": now,
                        "s": s,
                        "b": "99",
                        "B": "1",
                        "a": "101",
                        "A": "1",
                    },
                )
            )
        if self.ticks % 25 == 0:
            out.append(
                (
                    "!markPrice@arr@1s",
                    [
                        {
                            "e": "markPriceUpdate",
                            "E": now,
                            "s": s,
                            "p": "100.1",
                            "i": "100.0",
                            "P": "100.05",
                            "r": "0.0001",
                            "T": now + 3_600_000,
                        }
                        for s in self.symbols
                    ],
                )
            )
        return out

    def snapshot(self, symbol: str) -> dict[str, Any]:
        book = self.books[symbol]
        now = int(time.time() * 1000)
        return {
            "lastUpdateId": book.last_u,
            "E": now,
            "T": now,
            "bids": [[str(p), str(q)] for p, q in sorted(book.bids.items(), reverse=True)],
            "asks": [[str(p), str(q)] for p, q in sorted(book.asks.items())],
        }


class FakeExchange:
    def __init__(self) -> None:
        self.market = FakeMarket(SYMBOLS)
        self.subscribers: dict[ServerConnection, set[str]] = {}
        self.subscriptions: list[tuple[str, list[str]]] = []
        self.rejected_streams: list[str] = []
        self.rest_calls: list[str] = []
        self.paused = False
        self.port = 0
        self._weight = 0

    # ---------------------------------------------------------------- websocket side
    async def handler(self, ws: ServerConnection) -> None:
        assert ws.request is not None
        parts = urlsplit(ws.request.path)
        route = parts.path.strip("/").split("/")[0]
        streams = parse_qs(parts.query)["streams"][0].split("/")
        self.subscriptions.append((route, streams))
        allowed = set()
        for stream in streams:
            kind = stream.split("@")[1] if not stream.startswith("!") else stream[1:].split("@")[0]
            kind = "depth" if kind.startswith("depth") else kind
            if ROUTE_OF_KIND.get(kind) == route:
                allowed.add(stream)
            else:
                self.rejected_streams.append(f"{route}:{stream}")
        self.subscribers[ws] = allowed
        try:
            await ws.wait_closed()
        finally:
            self.subscribers.pop(ws, None)

    async def pump(self) -> None:
        while True:
            messages = self.market.tick()  # the market moves even while connections are down
            if not self.paused:
                for ws, streams in list(self.subscribers.items()):
                    for stream, data in messages:
                        if stream in streams:
                            with contextlib.suppress(Exception):
                                await ws.send(json.dumps({"stream": stream, "data": data}))
            await asyncio.sleep(0.02)

    async def drop_all_connections(self) -> None:
        for ws in list(self.subscribers):
            with contextlib.suppress(Exception):
                await ws.close()

    # ---------------------------------------------------------------- REST side
    def rest(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, dict(request.url.params)
        self.rest_calls.append(path)
        self._weight += 1
        now = int(time.time() * 1000)
        body: Any
        if path == "/fapi/v1/exchangeInfo":
            body = {
                "serverTime": now,
                "rateLimits": [
                    {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 2400}
                ],
                "symbols": [
                    *(
                        {
                            "symbol": s,
                            "status": "TRADING",
                            "contractType": "PERPETUAL",
                            "quoteAsset": "USDT",
                            "marginAsset": "USDT",
                            "underlyingType": "COIN",
                        }
                        for s in SYMBOLS
                    ),
                    {
                        "symbol": "XAUUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                        "marginAsset": "USDT",
                        "underlyingType": "COMMODITY",
                    },
                    {
                        "symbol": "BTCUSDT_261225",
                        "status": "TRADING",
                        "contractType": "CURRENT_QUARTER",
                        "quoteAsset": "USDT",
                        "marginAsset": "USDT",
                        "underlyingType": "COIN",
                    },
                ],
            }
        elif path == "/fapi/v1/ticker/24hr":
            body = [{"symbol": s, "quoteVolume": str(1e9 / (i + 1))} for i, s in enumerate(SYMBOLS)]
        elif path == "/fapi/v1/depth":
            body = self.market.snapshot(params["symbol"])
        elif path == "/fapi/v1/openInterest":
            body = {"symbol": params["symbol"], "openInterest": "12345.6", "time": now}
        elif path.startswith("/futures/data/"):
            body = [{"symbol": params["symbol"], "sumOpenInterest": "1", "timestamp": now}]
        elif path == "/fapi/v1/time":
            body = {"serverTime": now}
        elif path == "/fapi/v1/fundingInfo":
            body = []
        else:
            return httpx.Response(404, json={"code": -1, "msg": "not found"})
        return httpx.Response(200, json=body, headers={"X-MBX-USED-WEIGHT-1M": str(self._weight)})


@pytest.fixture
async def fake_exchange() -> AsyncIterator[FakeExchange]:
    exchange = FakeExchange()
    async with serve(exchange.handler, "127.0.0.1", 0) as server:
        exchange.port = server.sockets[0].getsockname()[1]
        pump = asyncio.create_task(exchange.pump())
        try:
            yield exchange
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
