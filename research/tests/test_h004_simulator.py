"""Synthetic checks of the H-004 simulator: no look-ahead, honest delistings, conservative funding.

These run on generated prices only; they were written before the simulator saw any market data.
"""

import importlib.util
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("purgedcv")


def _load():  # type: ignore[no-untyped-def]
    path = Path(__file__).resolve().parents[1] / "h004_tsmom_pit.py"
    spec = importlib.util.spec_from_file_location("h004_tsmom_pit", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


h4 = _load()
H = h4.H


def _panel(n_days: int, n_sym: int, seed: int) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    drift = rng.normal(0.002, 0.004, size=n_sym)
    log_close = np.cumsum(drift + rng.normal(0, 0.03, size=(n_days, n_sym)), axis=0)
    close = 100 * np.exp(log_close)
    open_ = np.vstack([close[:1] / np.exp(drift), close[:-1]])  # open = previous close
    qvol = rng.uniform(1e6, 1e8, size=(n_days, n_sym))
    return {"open": open_, "close": close, "qvol": qvol}


def _run(p: dict[str, np.ndarray], variant: str = "b", has_funding: np.ndarray | None = None):  # type: ignore[no-untyped-def]
    days = np.arange(len(p["close"]))
    universe = h4.pit_universe(p["close"], p["qvol"], top_n=3)
    funding = np.zeros_like(p["close"])
    has = np.ones_like(p["close"], dtype=bool) if has_funding is None else has_funding
    return h4.simulate(days, p["open"], p["close"], funding, has, universe, variant)


def test_universe_needs_31_consecutive_days_and_ranks_by_past_volume() -> None:
    p = _panel(80, 5, seed=1)
    p["qvol"][:, 4] = 1e3  # always the least traded
    p["close"][50, 0] = np.nan  # a one-day gap
    u = h4.pit_universe(p["close"], p["qvol"], top_n=3)
    assert not u[:30].any()  # no symbol has 31 days of history yet
    assert u[30:].sum(axis=1).max() == 3
    assert not u[50:81, 0].any()  # ineligible from the gap until 31 fresh days have passed
    assert not u[:, 4].any()  # never in the top 3 while three others are eligible


def test_decisions_do_not_depend_on_future_data() -> None:
    p = _panel(200, 6, seed=2)
    base = _run(p)
    k = 120
    q = {name: a.copy() for name, a in p.items()}
    q["close"][k:] *= np.random.default_rng(9).uniform(0.5, 1.5, size=q["close"][k:].shape)
    q["open"][k:] *= 1.3
    q["open"][k:, 2] = np.nan  # symbol 2 is delisted from day k
    q["close"][k:, 2] = np.nan
    changed = _run(q)
    early = base["day"] <= k - 2  # holding day h only uses data up to day h + 1
    assert np.array_equal(base["day"], changed["day"])
    assert np.allclose(base["ret"][early], changed["ret"][early])
    assert not np.allclose(base["ret"][~early], changed["ret"][~early])


def test_delisting_while_held_realises_the_last_day_loss() -> None:
    n = 90
    p = _panel(n, 2, seed=3)
    trend = 100 * 1.01 ** np.arange(n) * np.exp(np.random.default_rng(4).normal(0, 0.01, n))
    p["close"][:, 0] = trend
    p["open"][:, 0] = np.concatenate([[trend[0] / 1.01], trend[:-1]])
    last = 80  # symbol 0 trades for the last time on day 80 and collapses 90 % intraday
    p["close"][last, 0] = p["open"][last, 0] * 0.1
    p["open"][last + 1 :, 0] = np.nan
    p["close"][last + 1 :, 0] = np.nan
    sim = _run(p)
    i = int(np.flatnonzero(sim["day"] == last)[0])
    assert sim["forced"][i] == 1
    # weight decided at the close of day 79 (a strong uptrend), so the loss is large and realised
    assert sim["ret"][i] < -0.5
    # the H-002 simulator, which peeks at the next open, would have skipped this day entirely
    days = np.arange(n)
    old = H.simulate(days, p["open"], p["close"], np.zeros_like(p["close"]), "b")
    j = int(np.flatnonzero(old["day"] == last)[0])
    assert old["ret"][j] > -0.1


def test_missing_funding_is_charged_to_long_positions() -> None:
    p = _panel(150, 4, seed=5)
    with_records = _run(p)
    no_records = _run(p, has_funding=np.zeros_like(p["close"], dtype=bool))
    long_gross = with_records["gross"]  # long-only: gross exposure = sum of long weights
    assert np.allclose(with_records["ret"] - no_records["ret"], h4.MISSING_FUNDING_PER_DAY * long_gross)
    assert no_records["fallback"].sum() == with_records["n"].sum()


def test_baseline_holds_the_whole_universe_equally() -> None:
    p = _panel(120, 5, seed=6)
    sim = _run(p, variant="baseline")
    assert np.allclose(sim["gross"][sim["n"] > 0], 1.0)
    assert sim["n"].max() == 3


def _write_kline_zip(path: Path, rows: list[tuple[str, float, float, float]]) -> None:
    import zipfile
    from datetime import UTC, datetime

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["open_time,open,high,low,close,volume,close_time,quote_volume,count,tbv,tbqv,ignore"]
    for iso, o, c, qv in rows:
        t = int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp() * 1000)
        lines.append(f"{t},{o},{o},{c},{c},1,{t + 86_399_999},{qv},1,0,0,0")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.with_suffix(".csv").name, "\n".join(lines))


def test_daily_files_fill_monthly_gaps_and_zero_volume_bars_are_not_tradable(tmp_path: Path) -> None:
    base = tmp_path / "data/futures/um"
    _write_kline_zip(  # the monthly file is truncated and ends with a phantom zero-volume bar
        base / "monthly/klines/XUSDT/1d/XUSDT-1d-2022-02.zip",
        [("2022-02-24", 10.0, 11.0, 5e6), ("2022-02-25", 11.0, 12.0, 4e6), ("2022-03-01", 12.0, 12.0, 0.0)],
    )
    _write_kline_zip(
        base / "daily/klines/XUSDT/1d/XUSDT-1d-2022-02-26.zip", [("2022-02-26", 12.0, 13.0, 3e6)]
    )
    bars = h4.daily_bars(tmp_path, "XUSDT")
    assert sorted(bars) == [H._day("2022-02-24"), H._day("2022-02-25"), H._day("2022-02-26")]
    assert bars[H._day("2022-02-26")] == (12.0, 13.0, 3e6)
