"""Collector configuration: YAML file -> strictly validated, immutable structs.

Unknown keys are rejected (a typo must not silently fall back to a default).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import msgspec
from msgspec import Meta

from wt_data.binance.um.streams import DEFAULT_ROUTES, MAX_STREAMS_PER_CONNECTION, Route, stream_kind

PositiveFloat = Annotated[float, Meta(gt=0)]
PositiveInt = Annotated[int, Meta(gt=0)]
Fraction = Annotated[float, Meta(gt=0, le=1)]

FUTURES_DATA_PERIODS = ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d")
FUTURES_DATA_ENDPOINTS = (
    "openInterestHist",
    "topLongShortAccountRatio",
    "topLongShortPositionRatio",
    "globalLongShortAccountRatio",
    "takerlongshortRatio",
)
DEPTH_SNAPSHOT_LIMITS = (5, 10, 20, 50, 100, 500, 1000)


class _Section(msgspec.Struct, forbid_unknown_fields=True, kw_only=True, frozen=True):
    pass


class ExchangeConfig(_Section, frozen=True):
    rest_base_url: str = "https://fapi.binance.com"
    ws_base_url: str = "wss://fstream.binance.com"
    routes: dict[str, Route] = msgspec.field(default_factory=dict)  # overrides of DEFAULT_ROUTES
    max_streams_per_connection: Annotated[int, Meta(ge=1, le=MAX_STREAMS_PER_CONNECTION)] = 150
    max_url_length: Annotated[int, Meta(ge=200)] = 7000

    @property
    def effective_routes(self) -> Mapping[str, Route]:
        return {**DEFAULT_ROUTES, **self.routes}


class UniverseConfig(_Section, frozen=True):
    quote_asset: str = "USDT"
    contract_types: list[str] = msgspec.field(default_factory=lambda: ["PERPETUAL"])
    status: str = "TRADING"
    underlying_types: list[str] = msgspec.field(default_factory=lambda: ["COIN"])
    include: list[str] = msgspec.field(default_factory=list)
    exclude: list[str] = msgspec.field(default_factory=list)
    max_symbols: PositiveInt | None = None  # keep the most liquid N (by 24h quote volume)
    refresh_s: PositiveFloat = 600.0


class FocusConfig(_Section, frozen=True):
    """Symbols that get full order books (diff stream + snapshots)."""

    static: list[str] = msgspec.field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    top_by_quote_volume: Annotated[int, Meta(ge=0, le=200)] = 28
    refresh_s: PositiveFloat = 86_400.0


class StreamsConfig(_Section, frozen=True):
    per_symbol: list[str] = msgspec.field(default_factory=lambda: ["aggTrade"])
    focus: list[str] = msgspec.field(default_factory=lambda: ["depth@100ms", "bookTicker"])
    market_wide: list[str] = msgspec.field(
        default_factory=lambda: ["!markPrice@arr@1s", "!forceOrder@arr", "!miniTicker@arr", "!contractInfo"]
    )


class WsConfig(_Section, frozen=True):
    max_age_s: PositiveFloat = 82_800.0  # rotate well before the exchange's 24h cut
    rotation_overlap_s: Annotated[float, Meta(ge=0)] = 5.0
    silence_timeout_s: PositiveFloat = 60.0
    open_timeout_s: PositiveFloat = 15.0
    backoff_initial_s: PositiveFloat = 0.5
    backoff_max_s: PositiveFloat = 30.0
    use_env_proxy: bool = True


class RestConfig(_Section, frozen=True):
    timeout_s: PositiveFloat = 10.0
    weight_limit_per_minute: PositiveInt | None = None  # None => read REQUEST_WEIGHT from exchangeInfo
    max_weight_fraction: Fraction = 0.5  # the rest of the IP budget is left for other processes
    futures_data_requests_per_5min: PositiveInt = 600
    ca_bundle: str | None = None


class PollersConfig(_Section, frozen=True):
    exchange_info_s: PositiveFloat = 600.0
    server_time_s: PositiveFloat = 60.0
    open_interest_s: PositiveFloat = 300.0
    futures_stats_period: str = "5m"
    futures_stats_focus_s: PositiveFloat = 300.0
    futures_stats_other_s: PositiveFloat = 3600.0
    futures_stats_endpoints: list[str] = msgspec.field(default_factory=lambda: list(FUTURES_DATA_ENDPOINTS))
    depth_snapshot_limit: int = 1000
    depth_anchor_s: PositiveFloat = 1800.0  # periodic snapshot per focus symbol (offline rebuild anchors)
    funding_info_s: PositiveFloat = 86_400.0


class RecorderConfig(_Section, frozen=True):
    root: str = "./data/raw"
    source: str = "binance-um"
    zstd_level: Annotated[int, Meta(ge=1, le=19)] = 3
    flush_interval_s: PositiveFloat = 2.0
    queue_max: PositiveInt = 200_000


class QualityConfig(_Section, frozen=True):
    summary_interval_s: PositiveFloat = 60.0
    reorder_window_s: PositiveFloat = 2.0
    book_reorder_window_s: PositiveFloat = 1.5
    silent_kind_warning_s: PositiveFloat = 300.0  # planned stream kind with no data -> routing/plan problem


class MetricsConfig(_Section, frozen=True):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: Annotated[int, Meta(ge=0, le=65535)] = 9108


class CollectorConfig(_Section, frozen=True):
    exchange: ExchangeConfig = msgspec.field(default_factory=ExchangeConfig)
    universe: UniverseConfig = msgspec.field(default_factory=UniverseConfig)
    focus: FocusConfig = msgspec.field(default_factory=FocusConfig)
    streams: StreamsConfig = msgspec.field(default_factory=StreamsConfig)
    ws: WsConfig = msgspec.field(default_factory=WsConfig)
    rest: RestConfig = msgspec.field(default_factory=RestConfig)
    pollers: PollersConfig = msgspec.field(default_factory=PollersConfig)
    recorder: RecorderConfig = msgspec.field(default_factory=RecorderConfig)
    quality: QualityConfig = msgspec.field(default_factory=QualityConfig)
    metrics: MetricsConfig = msgspec.field(default_factory=MetricsConfig)

    def __post_init__(self) -> None:
        routes = self.exchange.effective_routes
        templates = [f"x@{s}" for s in (*self.streams.per_symbol, *self.streams.focus)]
        for stream in (*templates, *self.streams.market_wide):
            kind = stream_kind(stream)
            if kind not in routes:
                raise ValueError(f"stream {stream!r}: no route configured for kind {kind!r}")
        for stream in self.streams.market_wide:
            if not stream.startswith("!"):
                raise ValueError(f"market_wide streams must start with '!': {stream!r}")
        if self.pollers.futures_stats_period not in FUTURES_DATA_PERIODS:
            raise ValueError(f"futures_stats_period must be one of {FUTURES_DATA_PERIODS}")
        unknown = set(self.pollers.futures_stats_endpoints) - set(FUTURES_DATA_ENDPOINTS)
        if unknown:
            raise ValueError(f"unknown futures_stats_endpoints: {sorted(unknown)}")
        if self.pollers.depth_snapshot_limit not in DEPTH_SNAPSHOT_LIMITS:
            raise ValueError(f"depth_snapshot_limit must be one of {DEPTH_SNAPSHOT_LIMITS}")
        if self.ws.backoff_initial_s > self.ws.backoff_max_s:
            raise ValueError("ws.backoff_initial_s must not exceed ws.backoff_max_s")


def load_config(path: Path) -> CollectorConfig:
    return msgspec.yaml.decode(path.read_bytes(), type=CollectorConfig)
