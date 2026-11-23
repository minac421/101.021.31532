"""H-001 study: sweep & reclaim of the previous UTC day's high/low (family S1).

Pre-registered in docs/research/H-001-sweep-reclaim.md (committed before this ran).
The rules below implement that document exactly; changing them makes a new hypothesis.

* the lockbox (2026-08-01 onwards) is never loaded;
* the validation period is evaluated only if the development period passes
  every pre-registered criterion (``--force-validation`` exists for audits only).

Usage:
    uv run --group research python research/h001_sweep_reclaim.py \
        --archive data/archive --out research/results/h001
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "LTCUSDT",
    "DOTUSDT",
    "TRXUSDT",
    "BCHUSDT",
    "ATOMUSDT",
    "NEARUSDT",
    "FILUSDT",
    "ETCUSDT",
    "UNIUSDT",
    "XLMUSDT",
    "AAVEUSDT",
]


def _ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1000)


DEV = (_ms("2023-01-01"), _ms("2025-07-01"))
VAL = (_ms("2025-07-01"), _ms("2026-08-01"))
LOCKBOX_START = _ms("2026-08-01")
YEARS = {
    "2023": (_ms("2023-01-01"), _ms("2024-01-01")),
    "2024": (_ms("2024-01-01"), _ms("2025-01-01")),
    "2025H1": (_ms("2025-01-01"), _ms("2025-07-01")),
}

BAR_MS, DAY_MS = 15 * 60_000, 86_400_000
ATR_N, VOL_N, VOL_MULT = 14, 96, 1.5
SWEEP_ATR, STOP_ATR = 0.10, 0.10
MIN_RISK, MAX_RISK = 0.0015, 0.05
TARGET_R, HOLD_BARS = 2.0, 96
FEE, SLIP, STOP_EXTRA = 0.0005, 0.0002, 0.0003
BASELINE_PER_TRADE = 20


@dataclass
class Bars:
    t: np.ndarray  # open time ms
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    qv: np.ndarray


@dataclass
class Trade:
    symbol: str
    side: int
    signal_ms: int
    entry_ms: int
    exit_ms: int
    entry: float
    stop: float
    target: float
    exit: float
    exit_type: str
    risk_pct: float
    gross_r: float
    cost_r: float
    funding_r: float
    net_r: float


def load_bars(root: Path, symbol: str) -> Bars:
    rows: list[tuple[int, float, float, float, float, float]] = []
    for path in sorted((root / f"data/futures/um/monthly/klines/{symbol}/15m").glob("*.zip")):
        with zipfile.ZipFile(path) as z:
            text = z.read(z.namelist()[0]).decode()
        for rec in csv.reader(io.StringIO(text)):
            if not rec or not rec[0].isdigit():
                continue  # header
            open_ms = int(rec[0])
            if open_ms >= LOCKBOX_START:
                continue  # the lockbox is never loaded
            rows.append((open_ms, float(rec[1]), float(rec[2]), float(rec[3]), float(rec[4]), float(rec[7])))
    rows.sort()
    arr = np.array(rows, dtype=np.float64)
    return Bars(arr[:, 0].astype(np.int64), arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4], arr[:, 5])


def load_funding(root: Path, symbol: str) -> tuple[np.ndarray, np.ndarray]:
    times, rates = [], []
    for path in sorted((root / f"data/futures/um/monthly/fundingRate/{symbol}").glob("*.zip")):
        with zipfile.ZipFile(path) as z:
            text = z.read(z.namelist()[0]).decode()
        for rec in csv.reader(io.StringIO(text)):
            if rec and rec[0].isdigit():
                times.append(int(rec[0]))
                rates.append(float(rec[2]))
    order = np.argsort(times)
    return np.array(times, dtype=np.int64)[order], np.array(rates)[order]


def indicators(b: Bars) -> dict[str, np.ndarray]:
    n = len(b.t)
    prev_close = np.concatenate([[b.c[0]], b.c[:-1]])
    tr = np.maximum(b.h - b.lo, np.maximum(np.abs(b.h - prev_close), np.abs(b.lo - prev_close)))
    csum = np.concatenate([[0.0], np.cumsum(tr)])
    atr = np.full(n, np.nan)
    atr[ATR_N - 1 :] = (csum[ATR_N:] - csum[:-ATR_N]) / ATR_N  # includes the signal bar
    med_vol = np.full(n, np.nan)
    windows = np.lib.stride_tricks.sliding_window_view(b.qv, VOL_N)[:-1]  # previous 96 bars, excluding t
    med_vol[VOL_N:] = np.median(windows, axis=1)
    day = b.t // DAY_MS
    pdh, pdl = np.full(n, np.nan), np.full(n, np.nan)
    uniq, start = np.unique(day, return_index=True)
    ends = np.append(start[1:], n)
    highs = {d: (b.h[s:e].max(), b.lo[s:e].min(), e - s) for d, s, e in zip(uniq, start, ends, strict=True)}
    for d, s, e in zip(uniq, start, ends, strict=True):
        prev = highs.get(d - 1)
        if prev is not None and prev[2] >= 90:  # previous day must be (nearly) complete
            pdh[s:e], pdl[s:e] = prev[0], prev[1]
    return {"atr": atr, "med_vol": med_vol, "pdh": pdh, "pdl": pdl, "day": day}


def simulate(
    b: Bars, i_signal: int, side: int, stop: float, f_t: np.ndarray, f_r: np.ndarray
) -> Trade | None:
    e = i_signal + 1
    if e >= len(b.t):
        return None
    entry = b.o[e]
    d = (entry - stop) * side
    if d <= 0 or not MIN_RISK <= d / entry <= MAX_RISK:
        return None
    target = entry + side * TARGET_R * d
    last = min(i_signal + HOLD_BARS, len(b.t) - 1)
    exit_px, exit_type, exit_ms = b.c[last], "TIME", int(b.t[last]) + BAR_MS - 1
    for i in range(e, last + 1):
        hit_stop = b.lo[i] <= stop if side > 0 else b.h[i] >= stop
        hit_target = b.h[i] >= target if side > 0 else b.lo[i] <= target
        if hit_stop:  # stop first when both are inside the bar (conservative)
            gapped = (b.o[i] <= stop) if side > 0 else (b.o[i] >= stop)
            exit_px = b.o[i] if (gapped and i > e) else stop
            exit_type, exit_ms = "STOP", int(b.t[i])
            break
        if hit_target:
            exit_px, exit_type, exit_ms = target, "TARGET", int(b.t[i])
            break
    gross_r = (exit_px - entry) * side / d
    cost = entry * (FEE + SLIP) + exit_px * (FEE + SLIP + (STOP_EXTRA if exit_type == "STOP" else 0.0))
    entry_ms = int(b.t[e])
    lo_i, hi_i = np.searchsorted(f_t, entry_ms, side="right"), np.searchsorted(f_t, exit_ms, side="right")
    funding = 0.0
    for k in range(lo_i, hi_i):
        bar = min(max(np.searchsorted(b.t, f_t[k], side="right") - 1, 0), len(b.t) - 1)
        funding += side * f_r[k] * b.c[bar]
    return Trade(
        symbol="",
        side=side,
        signal_ms=int(b.t[i_signal]),
        entry_ms=entry_ms,
        exit_ms=exit_ms,
        entry=entry,
        stop=stop,
        target=target,
        exit=exit_px,
        exit_type=exit_type,
        risk_pct=d / entry,
        gross_r=gross_r,
        cost_r=cost / d,
        funding_r=funding / d,
        net_r=gross_r - cost / d - funding / d,
    )


def run_symbol(
    root: Path, symbol: str
) -> tuple[list[Trade], Bars, tuple[np.ndarray, np.ndarray], dict[str, int]]:
    b = load_bars(root, symbol)
    f_t, f_r = load_funding(root, symbol)
    ind = indicators(b)
    gaps = int(np.sum(np.diff(b.t) != BAR_MS))
    trades: list[Trade] = []
    busy_until = -1
    long_day, short_day = -1, -1
    stats = {
        "bars": len(b.t),
        "gaps": gaps,
        "long_signals": 0,
        "short_signals": 0,
        "conflicts": 0,
        "skipped_busy": 0,
        "skipped_risk": 0,
    }
    for t in range(max(ATR_N, VOL_N), len(b.t) - 1):
        atr, pdl, pdh, mv = ind["atr"][t], ind["pdl"][t], ind["pdh"][t], ind["med_vol"][t]
        if math.isnan(atr) or math.isnan(pdl) or math.isnan(mv) or b.qv[t] < VOL_MULT * mv:
            continue
        day = int(ind["day"][t])
        is_long = b.lo[t] < pdl - SWEEP_ATR * atr and b.c[t] > pdl and long_day != day
        is_short = b.h[t] > pdh + SWEEP_ATR * atr and b.c[t] < pdh and short_day != day
        if is_long:
            long_day = day
        if is_short:
            short_day = day
        if is_long and is_short:
            stats["conflicts"] += 1
            continue
        if not (is_long or is_short):
            continue
        stats["long_signals" if is_long else "short_signals"] += 1
        if t <= busy_until:
            stats["skipped_busy"] += 1
            continue
        side = 1 if is_long else -1
        stop = b.lo[t] - STOP_ATR * atr if is_long else b.h[t] + STOP_ATR * atr
        trade = simulate(b, t, side, stop, f_t, f_r)
        if trade is None:
            stats["skipped_risk"] += 1
            continue
        trade.symbol = symbol
        trades.append(trade)
        busy_until = int(np.searchsorted(b.t, trade.exit_ms, side="right")) - 1
    return trades, b, (f_t, f_r), stats


def baseline(
    trades: list[Trade],
    data: dict[str, tuple[Bars, tuple[np.ndarray, np.ndarray]]],
    period: tuple[int, int],
    rng: np.random.Generator,
) -> np.ndarray:
    out = []
    for tr in trades:
        b, (f_t, f_r) = data[tr.symbol]
        lo, hi = np.searchsorted(b.t, period[0]), np.searchsorted(b.t, period[1]) - 2
        for _ in range(BASELINE_PER_TRADE):
            t = int(rng.integers(max(lo, VOL_N), hi))
            entry = b.o[t + 1]
            stop = entry * (1 - tr.side * tr.risk_pct)
            sim = simulate(b, t, tr.side, stop, f_t, f_r)
            if sim is not None:
                out.append(sim.net_r)
    return np.array(out)


def metrics(r: np.ndarray) -> dict[str, float]:
    if len(r) == 0:
        return {"n": 0}
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    curve = np.cumsum(r)
    std = float(r.std(ddof=1)) if len(r) > 1 else float("nan")
    return {
        "n": len(r),
        "win_rate": round(float((r > 0).mean()), 4),
        "mean_r": round(float(r.mean()), 4),
        "median_r": round(float(np.median(r)), 4),
        "t_stat": round(float(r.mean() / (std / math.sqrt(len(r)))), 3) if std > 0 else float("nan"),
        "profit_factor": round(float(wins / losses), 3) if losses > 0 else float("inf"),
        "sum_r": round(float(r.sum()), 2),
        "max_drawdown_r": round(float((np.maximum.accumulate(curve) - curve).max()), 2),
    }


def evaluate(trades: list[Trade], period: tuple[int, int]) -> list[Trade]:
    return sorted((t for t in trades if period[0] <= t.entry_ms < period[1]), key=lambda t: t.entry_ms)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", type=Path, default=Path("data/archive"))
    ap.add_argument("--out", type=Path, default=Path("research/results/h001"))
    ap.add_argument("--force-validation", action="store_true", help="audit only; violates the protocol")
    args = ap.parse_args()

    all_trades: list[Trade] = []
    data: dict[str, tuple[Bars, tuple[np.ndarray, np.ndarray]]] = {}
    symbol_stats = {}
    for symbol in SYMBOLS:
        trades, bars, funding, stats = run_symbol(args.archive, symbol)
        all_trades += trades
        data[symbol] = (bars, funding)
        symbol_stats[symbol] = stats

    dev = evaluate(all_trades, DEV)
    r = np.array([t.net_r for t in dev])
    rng = np.random.default_rng(20261123)
    base = baseline(dev, data, DEV, rng)
    report: dict[str, object] = {
        "hypothesis": "H-001",
        "development": metrics(r),
        "development_gross_mean_r": round(float(np.mean([t.gross_r for t in dev])), 4) if dev else None,
        "development_mean_cost_r": round(float(np.mean([t.cost_r + t.funding_r for t in dev])), 4)
        if dev
        else None,
        "baseline_random": metrics(base),
        "by_year": {
            y: metrics(np.array([t.net_r for t in dev if p[0] <= t.entry_ms < p[1]]))
            for y, p in YEARS.items()
        },
        "by_side": {
            s: metrics(np.array([t.net_r for t in dev if t.side == v]))
            for s, v in (("long", 1), ("short", -1))
        },
        "by_exit": {k: sum(1 for t in dev if t.exit_type == k) for k in ("TARGET", "STOP", "TIME")},
        "by_symbol": {s: metrics(np.array([t.net_r for t in dev if t.symbol == s])) for s in SYMBOLS},
        "data": symbol_stats,
    }
    dm, bm = report["development"], report["baseline_random"]
    assert isinstance(dm, dict)
    assert isinstance(bm, dict)
    by_year = report["by_year"]
    assert isinstance(by_year, dict)
    criteria = {
        "trades >= 300": dm.get("n", 0) >= 300,
        "mean R >= 0.05": dm.get("mean_r", -1) >= 0.05,
        "t-stat >= 2.0": dm.get("t_stat", 0) >= 2.0,
        "profit factor >= 1.15": dm.get("profit_factor", 0) >= 1.15,
        "positive every year": all(by_year[y].get("mean_r", -1) > 0 for y in YEARS),
        "beats random baseline by >= 0.10R": dm.get("mean_r", -1) - bm.get("mean_r", 0) >= 0.10,
    }
    report["criteria"] = criteria
    accepted = all(criteria.values())
    report["development_decision"] = "ACCEPTED" if accepted else "REJECTED"
    if accepted or args.force_validation:
        val = evaluate(all_trades, VAL)
        vm = metrics(np.array([t.net_r for t in val]))
        report["validation"] = vm
        report["validation_decision"] = (
            "ACCEPTED" if vm.get("mean_r", -1) > 0 and vm.get("profit_factor", 0) >= 1.05 else "REJECTED"
        )
        if args.force_validation:
            report["protocol_note"] = "validation forced for audit; does not count as a protocol evaluation"
    else:
        report["validation"] = "not evaluated (development rejected) — per pre-registration"

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    with open(args.out / "development_trades.csv", "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(dev[0]).keys()) if dev else ["symbol"])
        writer.writeheader()
        for t in dev:
            writer.writerow(asdict(t))
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "development",
                    "baseline_random",
                    "by_year",
                    "by_side",
                    "by_exit",
                    "criteria",
                    "development_decision",
                    "validation",
                )
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
