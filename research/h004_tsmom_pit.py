"""H-004: H-002b rules on a point-in-time universe (delisted symbols included), without look-ahead.

Pre-registered in docs/research/H-004-tsmom-point-in-time-universe.md (committed before this ran).

* Universe at the close of day d: the 20 USDT perpetuals with the highest mean quote volume over
  d-29..d, among symbols with a daily bar on each of d-30..d.
* Signal, sizing, costs and funding: H-002b constants, imported from research/h002_tsmom.py.
* No look-ahead: a decision at the close of d uses data up to d only. A symbol without a bar on
  d+1 cannot be traded; one that stops trading while held exits at its last close with an extra
  1 % slippage. Long positions with no funding record that day are charged 0.03 % per day.

Usage:
    uv run --group research python research/h004_tsmom_pit.py --archive data/archive
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import math
import zipfile
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


H = _load("h002_tsmom")  # H-002b constants, metrics and windows: the rules are imported, not copied
is_candidate = _load("fetch_universe_archive").is_candidate

DAY_MS = H.DAY_MS
N_TRIALS = 5  # H-001, H-002a, H-002b, H-003, H-004
TOP_N = 20
FORCED_EXIT_SLIPPAGE = 0.01
MISSING_FUNDING_PER_DAY = 0.0003
DATA_START_DAY = H._day("2019-09-01")
PIT_END_DAY = H._day("2025-07-01")  # validation (2025-07 onwards) and lockbox are never loaded
PERIOD = (H._day("2020-01-01"), PIT_END_DAY)
YEARS = {y: (H._day(f"{y}-01-01"), H._day(f"{int(y) + 1}-01-01")) for y in ("2020", "2021", "2022", "2023")}
YEARS["2024"] = (H._day("2024-01-01"), H._day("2025-01-01"))
YEARS["2025H1"] = (H._day("2025-01-01"), PIT_END_DAY)


def _ms(raw: str) -> int:
    t = int(raw)
    return t // 1000 if t > 10**14 else t  # tolerate microsecond timestamps


def daily_bars(root: Path, symbol: str) -> dict[int, tuple[float, float, float]]:
    """day -> (open, close, quote volume) from the archive's 1d klines, registered range only.

    Monthly files first; official daily files fill the days some monthly files are missing.
    Zero-volume bars are dropped (no trade happened, so the day is not tradable)."""
    out: dict[int, tuple[float, float, float]] = {}
    for frequency in ("monthly", "daily"):
        for path in sorted((root / f"data/futures/um/{frequency}/klines/{symbol}/1d").glob("*.zip")):
            with zipfile.ZipFile(path) as z:
                text = z.read(z.namelist()[0]).decode()
            for rec in csv.reader(io.StringIO(text)):
                if rec and rec[0].isdigit():
                    day = _ms(rec[0]) // DAY_MS
                    # a bar counts only if something traded: after a delisting the archive keeps
                    # flat zero-volume bars (SRMUSDT: 560 days), which must not look tradable
                    if DATA_START_DAY <= day < PIT_END_DAY and day not in out and float(rec[7]) > 0:
                        out[day] = (float(rec[1]), float(rec[4]), float(rec[7]))
    return out


def funding_records(root: Path, symbol: str) -> dict[int, float]:
    """holding day -> summed funding rate in (open, next open], registered range only."""
    out: dict[int, float] = {}
    for path in sorted((root / f"data/futures/um/monthly/fundingRate/{symbol}").glob("*.zip")):
        with zipfile.ZipFile(path) as z:
            text = z.read(z.namelist()[0]).decode()
        for rec in csv.reader(io.StringIO(text)):
            if rec and rec[0].isdigit():
                holding_day = (_ms(rec[0]) - 1) // DAY_MS
                if DATA_START_DAY <= holding_day < PIT_END_DAY:
                    out[holding_day] = out.get(holding_day, 0.0) + float(rec[2])
    return out


def pit_universe(closes: np.ndarray, qvol: np.ndarray, top_n: int = TOP_N) -> np.ndarray:
    """Boolean (day, symbol) mask: top_n by mean quote volume over the last 30 days among symbols
    with a bar on each of the last 31 days. Row i uses rows <= i only."""
    n_days, n_sym = closes.shape
    present = np.vstack([np.zeros((1, n_sym)), np.cumsum(~np.isnan(closes), axis=0)])
    volume = np.vstack([np.zeros((1, n_sym)), np.cumsum(np.nan_to_num(qvol), axis=0)])
    mask = np.zeros((n_days, n_sym), dtype=bool)
    for i in range(H.VOL_N, n_days):
        eligible = (present[i + 1] - present[i - H.VOL_N]) == H.VOL_N + 1
        k = min(top_n, int(eligible.sum()))
        if k:
            score = np.where(eligible, (volume[i + 1] - volume[i + 1 - H.VOL_N]) / H.VOL_N, -np.inf)
            mask[i, np.argsort(-score, kind="stable")[:k]] = True
    return mask


def simulate(
    days: np.ndarray,
    opens: np.ndarray,
    closes: np.ndarray,
    funding: np.ndarray,
    has_funding: np.ndarray,
    universe: np.ndarray,
    variant: str,
) -> dict[str, np.ndarray]:
    """variant 'b' = H-002b rules, 'baseline' = equal-weight buy-and-hold of the same universe."""
    n_days, n_sym = closes.shape
    w_prev = np.zeros(n_sym)
    keys = ("day", "ret", "gross", "turnover", "funding", "n", "forced", "fallback")
    rec: dict[str, list[float]] = {k: [] for k in keys}
    for i in range(H.VOL_N + 1, n_days - 2):
        u = universe[i]
        with np.errstate(invalid="ignore", divide="ignore"):
            if variant == "baseline":
                w = np.where(u, 1.0 / max(int(u.sum()), 1), 0.0)
            else:
                window = closes[i - H.VOL_N : i + 1]
                sigma = np.std(window[1:] / window[:-1] - 1, axis=0, ddof=1)
                ok = u & (sigma > 0)
                r28 = np.where(ok, closes[i] / np.where(ok, closes[i - H.LOOKBACK], 1.0) - 1, 0.0)
                size = np.minimum((H.TARGET_VOL / math.sqrt(365)) / np.where(ok, sigma, 1.0), H.MAX_W)
                w = np.where(ok & (r28 > 0), size, 0.0) / max(int(ok.sum()), 1)
                gross = np.abs(w).sum()
                if gross > H.GROSS_CAP:
                    w = w * (H.GROSS_CAP / gross)
            tradable = ~np.isnan(opens[i + 1])  # execution needs a bar on the next day
            w = np.where(tradable, w, 0.0)
            stays = ~np.isnan(opens[i + 2])  # unknown at decision time: only used to settle the day
            hold = np.where(
                stays,
                opens[i + 2] / opens[i + 1] - 1,
                closes[i + 1] / opens[i + 1] - 1 - FORCED_EXIT_SLIPPAGE,
            )
        held = w != 0
        missing = held & (w > 0) & ~has_funding[i + 1]
        rate = np.where(has_funding[i + 1], funding[i + 1], np.where(missing, MISSING_FUNDING_PER_DAY, 0.0))
        fund_cost = float(np.where(held, w * rate, 0.0).sum())
        turnover = float(np.abs(w - w_prev).sum())
        ret = float(np.where(held, w * hold, 0.0).sum()) - H.COST * turnover - fund_cost
        rec["day"].append(float(days[i + 1]))
        rec["ret"].append(ret)
        rec["gross"].append(float(np.abs(w).sum()))
        rec["turnover"].append(turnover)
        rec["funding"].append(fund_cost)
        rec["n"].append(float(held.sum()))
        rec["forced"].append(float((held & ~stays).sum()))
        rec["fallback"].append(float(missing.sum()))
        w_prev = w
    out = {k: np.array(v) for k, v in rec.items()}
    out["day"] = out["day"].astype(np.int64)
    return out


def build_panel(root: Path) -> tuple[list[str], np.ndarray, dict[str, np.ndarray]]:
    base = root / "data/futures/um/monthly/klines"
    symbols = sorted(p.name for p in base.iterdir() if is_candidate(p.name) and (p / "1d").is_dir())
    bars = {s: b for s in symbols if (b := daily_bars(root, s))}
    symbols = sorted(bars)
    days = np.arange(min(min(b) for b in bars.values()), max(max(b) for b in bars.values()) + 1)
    index = {int(d): i for i, d in enumerate(days)}
    shape = (len(days), len(symbols))
    arrays = {k: np.full(shape, np.nan) for k in ("open", "close", "qvol")}
    for j, s in enumerate(symbols):
        for d, (o, c, q) in bars[s].items():
            i = index[d]
            arrays["open"][i, j], arrays["close"][i, j], arrays["qvol"][i, j] = o, c, q
    return symbols, days, arrays


def add_funding(
    root: Path, symbols: list[str], days: np.ndarray, needed: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    funding = np.zeros((len(days), len(symbols)))
    has = np.zeros((len(days), len(symbols)), dtype=bool)
    index = {int(d): i for i, d in enumerate(days)}
    for j, s in enumerate(symbols):
        if not needed[j]:
            continue
        for d, rate in funding_records(root, s).items():
            if d in index:
                funding[index[d], j], has[index[d], j] = rate, True
    return funding, has


def summarise(sim: dict[str, np.ndarray], period: tuple[int, int]) -> dict[str, object]:
    w = H.window(sim, period)
    return {
        **H.metrics(w["ret"]),
        "avg_gross_exposure": round(float(w["gross"].mean()), 3),
        "avg_positions": round(float(w["n"].mean()), 2),
        "avg_daily_turnover": round(float(w["turnover"].mean()), 4),
        "funding_drag_ann": round(float(w["funding"].mean() * 365), 4),
        "forced_exits_while_held": int(w["forced"].sum()),
        "funding_fallback_position_days": int(w["fallback"].sum()),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", type=Path, default=Path("data/archive"))
    ap.add_argument("--out", type=Path, default=Path("research/results/h004/summary.json"))
    args = ap.parse_args()

    symbols, days, a = build_panel(args.archive)
    universe = pit_universe(a["close"], a["qvol"])
    fixed = np.array([s in H.SYMBOLS for s in symbols])
    fixed_universe = pit_universe(np.where(fixed, a["close"], np.nan), a["qvol"], top_n=len(H.SYMBOLS))
    funding, has_funding = add_funding(args.archive, symbols, days, universe.any(axis=0) | fixed)

    runs = {
        name: simulate(days, a["open"], a["close"], funding, has_funding, uni, variant)
        for name, uni, variant in (
            ("pit_b", universe, "b"),
            ("pit_baseline", universe, "baseline"),
            ("fixed20_b", fixed_universe, "b"),
        )
    }
    pit, base = runs["pit_b"], runs["pit_baseline"]
    m, bm = H.metrics(H.window(pit, PERIOD)["ret"]), H.metrics(H.window(base, PERIOD)["ret"])
    by_year = {y: H.metrics(H.window(pit, p)["ret"]) for y, p in YEARS.items()}
    base_by_year = {y: H.metrics(H.window(base, p)["ret"]) for y, p in YEARS.items()}

    # DSR: variance of per-observation daily Sharpe across the trials that produced daily returns
    dev_panel = H.build_panel(args.archive, H.DATA_START_DAY, H.DEV[1] + 1)
    h003_start, h003_end = H._day("2020-01-01"), H._day("2023-01-01")
    h003_panel = H.build_panel(args.archive, h003_start, h003_end)
    trial_returns = [
        H.window(H.simulate(*dev_panel, "a"), H.DEV)["ret"],
        H.window(H.simulate(*dev_panel, "b"), H.DEV)["ret"],
        H.window(H.simulate(*h003_panel, "b"), (h003_start, h003_end))["ret"],
        H.window(pit, PERIOD)["ret"],
    ]
    sharpes = [float(r.mean() / r.std(ddof=0)) for r in trial_returns]
    dsr = purgedcv.deflated_sharpe_ratio(trial_returns[-1], N_TRIALS, float(np.var(sharpes, ddof=1)))
    loss_2022 = max(0.0, -by_year["2022"]["total_return"])
    base_loss_2022 = max(0.0, -base_by_year["2022"]["total_return"])
    criteria = {
        "Sharpe >= 0.8": m["sharpe"] >= 0.8,
        "t-stat >= 2.0": m["t_stat"] >= 2.0,
        "Sharpe >= buy-and-hold (same universe)": m["sharpe"] >= bm["sharpe"],
        "max drawdown <= 30%": m["max_drawdown"] <= 0.30,
        "2022 loss <= half of buy-and-hold loss": loss_2022 <= 0.5 * base_loss_2022,
        "DSR after 5 trials >= 0.95": dsr >= 0.95,
    }
    stopped = sorted(
        {
            symbols[j]
            for i in range(len(days) - 2)
            for j in np.flatnonzero(universe[i])
            if np.isnan(a["open"][i + 2, j])
        }
    )
    # first holding days of the published H-002 / H-003 series (31-day warm-up after their data start)
    h002_window, h003_window = (H._day("2023-02-02"), H.DEV[1]), (H._day("2020-02-02"), H._day("2022-12-31"))
    report: dict[str, object] = {
        "hypothesis": "H-004",
        "period": "2020-01-01 -> 2025-06-29 (holding days; data 2019-09 -> 2025-06)",
        "universe": {
            "symbols_in_archive_range": len(symbols),
            "symbols_ever_in_universe": int(universe.any(axis=0).sum()),
            "universe_members_that_stopped_trading": stopped,
        },
        "strategy": summarise(pit, PERIOD),
        "strategy_by_year": by_year,
        "buy_and_hold": summarise(base, PERIOD),
        "buy_and_hold_by_year": base_by_year,
        "psr_sharpe_above_0": round(purgedcv.probabilistic_sharpe_ratio(trial_returns[-1], 0.0), 4),
        "dsr_after_5_trials": round(dsr, 4),
        "per_obs_sharpe_of_trials": [round(s, 5) for s in sharpes],
        "criteria": criteria,
        "decision": "ACCEPTED" if all(criteria.values()) else "REJECTED",
        "diagnostic_fixed20_same_simulator": {
            "H-003 window (published Sharpe 1.284)": summarise(runs["fixed20_b"], h003_window),
            "H-002 development window (published Sharpe 1.114)": summarise(runs["fixed20_b"], h002_window),
            "full period": summarise(runs["fixed20_b"], PERIOD),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
