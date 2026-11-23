"""Synthetic checks of the H-005 simulator, written before it saw any market data.

Index 0 of a series is LOAD_START_H (2020-09-01 00:00 UTC), so index i is hour-of-day i % 24.
"""

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("purgedcv")


def _load():  # type: ignore[no-untyped-def]
    path = Path(__file__).resolve().parents[1] / "h005_deleveraging_reversal.py"
    spec = importlib.util.spec_from_file_location("h005_deleveraging_reversal", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


h5 = _load()


def _series(n: int = 900, seed: int = 0, funding: dict[int, float] | None = None):  # type: ignore[no-untyped-def]
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    open_ = np.concatenate([[100.0], close[:-1]])
    high, low = np.maximum(open_, close) * 1.001, np.minimum(open_, close) * 0.999
    oi = np.full(n, 1e6)
    minutes = np.array(sorted(funding or {}), dtype=np.int64)
    rates = np.array([(funding or {})[int(m)] for m in minutes])
    return h5.Series(
        "XUSDT", open_, high, low, close, oi.copy(), oi.copy(), np.ones(n, dtype=bool), minutes, rates
    )


def _minute(index: int, minute_of_hour: int = 0) -> int:
    return (h5.LOAD_START_H + index) * 60 + minute_of_hour


def test_sigma_uses_only_past_returns() -> None:
    s = _series()
    s.close[750] = s.close[749] * 1.2  # a huge move at hour 750
    r, sigma = h5.past_sigma(s.close)
    expected = np.std(r[750 - h5.SIGMA_WINDOW : 750], ddof=1)
    assert sigma[750] == pytest.approx(expected)
    assert sigma[750] < 0.01  # the jump itself is not in its own sigma
    assert math.isnan(sigma[500])  # fewer than 600 past returns


def test_event_needs_oi_drop_before_the_hour_ends_and_a_3_sigma_move() -> None:
    s = _series()
    k = 700
    s.close[k] = s.close[k - 1] * 0.9  # crash
    assert h5.detect_events(s) == []  # open interest did not fall
    s.oi_end[k] = s.oi_start[k] * 0.96
    assert h5.detect_events(s) == [(k, +1)]  # long after a crash
    s.in_universe[k] = False
    assert h5.detect_events(s) == []
    oi = {int(slot): 1e6 for slot in range((h5.LOAD_START_H) * 12, (h5.LOAD_START_H + 3) * 12)}
    oi[(h5.LOAD_START_H + 1) * 12 + 12] = 5e5  # a drop at the next hour's first snapshot: not used
    built = h5.build_series(
        "X", {}, oi, np.ones(h5.END_H - h5.LOAD_START_H, dtype=bool), (np.zeros(0), np.zeros(0))
    )
    assert built.oi_end[1] == 1e6
    assert built.oi_start[2] == 5e5


def test_long_time_exit_with_costs_and_funding() -> None:
    k = 624  # hour-of-day 0: entry 01:00, exit 13:00, so 08:00 is the only scheduled funding time
    s = _series(funding={_minute(632): 0.0001})
    t = h5.make_trade(s, k, +1)
    assert t is not None
    assert t.reason == "time"
    assert t.entry_h == h5.LOAD_START_H + k + 1
    assert t.exit_h == t.entry_h + 12
    gross = s.open[k + 13] / s.open[k + 1] - 1
    assert t.gross == pytest.approx(gross)
    assert t.funding == pytest.approx(0.0001)
    assert t.net == pytest.approx(gross - 2 * h5.COST_SIDE - 0.0001)
    assert sum(t.daily.values()) == pytest.approx(t.net)


def test_short_receives_positive_funding_and_missing_records_are_charged() -> None:
    k = 624
    with_record = h5.make_trade(_series(funding={_minute(632): 0.0002}), k, -1)
    without = h5.make_trade(_series(), k, -1)
    assert with_record is not None
    assert without is not None
    assert with_record.funding == pytest.approx(-0.0002)
    assert without.funding == pytest.approx(h5.MISSING_FUNDING)


def test_stop_fills_at_the_open_after_a_gap() -> None:
    s = _series()
    k = 700
    entry = s.open[k + 1]
    s.open[k + 4] = entry * 0.85  # gaps through the -10 % stop
    s.low[k + 4] = entry * 0.84
    t = h5.make_trade(s, k, +1)
    assert t is not None
    assert t.reason == "stop"
    assert t.gross == pytest.approx(0.85 * (1 - h5.STOP_SLIPPAGE) - 1)
    assert t.exit_h == h5.LOAD_START_H + k + 5


def test_missing_exit_bar_uses_the_next_bar_and_a_dead_symbol_is_force_closed() -> None:
    s = _series()
    k = 700
    s.open[k + 13] = np.nan
    t = h5.make_trade(s, k, +1)
    assert t is not None
    assert t.exit_h == h5.LOAD_START_H + k + 14
    assert t.gross == pytest.approx(s.open[k + 14] / s.open[k + 1] - 1)
    dead = _series()
    dead.open[k + 8 :] = np.nan
    dead.close[k + 8 :] = np.nan
    t = h5.make_trade(dead, k, +1)
    assert t is not None
    assert t.reason == "forced"
    assert t.gross == pytest.approx(dead.close[k + 7] * (1 - h5.FORCED_EXIT_SLIPPAGE) / dead.open[k + 1] - 1)


def test_portfolio_capacity_one_position_per_symbol_and_exits_first() -> None:
    def trade(symbol: str, entry: int, exit_: int):  # type: ignore[no-untyped-def]
        return h5.Trade(symbol, 1, entry, exit_, "time", 0.0, 0.0, 0.0, 0.0, {})

    batch = [trade(f"S{i:02d}", 100, 112) for i in range(12)]
    accepted = h5.run_portfolio([*batch, trade("S00", 105, 117)])
    assert [t.symbol for t in accepted] == [f"S{i:02d}" for i in range(10)]  # capacity, alphabetical
    later = h5.run_portfolio([*batch[:10], trade("S10", 112, 124)])
    assert "S10" in [t.symbol for t in later]  # exits at hour 112 free the slots first


def test_daily_attribution_across_midnight_sums_to_net() -> None:
    k = 640  # entry 17:00, exit 05:00 the next day
    s = _series()
    t = h5.make_trade(s, k, +1)
    assert t is not None
    assert len(t.daily) == 2
    assert sum(t.daily.values()) == pytest.approx(t.net)
    daily = h5.daily_returns([t], min(t.daily), max(t.daily) + 1)
    assert daily.sum() == pytest.approx(h5.WEIGHT * t.net)
