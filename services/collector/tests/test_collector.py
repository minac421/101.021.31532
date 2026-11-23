"""Collector: selection logic, configuration, and end-to-end runs against the fake exchange."""

import asyncio
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import msgspec
import pytest

from wt_collector.app import (
    Collector,
    is_diff_depth_stream,
    rank_by_quote_volume,
    select_focus,
    select_universe,
)
from wt_collector.config import (
    CollectorConfig,
    ExchangeConfig,
    FocusConfig,
    MetricsConfig,
    PollersConfig,
    QualityConfig,
    RecorderConfig,
    StreamsConfig,
    UniverseConfig,
    WsConfig,
    load_config,
)
from wt_data.binance.um.messages import ExchangeInfo, SymbolInfo, Ticker24h
from wt_data.binance.um.rest import BinanceUmRest
from wt_data.recorder import read_records

REPO = Path(__file__).resolve().parents[3]


# ------------------------------------------------------------------ selection
def _info() -> ExchangeInfo:
    def sym(name: str, **kw: str) -> SymbolInfo:
        base = {
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
            "underlyingType": "COIN",
        }
        return SymbolInfo(symbol=name, **(base | kw))

    return ExchangeInfo(
        symbols=[
            sym("BTCUSDT"),
            sym("ETHUSDT"),
            sym("NEWUSDT", status="PENDING_TRADING"),
            sym("BTCUSDC", quoteAsset="USDC"),
            sym("XAUUSDT", underlyingType="COMMODITY"),
            sym("ETHUSDT_261225", contractType="CURRENT_QUARTER"),
            sym("DOGEUSDT"),
        ]
    )


def test_universe_selection_filters_and_overrides() -> None:
    assert select_universe(_info(), UniverseConfig()) == ["BTCUSDT", "DOGEUSDT", "ETHUSDT"]
    cfg = UniverseConfig(include=["XAUUSDT"], exclude=["DOGEUSDT"])
    assert select_universe(_info(), cfg) == ["BTCUSDT", "ETHUSDT", "XAUUSDT"]


def test_focus_is_static_plus_most_liquid() -> None:
    universe = ["AUSDT", "BTCUSDT", "CUSDT", "ETHUSDT"]
    ranked = rank_by_quote_volume(
        universe, [Ticker24h("CUSDT", "900"), Ticker24h("AUSDT", "10"), Ticker24h("BTCUSDT", "5")]
    )
    assert ranked[0] == "CUSDT"
    assert select_focus(
        universe, ranked, FocusConfig(static=["BTCUSDT", "MISSING"], top_by_quote_volume=1)
    ) == [
        "BTCUSDT",
        "CUSDT",
    ]


def test_only_diff_depth_streams_feed_the_book() -> None:
    assert is_diff_depth_stream("btcusdt@depth@100ms")
    assert is_diff_depth_stream("btcusdt@depth")
    assert not is_diff_depth_stream("btcusdt@depth20@100ms")


# ------------------------------------------------------------------ configuration
def test_example_config_is_valid_and_matches_defaults() -> None:
    cfg = load_config(REPO / "config" / "collector.example.yaml")
    assert cfg == CollectorConfig()


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("exchange: {max_streams_per_connection: 201}", "<= 200"),
        ("streams: {per_symbol: [mysteryStream]}", "no route"),
        ("streams: {market_wide: [btcusdt@aggTrade]}", "must start with '!'"),
        ("pollers: {futures_stats_period: 7m}", "futures_stats_period"),
        ("pollers: {futures_stats_endpoints: [basis]}", "unknown futures_stats_endpoints"),
        ("recorder: {rooot: /tmp}", "unknown field"),
    ],
)
def test_invalid_configs_are_rejected(tmp_path: Path, yaml_text: str, message: str) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(yaml_text)
    with pytest.raises((msgspec.ValidationError, ValueError), match=message):
        load_config(path)


# ------------------------------------------------------------------ end-to-end
def _config(tmp_path: Path, port: int, **ws: Any) -> CollectorConfig:
    return CollectorConfig(
        exchange=ExchangeConfig(rest_base_url="https://fapi.test", ws_base_url=f"ws://127.0.0.1:{port}"),
        focus=FocusConfig(static=["BTCUSDT", "ETHUSDT"], top_by_quote_volume=0),
        streams=StreamsConfig(
            per_symbol=["aggTrade"], focus=["depth@100ms", "bookTicker"], market_wide=["!markPrice@arr@1s"]
        ),
        ws=WsConfig(**({"silence_timeout_s": 5.0, "open_timeout_s": 5.0, "use_env_proxy": False} | ws)),
        pollers=PollersConfig(
            open_interest_s=1.0,
            futures_stats_focus_s=1.0,
            futures_stats_other_s=2.0,
            server_time_s=1.0,
            depth_anchor_s=2.0,
            futures_stats_endpoints=["openInterestHist", "takerlongshortRatio"],
        ),
        recorder=RecorderConfig(root=str(tmp_path / "raw"), flush_interval_s=0.2),
        quality=QualityConfig(summary_interval_s=1.0),
        metrics=MetricsConfig(enabled=False),
    )


