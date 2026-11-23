"""Prometheus metrics for the collector (one registry per collector instance)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

_LAG_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10_000)


class CollectorMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.ws_messages = Counter("wt_ws_messages", "WebSocket messages received", ["kind"], registry=r)
        self.ws_undecodable = Counter(
            "wt_ws_undecodable", "WebSocket messages that failed to decode", registry=r
        )
        self.ws_events = Counter(
            "wt_ws_connection_events",
            "WebSocket connection lifecycle events",
            ["connection", "event"],
            registry=r,
        )
        self.ws_streams = Gauge("wt_ws_planned_connections", "Planned WebSocket connections", registry=r)
        self.stream_lag_ms = Histogram(
            "wt_stream_lag_ms",
            "Local receive time minus exchange event time",
            ["kind"],
            buckets=_LAG_BUCKETS_MS,
            registry=r,
        )
        self.last_message_age_s = Gauge(
            "wt_last_message_age_seconds",
            "Seconds since the last message per stream kind",
            ["kind"],
            registry=r,
        )
        self.agg_trade_missing = Counter(
            "wt_agg_trade_missing_ids", "Aggregate trade ids confirmed missing", ["symbol"], registry=r
        )
        self.book_live = Gauge(
            "wt_book_live", "1 if the local order book is live and valid", ["symbol"], registry=r
        )
        self.book_results = Counter(
            "wt_book_diff_results", "Order book diff outcomes", ["symbol", "result"], registry=r
        )
        self.book_snapshots = Counter(
            "wt_book_snapshots", "Depth snapshots fetched", ["symbol", "purpose", "result"], registry=r
        )
        self.rest_requests = Counter("wt_rest_requests", "REST requests", ["endpoint", "outcome"], registry=r)
        self.rest_latency_ms = Histogram(
            "wt_rest_latency_ms",
            "REST latency",
            ["endpoint"],
            buckets=(10, 25, 50, 100, 250, 500, 1000, 3000),
            registry=r,
        )
        self.rest_used_weight = Gauge(
            "wt_rest_used_weight_1m", "Server-reported used request weight", registry=r
        )
        self.rest_blocked_s = Gauge(
            "wt_rest_blocked_seconds", "Seconds REST calls stay paused (429/418)", registry=r
        )
        self.clock_offset_ms = Gauge(
            "wt_clock_offset_ms", "Exchange minus local clock, RTT-corrected", registry=r
        )
        self.universe_size = Gauge("wt_universe_symbols", "Symbols in the recording universe", registry=r)
        self.focus_size = Gauge("wt_focus_symbols", "Symbols with full order books", registry=r)
        self.recorder_enqueued = Gauge("wt_recorder_enqueued", "Records queued since start", registry=r)
        self.recorder_dropped = Gauge(
            "wt_recorder_dropped", "Records dropped (queue full) since start", registry=r
        )
        self.recorder_written = Gauge("wt_recorder_written_lines", "Records written since start", registry=r)
        self.recorder_bytes = Gauge(
            "wt_recorder_written_bytes", "Compressed bytes written since start", registry=r
        )
        self.recorder_queue = Gauge("wt_recorder_queue_depth", "Records waiting to be written", registry=r)
        self.recorder_errors = Gauge("wt_recorder_write_errors", "Write errors since start", registry=r)
        self.disk_free = Gauge("wt_disk_free_bytes", "Free bytes on the recording volume", registry=r)

    def serve(self, host: str, port: int) -> None:
        start_http_server(port, addr=host, registry=self.registry)
