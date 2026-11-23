"""H-005: 12-hour reversal after forced deleveraging (open interest -3 % in an hour with a >= 3 sigma move).

Pre-registered in docs/research/H-005-deleveraging-reversal.md (committed before the hourly and
open-interest data were downloaded). Universe: the H-004 point-in-time top 20, known at the
previous day's close. All thresholds, costs and sizes below are the registered values.

* sigma(h) uses the 720 hourly log returns *before* hour h (at least 600 present).
* dOI(h) = OI(h + 55 min) / OI(h) - 1: the last 5-minute snapshot before the hour closes.
* Event: dOI <= -3 % and |z| >= 3 -> trade against the move: long after a crash, short after a spike.
* Entry at the next hour's open, exit 12 hours later; -10 % catastrophe stop on hourly highs/lows
  (gaps fill at the open) with 0.10 % extra slippage; 0.10 % cost per side; actual funding.
* 10 % of capital per position, at most 10 positions, one per symbol.

Usage:
    uv run --group research python research/h005_deleveraging_reversal.py --archive data/archive
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import math
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import numpy as np
import purgedcv


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


H4 = _load("h004_tsmom_pit")
H = H4.H

HOUR_MS = 3_600_000
N_TRIALS = 6  # H-001, H-002a, H-002b, H-003, H-004, H-005
OI_DROP, Z_MIN, HOLD_H = -0.03, 3.0, 12
STOP, STOP_SLIPPAGE, COST_SIDE = 0.10, 0.0010, 0.0010
WEIGHT, MAX_POSITIONS = 0.10, 10
SIGMA_WINDOW, SIGMA_MIN_OBS = 720, 600
MISSING_FUNDING = 0.0001  # per scheduled 8-hour funding time with no record, either direction
FORCED_EXIT_SLIPPAGE = 0.01  # the symbol stops trading while held (as in H-004)
EXIT_SEARCH_H = 24  # a missing exit bar is replaced by the next bar within this many hours
LOAD_START_H = H._day("2020-09-01") * 24
START_H = H._day("2020-10-01") * 24
END_H = H4.PIT_END_DAY * 24  # exclusive: 2025-07-01 00:00 UTC
PERIODS = {
    "2020Q4+2021": (H._day("2020-10-01"), H._day("2022-01-01")),
    "2022": (H._day("2022-01-01"), H._day("2023-01-01")),
    "2023": (H._day("2023-01-01"), H._day("2024-01-01")),
    "2024": (H._day("2024-01-01"), H._day("2025-01-01")),
    "2025H1": (H._day("2025-01-01"), H4.PIT_END_DAY),
}


# ---------------------------------------------------------------------------------------------- data


def _rows(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as z:
        text = z.read(z.namelist()[0]).decode()
    return [rec for rec in csv.reader(io.StringIO(text)) if rec and rec[0][:1].isdigit()]


def load_hourly(root: Path, symbol: str) -> dict[int, tuple[float, float, float, float]]:
    """hour -> (open, high, low, close); monthly files first, daily files fill gaps; zero-volume
    bars are dropped (nothing traded, so the hour is not tradable)."""
    out: dict[int, tuple[float, float, float, float]] = {}
    for frequency in ("monthly", "daily"):
        for path in sorted((root / f"data/futures/um/{frequency}/klines/{symbol}/1h").glob("*.zip")):
            for rec in _rows(path):
                hour = H4._ms(rec[0]) // HOUR_MS
                if LOAD_START_H <= hour < END_H and hour not in out and float(rec[7]) > 0:
                    out[hour] = (float(rec[1]), float(rec[2]), float(rec[3]), float(rec[4]))
    return out


def load_open_interest(root: Path, symbol: str) -> dict[int, float]:
    """5-minute slot (minutes since epoch // 5) -> open interest in contracts."""
    out: dict[int, float] = {}
    for path in sorted((root / f"data/futures/um/daily/metrics/{symbol}").glob("*.zip")):
        for rec in _rows(path):
            try:
                t = datetime.fromisoformat(rec[0]).replace(tzinfo=UTC)
                value = float(rec[2])
            except ValueError:
                continue
            if value > 0:
                out[int(t.timestamp()) // 300] = value
    return out


def load_funding(root: Path, symbol: str) -> tuple[np.ndarray, np.ndarray]:
    """(funding times in minutes since epoch, sorted; rates)."""
    out: dict[int, float] = {}
    for path in sorted((root / f"data/futures/um/monthly/fundingRate/{symbol}").glob("*.zip")):
        for rec in _rows(path):
            minute = round(H4._ms(rec[0]) / 60_000)
            out[minute] = out.get(minute, 0.0) + float(rec[2])
    minutes = np.array(sorted(out), dtype=np.int64)
    return minutes, np.array([out[int(m)] for m in minutes])


@dataclass
class Series:
    """One symbol on the hourly grid LOAD_START_H .. END_H (index 0 = LOAD_START_H)."""

    symbol: str
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    oi_start: np.ndarray
    oi_end: np.ndarray
    in_universe: np.ndarray
    f_minutes: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    f_rates: np.ndarray = field(default_factory=lambda: np.zeros(0))


def build_series(
    symbol: str,
    bars: dict[int, tuple[float, float, float, float]],
    oi: dict[int, float],
    in_universe: np.ndarray,
    funding: tuple[np.ndarray, np.ndarray],
) -> Series:
    n = END_H - LOAD_START_H
    arr = np.full((4, n), np.nan)
    for hour, values in bars.items():
        arr[:, hour - LOAD_START_H] = values
    slots = (np.arange(LOAD_START_H, END_H) * 12).astype(np.int64)  # 5-minute slot of each hour start
    oi_start = np.array([oi.get(int(s), np.nan) for s in slots])
    oi_end = np.array([oi.get(int(s) + 11, np.nan) for s in slots])  # +55 minutes
    return Series(symbol, arr[0], arr[1], arr[2], arr[3], oi_start, oi_end, in_universe, *funding)


# -------------------------------------------------------------------------------------------- signal


def past_sigma(close: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(log return of each hour, std of the SIGMA_WINDOW returns strictly before it)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.log(close[1:] / close[:-1])
    r = np.concatenate([[np.nan], r])
    ok = np.isfinite(r)
    z = np.where(ok, r, 0.0)
    c1 = np.concatenate([[0.0], np.cumsum(z)])
    c2 = np.concatenate([[0.0], np.cumsum(z * z)])
    cn = np.concatenate([[0], np.cumsum(ok)])
    k = np.arange(len(r))
    lo = np.maximum(k - SIGMA_WINDOW, 0)
    n = cn[k] - cn[lo]  # returns at indices lo .. k-1
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = (c1[k] - c1[lo]) / n
        var = ((c2[k] - c2[lo]) - n * mean * mean) / (n - 1)
    sigma = np.where((n >= SIGMA_MIN_OBS) & (var > 0), np.sqrt(np.maximum(var, 0.0)), np.nan)
    return r, sigma


def detect_events(s: Series, oi_drop: float = OI_DROP, z_min: float = Z_MIN) -> list[tuple[int, int]]:
    """(hour index, direction) with direction +1 = long after a crash, -1 = short after a spike."""
    r, sigma = past_sigma(s.close)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = r / sigma
        d_oi = s.oi_end / s.oi_start - 1
    hit = s.in_universe & np.isfinite(z) & np.isfinite(d_oi) & (d_oi <= oi_drop) & (np.abs(z) >= z_min)
    return [(int(k), -int(np.sign(z[k]))) for k in np.flatnonzero(hit)]


# --------------------------------------------------------------------------------------------- trade


@dataclass
class Trade:
    symbol: str
    direction: int
    entry_h: int  # absolute hour of entry (open of that hour)
    exit_h: int  # absolute hour at which the position is closed (exclusive end of holding)
    reason: str  # time | stop | forced
    gross: float
    net: float
    funding: float
    control: float  # same-symbol mean 12h return in the trade's direction over the prior 30 days
    daily: dict[int, float]  # day -> P&L as a fraction of entry notional (costs and funding included)


def _funding_cost(s: Series, direction: int, start_ms: int, end_ms: int) -> tuple[float, dict[int, float]]:
    """Funding paid in (start, end]: actual records, plus MISSING_FUNDING per scheduled 00/08/16 UTC
    time with no record within one minute (charged in either direction)."""
    by_day: dict[int, float] = {}
    total = 0.0
    lo, hi = start_ms // 60_000, end_ms // 60_000
    i0, i1 = np.searchsorted(s.f_minutes, [lo, hi], side="right")
    for minute, rate in zip(s.f_minutes[i0:i1].tolist(), s.f_rates[i0:i1].tolist(), strict=True):
        total += direction * rate
        by_day[minute // 1440] = by_day.get(minute // 1440, 0.0) + direction * rate
    for slot in range(lo // 480 + 1, hi // 480 + 1):
        minute = slot * 480
        j = int(np.searchsorted(s.f_minutes, minute - 1))
        if not (j < len(s.f_minutes) and s.f_minutes[j] <= minute + 1):
            total += MISSING_FUNDING
            by_day[minute // 1440] = by_day.get(minute // 1440, 0.0) + MISSING_FUNDING
    return total, by_day


def make_trade(s: Series, k: int, direction: int, hold: int = HOLD_H) -> Trade | None:
    e = k + 1
    n = len(s.open)
    if e >= n or not np.isfinite(s.open[e]):
        return None
    entry = s.open[e]
    stop = entry * (1 - STOP) if direction > 0 else entry * (1 + STOP)
    exit_idx, exit_price, reason = None, math.nan, "time"
    last_close = entry
    for m in range(e, min(e + hold, n)):
        if not np.isfinite(s.open[m]):
            continue
        if direction > 0 and s.low[m] <= stop:
            exit_idx, exit_price, reason = m + 1, min(s.open[m], stop) * (1 - STOP_SLIPPAGE), "stop"
            break
        if direction < 0 and s.high[m] >= stop:
            exit_idx, exit_price, reason = m + 1, max(s.open[m], stop) * (1 + STOP_SLIPPAGE), "stop"
            break
        last_close = s.close[m]
    if exit_idx is None:
        target = e + hold
        if target + EXIT_SEARCH_H >= n:
            return None  # the exit would fall outside the registered data range
        later = [m for m in range(target, target + EXIT_SEARCH_H + 1) if np.isfinite(s.open[m])]
        if later:
            exit_idx, exit_price = later[0], s.open[later[0]]
        else:
            exit_idx, reason = target, "forced"
            exit_price = last_close * (1 - FORCED_EXIT_SLIPPAGE * direction)
    gross = direction * (exit_price / entry - 1)
    entry_h, exit_h = LOAD_START_H + e, LOAD_START_H + exit_idx
    funding, fund_by_day = _funding_cost(s, direction, entry_h * HOUR_MS, exit_h * HOUR_MS)
    net = gross - 2 * COST_SIDE - funding
    # mark-to-market by UTC day: price path at day boundaries inside the holding period
    daily: dict[int, float] = {}
    prev = entry
    for day in range(entry_h // 24 + 1, (exit_h - 1) // 24 + 1):
        idx = day * 24 - LOAD_START_H
        price = next((s.open[m] for m in range(idx, exit_idx) if np.isfinite(s.open[m])), prev)
        daily[day - 1] = daily.get(day - 1, 0.0) + direction * (price - prev) / entry
        prev = price
    last_day = (exit_h - 1) // 24
    daily[last_day] = daily.get(last_day, 0.0) + direction * (exit_price - prev) / entry
    first_day = entry_h // 24
    daily[first_day] = daily.get(first_day, 0.0) - COST_SIDE
    daily[last_day] = daily.get(last_day, 0.0) - COST_SIDE
    for day, cost in fund_by_day.items():
        daily[day] = daily.get(day, 0.0) - cost
    lo = max(0, e - SIGMA_WINDOW)
    with np.errstate(invalid="ignore", divide="ignore"):
        past = s.open[lo + hold : e] / s.open[lo : e - hold] - 1 if e - hold > lo else np.array([])
    control = float(direction * np.nanmean(past)) if np.isfinite(past).sum() else math.nan
    return Trade(s.symbol, direction, entry_h, exit_h, reason, gross, net, funding, control, daily)


# ------------------------------------------------------------------------------------------ portfolio


def run_portfolio(candidates: list[Trade]) -> list[Trade]:
    """Chronological capacity check: at most MAX_POSITIONS open, one per symbol; exits free a slot
    at their exit hour before entries of that hour are considered."""
    accepted: list[Trade] = []
    open_until: dict[str, int] = {}
    for t in sorted(candidates, key=lambda t: (t.entry_h, t.symbol)):
        open_until = {sym: end for sym, end in open_until.items() if end > t.entry_h}
        if t.symbol in open_until or len(open_until) >= MAX_POSITIONS:
            continue
        accepted.append(t)
        open_until[t.symbol] = t.exit_h
    return accepted


def daily_returns(trades: list[Trade], first_day: int, end_day: int) -> np.ndarray:
    out = np.zeros(end_day - first_day)
    for t in trades:
        for day, pnl in t.daily.items():
            if first_day <= day < end_day:
                out[day - first_day] += WEIGHT * pnl
    return out


def trade_stats(trades: list[Trade]) -> dict[str, object]:
    if len(trades) < 2:
        return {"trades": len(trades)}
    net = np.array([t.net for t in trades])
    gross = np.array([t.gross for t in trades])
    excess = np.array([t.gross - t.control for t in trades if math.isfinite(t.control)])
    return {
        "trades": len(trades),
        "mean_net": round(float(net.mean()), 5),
        "t_net": round(float(net.mean() / (net.std(ddof=1) / math.sqrt(len(net)))), 3),
        "win_rate": round(float((net > 0).mean()), 3),
        "mean_gross": round(float(gross.mean()), 5),
        "mean_excess_over_control": round(float(excess.mean()), 5) if len(excess) else None,
        "stops": sum(t.reason == "stop" for t in trades),
        "forced_exits": sum(t.reason == "forced" for t in trades),
        "mean_funding": round(float(np.mean([t.funding for t in trades])), 6),
    }


def evaluate(series: list[Series], oi_drop: float, z_min: float, hold: int) -> tuple[list[Trade], np.ndarray]:
    candidates = [
        t
        for s in series
        for k, d in detect_events(s, oi_drop, z_min)
        if (t := make_trade(s, k, d, hold)) is not None and t.entry_h >= START_H
    ]
    accepted = run_portfolio(candidates)
    return accepted, daily_returns(accepted, START_H // 24, END_H // 24)


# ----------------------------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", type=Path, default=Path("data/archive"))
    ap.add_argument("--out", type=Path, default=Path("research/results/h005/summary.json"))
    args = ap.parse_args()

    symbols_1d, days_1d, a = H4.build_panel(args.archive)
    universe = H4.pit_universe(a["close"], a["qvol"])
    ever = [j for j in range(len(symbols_1d)) if universe[:, j].any()]
    hours = np.arange(LOAD_START_H, END_H)
    row = hours // 24 - 1 - int(days_1d[0])  # universe known at the previous day's close
    series = []
    coverage = {"hours_with_bar": 0, "hours_with_oi": 0, "universe_hours": 0}
    for j in ever:
        sym = symbols_1d[j]
        valid_row = (row >= 0) & (row < len(days_1d))
        in_univ = valid_row & universe[np.clip(row, 0, len(days_1d) - 1), j]
        bars, oi = load_hourly(args.archive, sym), load_open_interest(args.archive, sym)
        s = build_series(sym, bars, oi, in_univ, load_funding(args.archive, sym))
        series.append(s)
        coverage["universe_hours"] += int(in_univ.sum())
        coverage["hours_with_bar"] += int((in_univ & np.isfinite(s.open)).sum())
        coverage["hours_with_oi"] += int((in_univ & np.isfinite(s.oi_start) & np.isfinite(s.oi_end)).sum())

    trades, daily = evaluate(series, OI_DROP, Z_MIN, HOLD_H)
    stats = trade_stats(trades)
    m = H.metrics(daily)
    by_period = {
        name: trade_stats([t for t in trades if lo <= t.entry_h // 24 < hi])
        for name, (lo, hi) in PERIODS.items()
    }

    # DSR: daily-return trials H-002a dev, H-002b dev, H-003, H-004 and this one
    dev_panel = H.build_panel(args.archive, H.DATA_START_DAY, H.DEV[1] + 1)
    h3s, h3e = H._day("2020-01-01"), H._day("2023-01-01")
    funding_1d, has_funding = H4.add_funding(args.archive, symbols_1d, days_1d, universe.any(axis=0))
    h4_sim = H4.simulate(days_1d, a["open"], a["close"], funding_1d, has_funding, universe, "b")
    h4_ret = H.window(h4_sim, H4.PERIOD)["ret"]
    trial_returns = [
        H.window(H.simulate(*dev_panel, "a"), H.DEV)["ret"],
        H.window(H.simulate(*dev_panel, "b"), H.DEV)["ret"],
        H.window(H.simulate(*H.build_panel(args.archive, h3s, h3e), "b"), (h3s, h3e))["ret"],
        h4_ret,
        daily,
    ]
    sharpes = [float(r.mean() / r.std(ddof=0)) for r in trial_returns]
    dsr = purgedcv.deflated_sharpe_ratio(daily, N_TRIALS, float(np.var(sharpes, ddof=1)))

    criteria = {
        ">= 200 trades": stats["trades"] >= 200,
        "mean net per trade > 0 and t >= 2.0": stats.get("mean_net", -1) > 0 and stats.get("t_net", 0) >= 2.0,
        "mean excess over control >= 0.40%": (stats.get("mean_excess_over_control") or -1) >= 0.004,
        "portfolio Sharpe >= 0.8": m.get("sharpe", 0) >= 0.8,
        "max drawdown <= 30%": m.get("max_drawdown", 1) <= 0.30,
        "positive mean net in >= 4 of 5 periods": sum(
            (v.get("mean_net") or -1) > 0
            for v in by_period.values()  # type: ignore[operator]
        )
        >= 4,
        "DSR after 6 trials >= 0.95": dsr >= 0.95,
    }
    grid = {}
    for oi_drop in (-0.02, -0.03, -0.05):
        for z_min in (2.0, 3.0, 4.0):
            g_trades, g_daily = evaluate(series, oi_drop, z_min, HOLD_H)
            key = f"dOI<={oi_drop:.0%} |z|>={z_min:g}"
            grid[key] = {**trade_stats(g_trades), "sharpe": H.metrics(g_daily).get("sharpe")}
    horizons = {}
    for hold in (4, 24):
        h_trades, h_daily = evaluate(series, OI_DROP, Z_MIN, hold)
        horizons[f"{hold}h"] = {**trade_stats(h_trades), "sharpe": H.metrics(h_daily).get("sharpe")}
    report: dict[str, object] = {
        "hypothesis": "H-005",
        "period": "2020-10-01 -> 2025-06-30 (entries; exits inside the range)",
        "coverage": coverage,
        "trades": stats,
        "portfolio_daily": m,
        "avg_open_positions": round(
            float(sum((t.exit_h - t.entry_h) for t in trades) / (END_H - START_H)), 3
        ),
        "by_period": by_period,
        "by_side": {
            "long after crash": trade_stats([t for t in trades if t.direction > 0]),
            "short after spike": trade_stats([t for t in trades if t.direction < 0]),
        },
        "psr_sharpe_above_0": round(purgedcv.probabilistic_sharpe_ratio(daily, 0.0), 4),
        "dsr_after_6_trials": round(dsr, 4),
        "per_obs_sharpe_of_trials": [round(x, 5) for x in sharpes],
        "criteria": criteria,
        "decision": "ACCEPTED" if all(criteria.values()) else "REJECTED",
        "diagnostic_threshold_grid": grid,
        "diagnostic_horizons": horizons,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