async def _run(collector: Collector, seconds: float, during: Any = None) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(stop))
    await asyncio.wait_for(collector.ready.wait(), 10)
    if during is not None:
        await during()
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(task, 30)


def _records(root: Path, kind: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(root.rglob(f"{kind}/**/*.jsonl.zst")):
        out.extend(read_records(path))
    return out


@pytest.mark.integration
async def test_records_everything_and_rotates_without_gaps(tmp_path: Path, fake_exchange: Any) -> None:
    cfg = _config(tmp_path, fake_exchange.port, max_age_s=1.2, rotation_overlap_s=0.3)
    rest = BinanceUmRest(cfg.exchange.rest_base_url, transport=httpx.MockTransport(fake_exchange.rest))
    collector = Collector(cfg, rest=rest, instance_id="it")
    await _run(collector, 4.0)

    root = tmp_path / "raw" / "binance-um"
    kinds = {p.name for p in root.iterdir()}
    assert {
        "ws.aggTrade",
        "ws.depth",
        "ws.bookTicker",
        "ws.markPrice",
        "rest.depth",
        "rest.exchangeInfo",
        "rest.ticker24hr",
        "rest.openInterest",
        "rest.time",
        "control",
        "quality.summary",
        "rest.futures_data.openInterestHist",
        "rest.futures_data.takerlongshortRatio",
    } <= kinds

    # routing: every stream was requested on the path the exchange serves it on
    assert fake_exchange.rejected_streams == []
    # universe excludes the commodity perpetual and the quarterly contract
    assert collector.universe == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert collector.focus == ["BTCUSDT", "ETHUSDT"]

    # rotations happened, and they lost nothing
    events = Counter(r["payload"]["event"] for r in _records(root, "control"))
    assert events["ws_rotated"] >= 3
    for symbol in ("BTCUSDT", "ETHUSDT"):
        book = collector.books[symbol]
        assert book.is_valid, book.stats
        assert book.stats.gaps == 0
        assert book.stats.duplicates > 0  # overlap really delivered duplicates
        assert book.last_update_id <= fake_exchange.market.books[symbol].last_u
    summary = _records(root, "quality.summary")[-1]["payload"]
    assert summary["agg_trade_continuity"] == {} or all(
        v["confirmed_missing"] == 0 for v in summary["agg_trade_continuity"].values()
    )

    # recorded depth diffs + recorded snapshot are enough to rebuild the book offline
    from wt_data.binance.um.messages import depth_snapshot_decoder, depth_update_decoder
    from wt_data.book import OrderBook

    offline = OrderBook("BTCUSDT")
    snapshots = [r for r in _records(root, "rest.depth") if "symbol=BTCUSDT" in r["origin"]]
    diffs = [r for r in _records(root, "ws.depth") if r["payload"]["stream"] == "btcusdt@depth@100ms"]
    first_snapshot_ns = snapshots[0]["recv_ns"]
    for r in diffs:
        if r["recv_ns"] <= first_snapshot_ns:
            offline.on_diff(depth_update_decoder.decode(msgspec.json.encode(r["payload"]["data"])), 0)
    offline.apply_snapshot(depth_snapshot_decoder.decode(msgspec.json.encode(snapshots[0]["payload"])))
    for r in diffs:
        if r["recv_ns"] > first_snapshot_ns:
            offline.on_diff(depth_update_decoder.decode(msgspec.json.encode(r["payload"]["data"])), 0)
    live = collector.books["BTCUSDT"]
    assert offline.is_valid
    assert offline.last_update_id == live.last_update_id
    assert offline.top_levels(50) == live.top_levels(50)


@pytest.mark.integration
async def test_recovers_from_disconnects_and_reports_the_gap(tmp_path: Path, fake_exchange: Any) -> None:
    cfg = _config(tmp_path, fake_exchange.port, backoff_initial_s=0.3, backoff_max_s=0.5)
    rest = BinanceUmRest(cfg.exchange.rest_base_url, transport=httpx.MockTransport(fake_exchange.rest))
    collector = Collector(cfg, rest=rest, instance_id="it2")

    async def disconnect() -> None:
        await asyncio.sleep(1.0)
        await fake_exchange.drop_all_connections()  # the market keeps moving while we are away

    await _run(collector, 4.0, during=disconnect)
    root = tmp_path / "raw" / "binance-um"
    events = Counter(r["payload"]["event"] for r in _records(root, "control"))
    assert events["ws_disconnected"] >= 1
    for symbol in ("BTCUSDT", "ETHUSDT"):
        book = collector.books[symbol]
        assert book.is_valid, book.stats  # resynced after the gap
        assert book.stats.gaps >= 1
        assert book.stats.resyncs >= 1
    summaries = [r["payload"] for r in _records(root, "quality.summary")]
    missing = sum(v["confirmed_missing"] for s in summaries[-1:] for v in s["agg_trade_continuity"].values())
    assert missing > 0, "missed trades must be reported, not hidden"
