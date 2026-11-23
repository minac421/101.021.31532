"""Resilient WebSocket stream for Binance USD-M combined streams.

One ``ManagedStream`` owns one logical subscription (a ``ConnectionSpec`` URL)
and keeps it alive:

* reconnects with exponential backoff and jitter (short-lived sessions also
  back off, so a server that accepts and immediately closes cannot cause a
  tight reconnect loop);
* a silence watchdog replaces half-open connections that stopped delivering;
* **zero-gap rotation**: before the exchange's 24h connection limit a second
  socket is opened, both run in parallel for ``rotation_overlap_s`` after the new
  one delivered its first message, then the old one is closed. Consumers see some
  duplicates during the overlap and de-duplicate by sequence ids (see
  ``wt_data.book`` and ``wt_data.quality``).

The subscription lives in the URL, so no SUBSCRIBE messages are ever sent (the
10 incoming messages/s limit is never at risk). Client pings are disabled: the
server pings every few minutes and the library answers with pongs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from wt_core.clock import NS_PER_S, Clock, Stamp, SystemClock

log = logging.getLogger(__name__)

MessageHandler = Callable[[str, str | bytes, Stamp], None]
EventHandler = Callable[[str, str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class WsSettings:
    max_age_s: float = 23 * 3600.0
    rotation_overlap_s: float = 5.0
    silence_timeout_s: float = 60.0
    open_timeout_s: float = 15.0
    first_message_timeout_s: float = 15.0
    backoff_initial_s: float = 0.5
    backoff_max_s: float = 30.0
    short_session_s: float = 10.0
    max_message_bytes: int = 16 * 1024 * 1024
    max_queue: int = 4096
    check_interval_s: float = 0.25
    use_env_proxy: bool = True


class _Outcome(StrEnum):
    CLOSED = "closed"
    SILENT = "silent"
    ROTATE = "rotate"
    STOP = "stop"


@dataclass(eq=False)
class _Socket:
    ws: ClientConnection
    sid: int
    opened_mono_ns: int
    rotate_after_mono_ns: int
    last_msg_mono_ns: int
    first_message: asyncio.Event = field(default_factory=asyncio.Event)
    messages: int = 0
    reader: asyncio.Task[None] | None = None


@dataclass(slots=True)
class StreamStats:
    connects: int = 0
    connect_failures: int = 0
    disconnects: int = 0
    rotations: int = 0
    messages: int = 0


class ManagedStream:
    def __init__(
        self,
        name: str,
        url: str,
        *,
        on_message: MessageHandler,
        on_event: EventHandler | None = None,
        settings: WsSettings | None = None,
        clock: Clock | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.name = name
        self.url = url
        self.settings = settings or WsSettings()
        self.stats = StreamStats()
        self.first_message = asyncio.Event()  # set once any socket delivered a message
        self._on_message = on_message
        self._on_event = on_event or (lambda _n, _e, _d: None)
        self._clock = clock or SystemClock()
        self._ssl = ssl_context
        self._stop = asyncio.Event()
        self._sid = 0
        self._sockets: set[_Socket] = set()
        self._reconnect_delay = self.settings.backoff_initial_s

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        sock: _Socket | None = None
        try:
            while not self._stop.is_set():
                if sock is None:
                    sock = await self._open()
                    if sock is None:
                        break
                outcome = await self._watch(sock)
                if outcome is _Outcome.STOP:
                    break
                if outcome is _Outcome.ROTATE:
                    sock = await self._rotate(sock)
                    if sock is None:
                        break
                    continue
                lived_s = (self._now().mono_ns - sock.opened_mono_ns) / NS_PER_S
                await self._close(sock)
                self.stats.disconnects += 1
                self._emit(
                    "disconnected", {"sid": sock.sid, "reason": outcome.value, "lived_s": round(lived_s, 3)}
                )
                sock = None
                if lived_s < self.settings.short_session_s:
                    await self._sleep(self._next_delay())
                else:
                    self._reconnect_delay = self.settings.backoff_initial_s
        finally:
            for open_sock in list(self._sockets):
                await self._close(open_sock)
            self._emit("stopped", {})

    async def _rotate(self, old: _Socket) -> _Socket | None:
        new = await self._open()
        if new is None:
            return None
        try:
            await asyncio.wait_for(new.first_message.wait(), timeout=self.settings.first_message_timeout_s)
        except TimeoutError:
            await self._close(new)
            old.rotate_after_mono_ns = self._now().mono_ns + 60 * NS_PER_S
            self._emit("rotation_failed", {"old_sid": old.sid, "new_sid": new.sid})
            return old
        await self._sleep(self.settings.rotation_overlap_s)
        await self._close(old)
        self.stats.rotations += 1
        self._emit("rotated", {"old_sid": old.sid, "new_sid": new.sid})
        return new

    async def _watch(self, sock: _Socket) -> _Outcome:
        silence_ns = round(self.settings.silence_timeout_s * NS_PER_S)
        assert sock.reader is not None
        while True:
            if self._stop.is_set():
                return _Outcome.STOP
            if sock.reader.done():
                return _Outcome.CLOSED
            now = self._now().mono_ns
            if now - sock.last_msg_mono_ns > silence_ns:
                return _Outcome.SILENT
            if now >= sock.rotate_after_mono_ns:
                return _Outcome.ROTATE
            await asyncio.wait({sock.reader}, timeout=self.settings.check_interval_s)

    # ------------------------------------------------------------------ sockets
    async def _open(self) -> _Socket | None:
        delay = self.settings.backoff_initial_s
        while not self._stop.is_set():
            kwargs: dict[str, Any] = {
                "open_timeout": self.settings.open_timeout_s,
                "ping_interval": None,
                "close_timeout": 5,
                "max_size": self.settings.max_message_bytes,
                "max_queue": self.settings.max_queue,
                "proxy": True if self.settings.use_env_proxy else None,
                "user_agent_header": "wt-collector/0.1 (read-only market data)",
            }
            if self._ssl is not None and self.url.startswith("wss://"):
                kwargs["ssl"] = self._ssl
            try:
                ws = await connect(self.url, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # OSError, TimeoutError, InvalidHandshake, InvalidURI, proxy errors
                self.stats.connect_failures += 1
                wait = delay * random.uniform(0.8, 1.2)
                self._emit("connect_failed", {"error": repr(exc)[:300], "retry_in_s": round(wait, 2)})
                await self._sleep(wait)
                delay = min(delay * 2, self.settings.backoff_max_s)
                continue
            self._sid += 1
            now = self._now().mono_ns
            sock = _Socket(
                ws=ws,
                sid=self._sid,
                opened_mono_ns=now,
                rotate_after_mono_ns=now + round(self.settings.max_age_s * NS_PER_S),
                last_msg_mono_ns=now,
            )
            sock.reader = asyncio.create_task(self._read(sock), name=f"ws:{self.name}/s{sock.sid}")
            self._sockets.add(sock)
            self.stats.connects += 1
            self._emit("connected", {"sid": sock.sid})
            return sock
        return None

    async def _read(self, sock: _Socket) -> None:
        origin = f"{self.name}/s{sock.sid}"
        try:
            async for message in sock.ws:
                stamp = self._now()
                sock.last_msg_mono_ns = stamp.mono_ns
                sock.messages += 1
                self.stats.messages += 1
                if not sock.first_message.is_set():
                    sock.first_message.set()
                    self.first_message.set()
                try:
                    self._on_message(origin, message, stamp)
                except Exception:
                    log.exception("message handler failed on %s", origin)
        except ConnectionClosed as exc:
            log.info("socket %s closed: %s", origin, exc)

    async def _close(self, sock: _Socket) -> None:
        self._sockets.discard(sock)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(sock.ws.close(), timeout=5)
        if sock.reader is not None and not sock.reader.done():
            sock.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sock.reader

    # ------------------------------------------------------------------ helpers
    def _next_delay(self) -> float:
        delay = self._reconnect_delay * random.uniform(0.8, 1.2)
        self._reconnect_delay = min(self._reconnect_delay * 2, self.settings.backoff_max_s)
        return delay

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    def _now(self) -> Stamp:
        return self._clock.now()

    def _emit(self, event: str, details: dict[str, Any]) -> None:
        try:
            self._on_event(self.name, event, details)
        except Exception:
            log.exception("event handler failed on %s/%s", self.name, event)
