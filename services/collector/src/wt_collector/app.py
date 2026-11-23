"""The read-only market data recorder.

Responsibilities (docs/11-roadmap.md §1 — "start recording now"):

* select the universe (all eligible USD-M perpetuals) and the focus set
  (symbols that get full order books);
* keep the planned WebSocket connections alive (``ManagedStream``) and record
  every message exactly as received;
* keep local order books in sync, fetching REST snapshots on start and after
  any gap, and recording those snapshots — together with the recorded diffs
  they make every book reconstructible offline;
* poll REST statistics that have short exchange-side history (open interest,
  long/short ratios, taker volume) and record the raw responses;
* measure data quality and clock offset, and export Prometheus metrics.

It uses public endpoints only: it has no API keys and cannot place orders.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import heapq
import logging
import math
import ssl
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgspec

from wt_collector.config import CollectorConfig, FocusConfig, UniverseConfig
from wt_collector.metrics import CollectorMetrics
from wt_core.clock import NS_PER_MS, Clock, Stamp, SystemClock
from wt_data.binance.um.messages import (
    ExchangeInfo,
    Ticker24h,
    agg_trade_decoder,
    depth_snapshot_decoder,
    depth_update_decoder,
    envelope_decoder,
    event_time_ms,
    exchange_info_decoder,
    server_time_decoder,
    tickers_decoder,
)
from wt_data.binance.um.rest import BinanceUmRest, RateLimitedError, RestError, RestResponse
from wt_data.binance.um.streams import (
    ConnectionSpec,
    build_groups,
    plan_connections,
    stream_kind,
    stream_symbol,
)
from wt_data.binance.um.ws import ManagedStream, WsSettings
from wt_data.book import OrderBook, SnapshotResult
from wt_data.quality import QualityMonitor
from wt_data.recorder import RawRecorder

log = logging.getLogger(__name__)

PERIOD_SECONDS = {
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14_400,
    "6h": 21_600,
    "12h": 43_200,
    "1d": 86_400,
}
_RESYNC, _ANCHOR = 0, 1


# ---------------------------------------------------------------------- pure selection logic
def select_universe(info: ExchangeInfo, cfg: UniverseConfig) -> list[str]:
    """Eligible symbols (sorted). ``include`` forces a symbol in if it is trading; ``exclude`` wins."""
    selected = []
    for s in info.symbols:
        if s.symbol in cfg.exclude or s.status != cfg.status:
            continue
        eligible = (
            s.contractType in cfg.contract_types
            and s.quoteAsset == cfg.quote_asset
            and (not cfg.underlying_types or not s.underlyingType or s.underlyingType in cfg.underlying_types)
        )
        if eligible or s.symbol in cfg.include:
            selected.append(s.symbol)
    return sorted(selected)


def rank_by_quote_volume(symbols: Iterable[str], tickers: Iterable[Ticker24h]) -> list[str]:
    volume = {}
    for t in tickers:
        try:
            volume[t.symbol] = float(t.quoteVolume)
        except ValueError:
            continue
    return sorted(symbols, key=lambda s: (-volume.get(s, 0.0), s))


def select_focus(universe: list[str], ranked: list[str], cfg: FocusConfig) -> list[str]:
    """Static focus symbols (if eligible) plus the top-N most liquid others."""
    allowed = set(universe)
    static = [s for s in cfg.static if s in allowed]
    extra = [s for s in ranked if s in allowed and s not in static][: cfg.top_by_quote_volume]
    return sorted({*static, *extra})


def is_diff_depth_stream(stream: str) -> bool:
    """``x@depth`` / ``x@depth@100ms`` carry diffs; ``x@depth20@100ms`` are partial snapshots."""
    _, _, suffix = stream.partition("@")
    return suffix == "depth" or suffix.startswith("depth@")


@dataclass(slots=True)
class _RunningStream:
    spec: ConnectionSpec
    stream: ManagedStream
    task: asyncio.Task[None]


class Collector:
    def __init__(
        self,
        config: CollectorConfig,
        *,
        rest: BinanceUmRest | None = None,
        metrics: CollectorMetrics | None = None,
        clock: Clock | None = None,
        ssl_context: ssl.SSLContext | None = None,
        instance_id: str | None = None,
    ) -> None:
        self.cfg = config
        self.clock = clock or SystemClock()
        self.metrics = metrics or CollectorMetrics()
        self.rest = rest or BinanceUmRest(
            config.exchange.rest_base_url,
            weight_limit_per_minute=config.rest.weight_limit_per_minute or 2400,
            weight_fraction=config.rest.max_weight_fraction,
            futures_data_limit=config.rest.futures_data_requests_per_5min,
            timeout_s=config.rest.timeout_s,
            verify=ssl_context if ssl_context is not None else True,
            clock=self.clock,
        )
        self.recorder = RawRecorder(
            Path(config.recorder.root),
            source=config.recorder.source,
            zstd_level=config.recorder.zstd_level,
            flush_interval_s=config.recorder.flush_interval_s,
            queue_max=config.recorder.queue_max,
            instance_id=instance_id,
        )
        self.quality = QualityMonitor(reorder_window_s=config.quality.reorder_window_s)
        self.books: dict[str, OrderBook] = {}
        self.universe: list[str] = []
        self.focus: list[str] = []
        self._ranked: list[str] = []
        self._ssl = ssl_context
        self._ws_settings = WsSettings(
            max_age_s=config.ws.max_age_s,
            rotation_overlap_s=config.ws.rotation_overlap_s,
            silence_timeout_s=config.ws.silence_timeout_s,
            open_timeout_s=config.ws.open_timeout_s,
            first_message_timeout_s=config.ws.open_timeout_s,
            backoff_initial_s=config.ws.backoff_initial_s,
            backoff_max_s=config.ws.backoff_max_s,
            use_env_proxy=config.ws.use_env_proxy,
        )
        self._running: dict[str, _RunningStream] = {}
        self._plan_lock = asyncio.Lock()
        self._snapshots: asyncio.PriorityQueue[tuple[int, int, str]] = asyncio.PriorityQueue()
        self._snapshot_pending: set[str] = set()
        self._snapshot_seq = 0
        self._tasks: list[asyncio.Task[None]] = []
        self.ready = asyncio.Event()  # set once the first plan is running

    # ================================================================== lifecycle
    async def run(self, stop: asyncio.Event) -> None:
        self.recorder.start()
        if self.cfg.metrics.enabled:
            self.metrics.serve(self.cfg.metrics.host, self.cfg.metrics.port)
        self._record_control("collector_started", {"config": msgspec.to_builtins(self.cfg)})
        try:
            if not await self._bootstrap(stop):
                return
            for coro in (
                self._snapshot_loop(),
                self._anchor_loop(),
                self._housekeeping_loop(),
                self._every(self.cfg.quality.summary_interval_s, self._quality_tick, "quality"),
                self._every(self.cfg.pollers.server_time_s, self._poll_server_time, "server_time"),
                self._every(
                    self.cfg.pollers.exchange_info_s,
                    self._poll_exchange_info,
                    "exchange_info",
                    initial_delay=True,
                ),
                self._every(
                    self.cfg.focus.refresh_s, self._refresh_focus, "focus_refresh", initial_delay=True
                ),
                self._every(self.cfg.pollers.funding_info_s, self._poll_funding_info, "funding_info"),
                self._symbol_scheduler("open_interest", self._oi_jobs, self._fetch_open_interest),
                self._symbol_scheduler("futures_stats", self._stats_jobs, self._fetch_futures_stats),
            ):
                self._tasks.append(asyncio.create_task(coro))
            self.ready.set()
            await stop.wait()
        finally:
            await self._shutdown()

    async def _bootstrap(self, stop: asyncio.Event) -> bool:
        delay = 1.0
        while not stop.is_set():
            try:
                info = await self._fetch_exchange_info()
                tickers = await self._fetch_tickers()
                break
            except RestError as exc:
                log.warning("bootstrap failed (%s); retrying in %.0fs", exc, delay)
                self._record_control("bootstrap_failed", {"error": str(exc)})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                delay = min(delay * 2, 60.0)
        else:
            return False
        universe = select_universe(info, self.cfg.universe)
        self._ranked = rank_by_quote_volume(universe, tickers)
        if self.cfg.universe.max_symbols is not None:
            keep = set(self._ranked[: self.cfg.universe.max_symbols]) | set(self.cfg.universe.include)
            universe = sorted(s for s in universe if s in keep)
        focus = select_focus(universe, self._ranked, self.cfg.focus)
        await self._apply_universe(universe, focus)
        return True

    async def _shutdown(self) -> None:
        for running in self._running.values():
            running.stream.stop()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(r.task for r in self._running.values()), return_exceptions=True), timeout=15
            )
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.rest.close()
        self._record_control("collector_stopped", {})
        await asyncio.to_thread(self.recorder.close)

    # ================================================================== universe & plan
    async def _apply_universe(self, universe: list[str], focus: list[str]) -> None:
        async with self._plan_lock:
            self.universe, self.focus = universe, focus
            for symbol in focus:
                if symbol not in self.books:
                    self.books[symbol] = OrderBook(
                        symbol, reorder_window_s=self.cfg.quality.book_reorder_window_s
                    )
            for symbol in [s for s in self.books if s not in focus]:
                del self.books[symbol]
                with contextlib.suppress(KeyError):
                    self.metrics.book_live.remove(symbol)
            groups = build_groups(
                symbols=universe,
                focus_symbols=focus,
                per_symbol=self.cfg.streams.per_symbol,
                focus_streams=self.cfg.streams.focus,
                market_wide=self.cfg.streams.market_wide,
                routes=self.cfg.exchange.effective_routes,
            )
            plan = plan_connections(
                groups,
                max_streams_per_connection=self.cfg.exchange.max_streams_per_connection,
                max_url_length=self.cfg.exchange.max_url_length,
                ws_base_url=self.cfg.exchange.ws_base_url,
            )
            await self._apply_plan(plan)
            for symbol, book in self.books.items():
                if book.needs_snapshot:
                    self._request_snapshot(symbol, _RESYNC)
            self.metrics.universe_size.set(len(universe))
            self.metrics.focus_size.set(len(focus))
            self._record_control(
                "plan_applied",
                {
                    "universe": universe,
                    "focus": focus,
                    "connections": [
                        {"name": s.name, "route": s.route.value, "streams": len(s.streams)} for s in plan
                    ],
                },
            )

    async def _apply_plan(self, plan: list[ConnectionSpec]) -> None:
        wanted = {spec.name: spec for spec in plan}
        starting: list[_RunningStream] = []
        retiring: list[_RunningStream] = []
        for name, spec in wanted.items():
            current = self._running.get(name)
            if current is not None and current.spec == spec:
                continue
            starting.append(self._start_stream(spec))
            if current is not None:
                retiring.append(current)
        retiring.extend(r for name, r in self._running.items() if name not in wanted)
        if starting and retiring:  # overlap: let replacements deliver before stopping what they replace
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*(r.stream.first_message.wait() for r in starting)),
                    timeout=self.cfg.ws.open_timeout_s,
                )
            await asyncio.sleep(self.cfg.ws.rotation_overlap_s)
        for running in retiring:
            await self._stop_stream(running)
            if self._running.get(running.spec.name) is running:
                del self._running[running.spec.name]
        for running in starting:
            self._running[running.spec.name] = running
        self.metrics.ws_streams.set(len(self._running))

    def _start_stream(self, spec: ConnectionSpec) -> _RunningStream:
        stream = ManagedStream(
            spec.name,
            spec.url(self.cfg.exchange.ws_base_url),
            on_message=self._on_ws_message,
            on_event=self._on_ws_event,
            settings=self._ws_settings,
            clock=self.clock,
            ssl_context=self._ssl,
        )
        return _RunningStream(spec, stream, asyncio.create_task(stream.run(), name=f"stream:{spec.name}"))

    async def _stop_stream(self, running: _RunningStream) -> None:
        running.stream.stop()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(running.task, timeout=15)

    # ================================================================== hot path
    def _on_ws_message(self, origin: str, raw: str | bytes, stamp: Stamp) -> None:
        data = raw.encode() if isinstance(raw, str) else raw
        try:
            envelope = envelope_decoder.decode(data)
            kind = stream_kind(envelope.stream)
        except (msgspec.DecodeError, ValueError):
            self.metrics.ws_undecodable.inc()
            with contextlib.suppress(ValueError, msgspec.DecodeError):
                self.recorder.record(
                    "ws.undecodable",
                    recv_ns=stamp.wall_ns,
                    mono_ns=stamp.mono_ns,
                    origin=origin,
                    payload=data,
                )
            return
        self.recorder.record(
            f"ws.{kind}", recv_ns=stamp.wall_ns, mono_ns=stamp.mono_ns, origin=origin, payload=data
        )
        self.metrics.ws_messages.labels(kind).inc()
        lag_ms = self.quality.on_message(kind, event_time_ms(envelope.data), stamp)
        if lag_ms is not None:
            self.metrics.stream_lag_ms.labels(kind).observe(lag_ms)
        if kind == "aggTrade":
            trade = agg_trade_decoder.decode(envelope.data)
            self.quality.on_agg_trade(trade.s, trade.a, stamp.mono_ns)
        elif kind == "depth" and is_diff_depth_stream(envelope.stream):
            symbol = stream_symbol(envelope.stream)
            book = self.books.get(symbol) if symbol else None
            if book is not None:
                result = book.on_diff(depth_update_decoder.decode(envelope.data), stamp.mono_ns)
                self.metrics.book_results.labels(book.symbol, result.value).inc()
                if book.needs_snapshot:
                    self._request_snapshot(book.symbol, _RESYNC)

    def _on_ws_event(self, connection: str, event: str, details: dict[str, Any]) -> None:
        self.metrics.ws_events.labels(connection, event).inc()
        level = (
            logging.WARNING
            if event in ("disconnected", "connect_failed", "rotation_failed")
            else logging.INFO
        )
        log.log(level, "ws %s: %s %s", connection, event, details)
        self._record_control(f"ws_{event}", {"connection": connection, **details})

    # ================================================================== order book snapshots
    def _request_snapshot(self, symbol: str, priority: int) -> None:
        if priority == _RESYNC:
            if symbol in self._snapshot_pending:
                return
            self._snapshot_pending.add(symbol)
        self._snapshot_seq += 1
        self._snapshots.put_nowait((priority, self._snapshot_seq, symbol))

    async def _snapshot_loop(self) -> None:
        while True:
            priority, _, symbol = await self._snapshots.get()
            book = self.books.get(symbol)
            if book is None or (priority == _RESYNC and not book.needs_snapshot):
                self._snapshot_pending.discard(symbol)
                continue
            if priority == _RESYNC and not book.has_buffered_events:
                # The snapshot must not predate our buffer: wait until the diff stream delivers.
                await asyncio.sleep(0.1)
                self._requeue_snapshot(priority, symbol)
                continue
            try:
                response = await self.rest.depth(symbol, self.cfg.pollers.depth_snapshot_limit)
            except RateLimitedError as exc:
                await asyncio.sleep(exc.retry_after_s)
                self._requeue_snapshot(priority, symbol)
                continue
            except RestError as exc:
                self.metrics.rest_requests.labels("depth", "error").inc()
                log.warning("depth snapshot for %s failed: %s", symbol, exc)
                await asyncio.sleep(1.0)
                self._requeue_snapshot(priority, symbol)
                continue
            self._record_rest("depth", response)
            purpose, outcome = ("resync" if priority == _RESYNC else "anchor"), "recorded"
            if book.needs_snapshot and self.books.get(symbol) is book:
                result = book.apply_snapshot(depth_snapshot_decoder.decode(response.content))
                outcome = result.value
                if result is SnapshotResult.TOO_OLD:
                    self._requeue_snapshot(_RESYNC, symbol)
            self.metrics.book_snapshots.labels(symbol, purpose, outcome).inc()
            if not book.needs_snapshot:
                self._snapshot_pending.discard(symbol)

    def _requeue_snapshot(self, priority: int, symbol: str) -> None:
        self._snapshot_seq += 1
        self._snapshots.put_nowait((priority, self._snapshot_seq, symbol))

    async def _anchor_loop(self) -> None:
        """Periodic snapshots of live books: anchors that make offline reconstruction robust."""
        index = 0
        while True:
            focus = list(self.focus)
            await asyncio.sleep(self.cfg.pollers.depth_anchor_s / max(1, len(focus)))
            if not focus:
                continue
            symbol = focus[index % len(focus)]
            index += 1
            book = self.books.get(symbol)
            if book is not None and book.is_valid:
                self._request_snapshot(symbol, _ANCHOR)

    # ================================================================== periodic work
    async def _housekeeping_loop(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            now = self.clock.now()
            for symbol, book in list(self.books.items()):
                if book.check_timeouts(now.mono_ns):
                    self._request_snapshot(symbol, _RESYNC)
                self.metrics.book_live.labels(symbol).set(1 if book.is_valid else 0)
            for symbol, count in self.quality.expire(now.mono_ns).items():
                self.metrics.agg_trade_missing.labels(symbol).inc(count)
            self.metrics.rest_blocked_s.set(self.rest.blocked_for_s)

    async def _quality_tick(self) -> None:
        now = self.clock.now()
        summary = self.quality.summary_and_reset(now)
        summary["books"] = {
            s: {"state": b.state.value, "valid": b.is_valid, **dataclasses.asdict(b.stats)}
            for s, b in self.books.items()
        }
        stats = self.recorder.stats
        summary["recorder"] = {**dataclasses.asdict(stats), "queue_depth": self.recorder.queue_depth}
        summary["rest"] = {
            "used_weight_1m": self.rest.weight_budget.used,
            "blocked_s": self.rest.blocked_for_s,
        }
        self.recorder.record_event(
            "quality.summary", recv_ns=now.wall_ns, mono_ns=now.mono_ns, origin="collector", event=summary
        )
        for kind, age in self.quality.seconds_since_last(now).items():
            self.metrics.last_message_age_s.labels(kind).set(age)
        self.metrics.recorder_enqueued.set(stats.enqueued)
        self.metrics.recorder_dropped.set(stats.dropped)
        self.metrics.recorder_written.set(stats.written_lines)
        self.metrics.recorder_bytes.set(stats.written_bytes)
        self.metrics.recorder_errors.set(stats.write_errors)
        self.metrics.recorder_queue.set(self.recorder.queue_depth)
        with contextlib.suppress(OSError):
            self.metrics.disk_free.set(self.recorder.disk_free_bytes())
        if stats.dropped:
            log.error("raw recorder dropped %d records so far (queue full) — data is DEGRADED", stats.dropped)

    async def _every(
        self, interval_s: float, fn: Callable[[], Awaitable[None]], name: str, *, initial_delay: bool = False
    ) -> None:
        if initial_delay:
            await asyncio.sleep(interval_s)
        while True:
            started = time.monotonic()
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except RateLimitedError as exc:
                await asyncio.sleep(exc.retry_after_s)
            except Exception:
                log.exception("periodic task %s failed", name)
            await asyncio.sleep(max(0.0, interval_s - (time.monotonic() - started)))

    async def _fetch_exchange_info(self) -> ExchangeInfo:
        response = await self.rest.exchange_info()
        self._record_rest("exchangeInfo", response)
        info = exchange_info_decoder.decode(response.content)
        if self.cfg.rest.weight_limit_per_minute is None:
            for limit in info.rateLimits:
                if (
                    limit.rateLimitType == "REQUEST_WEIGHT"
                    and limit.interval == "MINUTE"
                    and limit.intervalNum == 1
                ):
                    self.rest.set_weight_limit(limit.limit)
        return info

    async def _fetch_tickers(self) -> list[Ticker24h]:
        response = await self.rest.tickers_24h()
        self._record_rest("ticker24hr", response)
        return tickers_decoder.decode(response.content)

    async def _poll_exchange_info(self) -> None:
        info = await self._fetch_exchange_info()
        universe = select_universe(info, self.cfg.universe)
        if self.cfg.universe.max_symbols is not None:
            keep = set(self._ranked[: self.cfg.universe.max_symbols]) | set(self.cfg.universe.include)
            universe = sorted(s for s in universe if s in keep)
        if universe != self.universe:
            focus = [s for s in self.focus if s in set(universe)]
            log.info(
                "universe changed: +%s -%s",
                sorted(set(universe) - set(self.universe)),
                sorted(set(self.universe) - set(universe)),
            )
            await self._apply_universe(universe, focus)

    async def _refresh_focus(self) -> None:
        tickers = await self._fetch_tickers()
        self._ranked = rank_by_quote_volume(self.universe, tickers)
        focus = select_focus(self.universe, self._ranked, self.cfg.focus)
        if focus != self.focus:
            await self._apply_universe(self.universe, focus)

    async def _poll_server_time(self) -> None:
        response = await self.rest.server_time()
        self._record_rest("time", response)
        server_ms = server_time_decoder.decode(response.content).serverTime
        local_mid_ms = (response.sent.wall_ns + response.received.wall_ns) / 2 / NS_PER_MS
        offset = server_ms - local_mid_ms
        self.metrics.clock_offset_ms.set(offset)
        if abs(offset) > 250:
            log.warning(
                "clock offset vs exchange is %.0f ms (RTT %.0f ms) — check chrony/NTP",
                offset,
                response.latency_ms,
            )

    async def _poll_funding_info(self) -> None:
        self._record_rest("fundingInfo", await self.rest.funding_info())

    # ================================================================== per-symbol schedulers
    def _oi_jobs(self) -> dict[str, float]:
        return dict.fromkeys(self.universe, self.cfg.pollers.open_interest_s)

    async def _fetch_open_interest(self, job: str, interval_s: float) -> None:
        self._record_rest("openInterest", await self.rest.open_interest(job))

    def _stats_jobs(self) -> dict[str, float]:
        focus = set(self.focus)
        p = self.cfg.pollers
        return {
            f"{endpoint}|{s}": (p.futures_stats_focus_s if s in focus else p.futures_stats_other_s)
            for endpoint in p.futures_stats_endpoints
            for s in self.universe
        }

    async def _fetch_futures_stats(self, job: str, interval_s: float) -> None:
        endpoint, symbol = job.split("|", 1)
        period = self.cfg.pollers.futures_stats_period
        limit = min(500, math.ceil(interval_s / PERIOD_SECONDS[period]) + 1)
        response = await self.rest.futures_data(endpoint, symbol, period, limit)
        self._record_rest(f"futures_data.{endpoint}", response)

    async def _symbol_scheduler(
        self,
        name: str,
        jobs: Callable[[], dict[str, float]],
        fetch: Callable[[str, float], Awaitable[None]],
    ) -> None:
        """Run every job at its own interval, spreading requests evenly over time.

        The job set is re-read continuously, so universe/focus changes take effect
        without restarts; stale heap entries are skipped via ``next_due``.
        """
        heap: list[tuple[float, str]] = []
        next_due: dict[str, float] = {}
        while True:
            current = jobs()
            now = time.monotonic()
            new_jobs = sorted(set(current) - set(next_due))
            for i, job in enumerate(new_jobs):
                due = now + current[job] * (i + 1) / (len(new_jobs) + 1)
                next_due[job] = due
                heapq.heappush(heap, (due, job))
            for job in [j for j in next_due if j not in current]:
                del next_due[job]
            if not heap:
                await asyncio.sleep(1.0)
                continue
            due, job = heap[0]
            if next_due.get(job) != due:
                heapq.heappop(heap)  # stale entry
                continue
            if due > now:
                await asyncio.sleep(min(due - now, 1.0))
                continue
            heapq.heappop(heap)
            interval = current[job]
            try:
                await fetch(job, interval)
            except asyncio.CancelledError:
                raise
            except RateLimitedError as exc:
                await asyncio.sleep(exc.retry_after_s)
            except RestError as exc:
                self.metrics.rest_requests.labels(name, "error").inc()
                log.warning("%s job %s failed: %s", name, job, exc)
            except Exception:
                log.exception("%s job %s crashed", name, job)
            following = max(due + interval, time.monotonic())
            next_due[job] = following
            heapq.heappush(heap, (following, job))

    # ================================================================== recording helpers
    def _record_rest(self, kind: str, response: RestResponse) -> None:
        self.recorder.record(
            f"rest.{kind}",
            recv_ns=response.received.wall_ns,
            mono_ns=response.received.mono_ns,
            origin=response.origin,
            payload=response.content,
        )
        self.metrics.rest_requests.labels(kind, "ok").inc()
        self.metrics.rest_latency_ms.labels(kind).observe(response.latency_ms)
        if response.used_weight_1m is not None:
            self.metrics.rest_used_weight.set(response.used_weight_1m)

    def _record_control(self, event: str, details: dict[str, Any]) -> None:
        now = self.clock.now()
        self.recorder.record_event(
            "control",
            recv_ns=now.wall_ns,
            mono_ns=now.mono_ns,
            origin="collector",
            event={"event": event, **details},
        )


__all__ = ["Collector", "is_diff_depth_stream", "rank_by_quote_volume", "select_focus", "select_universe"]
