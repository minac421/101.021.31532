"""ManagedStream against a local WebSocket server: reconnects, silence watchdog, zero-gap rotation."""

import asyncio
import contextlib
import itertools
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from wt_core.clock import Stamp
from wt_data.binance.um.ws import ManagedStream, WsSettings

Handler = Callable[[ServerConnection], Awaitable[None]]


@contextlib.asynccontextmanager
async def server(handler: Handler) -> AsyncIterator[str]:
    async with serve(handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}/market/stream?streams=test@aggTrade"


def settings(**overrides: Any) -> WsSettings:
    base: dict[str, Any] = {
        "backoff_initial_s": 0.05,
        "backoff_max_s": 0.2,
        "check_interval_s": 0.02,
        "short_session_s": 0.0,
        "use_env_proxy": False,
    }
    return WsSettings(**(base | overrides))


class Sink:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []

    def on_message(self, origin: str, raw: str | bytes, stamp: Stamp) -> None:
        self.messages.append((origin, json.loads(raw)))

    def on_event(self, name: str, event: str, details: dict[str, Any]) -> None:
        self.events.append((event, details))

    def event_names(self) -> list[str]:
        return [e for e, _ in self.events]


async def run_until(stream: ManagedStream, condition: Callable[[], bool], limit_s: float = 10.0) -> None:
    task = asyncio.create_task(stream.run())
    try:
        async with asyncio.timeout(limit_s):
            while not condition():  # noqa: ASYNC110 — polling an arbitrary predicate
                await asyncio.sleep(0.02)
    finally:
        stream.stop()
        await asyncio.wait_for(task, 5)


async def test_reconnects_after_the_server_closes() -> None:
    connections = itertools.count(1)

    async def handler(ws: ServerConnection) -> None:
        n = next(connections)
        for i in range(3):
            await ws.send(json.dumps({"conn": n, "i": i}))
        if n == 1:
            await ws.close()
        else:
            await asyncio.sleep(10)

    sink = Sink()
    async with server(handler) as url:
        stream = ManagedStream(
            "t", url, on_message=sink.on_message, on_event=sink.on_event, settings=settings()
        )
        await run_until(stream, lambda: len(sink.messages) >= 6)
    assert {m["conn"] for _, m in sink.messages} == {1, 2}
    assert "disconnected" in sink.event_names()
    assert stream.stats.connects >= 2


async def test_silent_connection_is_replaced() -> None:
    connections = itertools.count(1)

    async def handler(ws: ServerConnection) -> None:
        n = next(connections)
        if n == 1:
            await ws.send(json.dumps({"conn": 1}))
            await asyncio.sleep(10)  # then goes silent (half-open from the client's view)
        else:
            while True:
                await ws.send(json.dumps({"conn": n}))
                await asyncio.sleep(0.02)

    sink = Sink()
    async with server(handler) as url:
        stream = ManagedStream(
            "t",
            url,
            on_message=sink.on_message,
            on_event=sink.on_event,
            settings=settings(silence_timeout_s=0.3),
        )
        await run_until(stream, lambda: any(m["conn"] == 2 for _, m in sink.messages))
    reasons = [d.get("reason") for e, d in sink.events if e == "disconnected"]
    assert "silent" in reasons


async def test_rotation_overlaps_sockets_without_losing_messages() -> None:
    sequence = itertools.count()
    subscribers: set[ServerConnection] = set()

    async def broadcaster() -> None:
        while True:
            message = json.dumps({"seq": next(sequence)})
            for ws in list(subscribers):
                with contextlib.suppress(Exception):
                    await ws.send(message)
            await asyncio.sleep(0.005)

    async def handler(ws: ServerConnection) -> None:
        subscribers.add(ws)
        try:
            await ws.wait_closed()
        finally:
            subscribers.discard(ws)

    sink = Sink()
    pump = asyncio.create_task(broadcaster())
    try:
        async with server(handler) as url:
            stream = ManagedStream(
                "t",
                url,
                on_message=sink.on_message,
                on_event=sink.on_event,
                settings=settings(max_age_s=0.4, rotation_overlap_s=0.1, first_message_timeout_s=2),
            )
            await run_until(stream, lambda: stream.stats.rotations >= 2)
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump

    assert "rotated" in sink.event_names()
    origins = {o for o, _ in sink.messages}
    assert len(origins) >= 3  # s1, s2, s3 each delivered data
    seqs = sorted({m["seq"] for _, m in sink.messages})
    assert seqs == list(range(seqs[0], seqs[-1] + 1)), "rotation must not lose messages"
    assert len(sink.messages) > len(seqs), "overlap delivers duplicates that consumers de-duplicate"


async def test_connect_failures_back_off_and_stop_cleanly() -> None:
    sink = Sink()
    stream = ManagedStream(
        "t",
        "ws://127.0.0.1:9/market/stream?streams=x@aggTrade",
        on_message=sink.on_message,
        on_event=sink.on_event,
        settings=settings(open_timeout_s=0.5),
    )
    with pytest.raises(TimeoutError):
        await run_until(stream, lambda: False, limit_s=0.5)
    assert stream.stats.connect_failures >= 2
    assert "stopped" in sink.event_names()
