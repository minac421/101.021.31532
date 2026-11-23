import pytest

from wt_data.binance.um.streams import (
    DEFAULT_ROUTES,
    MAX_STREAMS_PER_CONNECTION,
    Route,
    StreamGroup,
    build_groups,
    plan_connections,
    shard_group,
    stream_kind,
    stream_symbol,
)


@pytest.mark.parametrize(
    ("stream", "kind"),
    [
        ("btcusdt@depth@100ms", "depth"),
        ("btcusdt@depth", "depth"),
        ("btcusdt@depth20@100ms", "depth"),
        ("btcusdt@aggTrade", "aggTrade"),
        ("btcusdt@kline_1m", "kline"),
        ("btcusdt_perp@continuousKline_1m", "continuousKline"),
        ("!markPrice@arr@1s", "markPrice"),
        ("!forceOrder@arr", "forceOrder"),
        ("!contractInfo", "contractInfo"),
    ],
)
def test_stream_kind(stream: str, kind: str) -> None:
    assert stream_kind(stream) == kind


def test_stream_symbol() -> None:
    assert stream_symbol("btcusdt@aggTrade") == "BTCUSDT"
    assert stream_symbol("!markPrice@arr@1s") is None
    with pytest.raises(ValueError, match="not a Binance stream"):
        stream_symbol("garbage")


def test_verified_routes() -> None:
    assert DEFAULT_ROUTES["depth"] is Route.PUBLIC
    assert DEFAULT_ROUTES["bookTicker"] is Route.PUBLIC
    assert DEFAULT_ROUTES["aggTrade"] is Route.MARKET
    assert DEFAULT_ROUTES["markPrice"] is Route.MARKET


def _groups(n: int) -> list[StreamGroup]:
    symbols = [f"S{i:03d}USDT" for i in range(n)]
    return build_groups(
        symbols=symbols,
        focus_symbols=symbols[:3],
        per_symbol=["aggTrade"],
        focus_streams=["depth@100ms"],
        market_wide=["!markPrice@arr@1s", "!forceOrder@arr"],
        routes=DEFAULT_ROUTES,
    )


def test_groups_are_routed_and_expanded() -> None:
    groups = {g.label: g for g in _groups(5)}
    assert groups["aggTrade"].route is Route.MARKET
    assert groups["aggTrade"].streams[0] == "s000usdt@aggTrade"
    assert groups["focus-depth@100ms"].route is Route.PUBLIC
    assert len(groups["focus-depth@100ms"].streams) == 3
    assert groups["market-wide"].streams == ("!markPrice@arr@1s", "!forceOrder@arr")


def test_unknown_kind_has_no_route() -> None:
    with pytest.raises(ValueError, match="no WebSocket route"):
        build_groups(
            symbols=["BTCUSDT"],
            focus_symbols=[],
            per_symbol=["mysteryStream"],
            focus_streams=[],
            market_wide=[],
            routes=DEFAULT_ROUTES,
        )


def test_plan_respects_limits_and_builds_routed_urls() -> None:
    plan = plan_connections(_groups(500), max_streams_per_connection=150)
    assert all(len(spec.streams) <= 150 for spec in plan)
    assert sum(len(spec.streams) for spec in plan) == 500 + 3 + 2
    agg = next(spec for spec in plan if spec.name.startswith("market-aggTrade"))
    url = agg.url("wss://fstream.binance.com")
    assert url.startswith("wss://fstream.binance.com/market/stream?streams=")
    depth = next(spec for spec in plan if "depth" in spec.name)
    assert depth.url("wss://fstream.binance.com/").startswith(
        "wss://fstream.binance.com/public/stream?streams="
    )


def test_sharding_is_stable_when_one_symbol_is_added() -> None:
    def shards(n: int) -> dict[str, frozenset[str]]:
        group = next(g for g in _groups(n) if g.label == "aggTrade")
        return {s.name: frozenset(s.streams) for s in shard_group(group, max_streams_per_connection=150)}

    before, after = shards(300), shards(301)
    assert before.keys() == after.keys()
    changed = [name for name in before if before[name] != after[name]]
    assert len(changed) == 1


def test_url_length_limit_increases_shard_count() -> None:
    group = next(g for g in _groups(200) if g.label == "aggTrade")
    specs = shard_group(group, max_streams_per_connection=150, max_url_length=1500)
    assert all(len(s.url("wss://fstream.binance.com")) <= 1500 for s in specs)


def test_hard_limit_enforced() -> None:
    group = next(g for g in _groups(10) if g.label == "aggTrade")
    with pytest.raises(ValueError, match="max_streams_per_connection"):
        shard_group(group, max_streams_per_connection=MAX_STREAMS_PER_CONNECTION + 1)
