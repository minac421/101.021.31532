"""H-002 study: 28-day time-series momentum with volatility targeting, daily rebalancing.

Pre-registered in docs/research/H-002-time-series-momentum.md (committed before this ran).
Variants: a = long/short (sign of the 28-day return), b = long-only (flat when negative).
The lockbox (2026-08-01 onwards) is never loaded; validation is evaluated only for a variant
that passes every development criterion.

Usage:
    uv run --group research python research/h002_tsmom.py --archive data/archive --out research/results/h002
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
    "LTCUSDT", "DOTUSDT", "TRXUSDT", "BCHUSDT", "ATOMUSDT", "NEARUSDT", "FILUSDT", "ETCUSDT", "UNIUSDT",
    "XLMUSDT", "AAVEUSDT",
]  # fmt: skip
DAY_MS = 86_400_000


def _day(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp() * 1000) // DAY_MS


DEV = (_day("2023-01-01"), _day("2025-07-01"))
VAL = (_day("2025-07-01"), _day("2026-08-01"))
LOCKBOX_DAY = _day("2026-08-01")
DATA_START_DAY = _day("2023-01-01")  # H-002 was registered on data from 2023-01-01
YEARS = {"2023": (_day("2023-01-01"), _day("2024-01-01")), "2024": (_day("2024-01-01"), _day("2025-01-01")),
         "2025H1": (_day("2025-01-01"), _day("2025-07-01"))}  # fmt: skip

LOOKBACK, VOL_N, TARGET_VOL, MAX_W, GROSS_CAP, COST = 28, 30, 0.40, 2.0, 2.0, 0.0007


def daily_bars(
    root: Path, symbol: str, start_day: int = DATA_START_DAY, end_day: int = LOCKBOX_DAY
) -> dict[int, tuple[float, float]]:
    """day -> (open, close) from 15m klines; days with fewer than 90 bars are dropped."""
    per_day: dict[int, list[tuple[int, float, float]]] = {}
    for path in sorted((root / f"data/futures/um/monthly/klines/{symbol}/15m").glob("*.zip")):
        with zipfile.ZipFile(path) as z:
            text = z.read(z.namelist()[0]).decode()
        for rec in csv.reader(io.StringIO(text)):
            if rec and rec[0].isdigit():
                day = int(rec[0]) // DAY_MS
                if start_day <= day < min(end_day, LOCKBOX_DAY):  # registered range only
                    per_day.setdefault(day, []).append((int(rec[0]), float(rec[1]), float(rec[4])))
    out = {}
    for day, bars in per_day.items():
        if len(bars) >= 90:
            bars.sort()
            out[day] = (bars[0][1], bars[-1][2])
    return out


def funding_by_holding_day(
    root: Path, symbol: str, start_day: int = DATA_START_DAY, end_day: int = LOCKBOX_DAY
) -> dict[int, float]:
    """Sum of funding rates charged while holding from open(day) to open(day+1), i.e. in (open, next open]."""
    out: dict[int, float] = {}
    for path in sorted((root / f"data/futures/um/monthly/fundingRate/{symbol}").glob("*.zip")):
        with zipfile.ZipFile(path) as z:
            text = z.read(z.namelist()[0]).decode()
        for rec in csv.reader(io.StringIO(text)):
            if rec and rec[0].isdigit():
                t = int(rec[0])
                holding_day = (t - 1) // DAY_MS  # (open, next open] window
                if start_day <= holding_day < min(end_day, LOCKBOX_DAY):
                    out[holding_day] = out.get(holding_day, 0.0) + float(rec[2])
    return out


def build_panel(
    root: Path, start_day: int = DATA_START_DAY, end_day: int = LOCKBOX_DAY
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    bars = {s: daily_bars(root, s, start_day, end_day) for s in SYMBOLS}
    fund = {s: funding_by_holding_day(root, s, start_day, end_day) for s in SYMBOLS}
    bars = {s: b for s, b in bars.items() if b}  # symbols with no data in range are skipped
    if not bars:
        raise SystemExit("no data in the requested range")
    first = min(min(b) for b in bars.values())
    last = max(max(b) for b in bars.values())
    days = np.arange(first, last + 1)
    shape = (len(days), len(SYMBOLS))
    opens, closes, funding = np.full(shape, np.nan), np.full(shape, np.nan), np.zeros(shape)
    for j, s in enumerate(SYMBOLS):
        for i, d in enumerate(days):
            if d in bars.get(s, {}):
                opens[i, j], closes[i, j] = bars[s][d]
            funding[i, j] = fund[s].get(int(d), 0.0)
    return days, opens, closes, funding


def simulate(days: np.ndarray, opens: np.ndarray, closes: np.ndarray, funding: np.ndarray,
             variant: str) -> dict[str, np.ndarray]:  # fmt: skip
    n_days, n_sym = closes.shape
    w_prev = np.zeros(n_sym)
    rec_day, rec_ret, rec_gross, rec_turn, rec_fund = [], [], [], [], []
    for i in range(VOL_N + 1, n_days - 2):
        c = closes[i]
        if variant == "baseline":
            valid = ~np.isnan(c) & ~np.isnan(opens[i + 1]) & ~np.isnan(opens[i + 2])
            w = np.where(valid, 1.0 / max(valid.sum(), 1), 0.0)
        else:
            past = closes[i - LOOKBACK]
            window = closes[i - VOL_N : i + 1]
            rets = window[1:] / window[:-1] - 1
            sigma = np.std(rets, axis=0, ddof=1)
            valid = (~np.isnan(c) & ~np.isnan(past) & ~np.isnan(sigma) & (sigma > 0)
                     & ~np.isnan(opens[i + 1]) & ~np.isnan(opens[i + 2]))  # fmt: skip
            r28 = np.where(valid, c / np.where(valid, past, 1.0) - 1, 0.0)
            s = np.sign(r28) if variant == "a" else (r28 > 0).astype(float)
            n_valid = max(int(valid.sum()), 1)
            raw = np.where(
                valid, s * np.minimum((TARGET_VOL / math.sqrt(365)) / np.where(valid, sigma, 1.0), MAX_W), 0
            )
            w = raw / n_valid
            gross = np.abs(w).sum()
            if gross > GROSS_CAP:
                w = w * (GROSS_CAP / gross)
        hold = np.where(valid, opens[i + 2] / np.where(valid, opens[i + 1], 1.0) - 1, 0.0)
        turnover = np.abs(w - w_prev).sum()
        fund_cost = float((w * funding[i + 1]).sum())
        ret = float((w * hold).sum()) - COST * turnover - fund_cost
        rec_day.append(days[i + 1])
        rec_ret.append(ret)
        rec_gross.append(np.abs(w).sum())
        rec_turn.append(turnover)
        rec_fund.append(fund_cost)
        w_prev = w
    return {"day": np.array(rec_day), "ret": np.array(rec_ret), "gross": np.array(rec_gross),
            "turnover": np.array(rec_turn), "funding": np.array(rec_fund)}  # fmt: skip


def metrics(r: np.ndarray) -> dict[str, float]:
    if len(r) < 2:
        return {"days": len(r)}
    equity = np.cumprod(1 + r)
    std = float(r.std(ddof=1))
    return {
        "days": len(r),
        "total_return": round(float(equity[-1] - 1), 4),
        "ann_return": round(float(r.mean() * 365), 4),
        "ann_vol": round(std * math.sqrt(365), 4),
        "sharpe": round(float(r.mean() / std * math.sqrt(365)), 3) if std > 0 else float("nan"),
        "t_stat": round(float(r.mean() / (std / math.sqrt(len(r)))), 3) if std > 0 else float("nan"),
        "max_drawdown": round(float((1 - equity / np.maximum.accumulate(equity)).max()), 4),
    }


def window(sim: dict[str, np.ndarray], period: tuple[int, int]) -> dict[str, np.ndarray]:
    m = (sim["day"] >= period[0]) & (sim["day"] < period[1])
    return {k: v[m] for k, v in sim.items()}


def evaluate(sim: dict[str, np.ndarray], base: dict[str, np.ndarray]) -> dict[str, object]:
    dev, bdev = window(sim, DEV), window(base, DEV)
    dm, bm = metrics(dev["ret"]), metrics(bdev["ret"])
    by_year = {y: metrics(window(sim, p)["ret"]) for y, p in YEARS.items()}
    criteria = {
        ">= 700 days": dm["days"] >= 700,
        "Sharpe >= 0.8": dm.get("sharpe", 0) >= 0.8,
        "t-stat >= 2.0": dm.get("t_stat", 0) >= 2.0,
        "positive every year": all(v.get("total_return", -1) > 0 for v in by_year.values()),
        "max drawdown <= 30%": dm.get("max_drawdown", 1) <= 0.30,
        "Sharpe >= buy-and-hold": dm.get("sharpe", 0) >= bm.get("sharpe", 0),
    }
    return {
        "development": dm,
        "by_year": by_year,
        "avg_gross_exposure": round(float(dev["gross"].mean()), 3),
        "avg_daily_turnover": round(float(dev["turnover"].mean()), 4),
        "funding_drag_ann": round(float(dev["funding"].mean() * 365), 4),
        "criteria": criteria,
        "development_decision": "ACCEPTED" if all(criteria.values()) else "REJECTED",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", type=Path, default=Path("data/archive"))
    ap.add_argument("--out", type=Path, default=Path("research/results/h002"))
    args = ap.parse_args()
    days, opens, closes, funding = build_panel(args.archive)
    base = simulate(days, opens, closes, funding, "baseline")
    report: dict[str, object] = {
        "hypothesis": "H-002",
        "baseline_buy_and_hold": {
            "development": metrics(window(base, DEV)["ret"]),
            "by_year": {y: metrics(window(base, p)["ret"]) for y, p in YEARS.items()},
        },
    }
    for variant in ("a", "b"):
        sim = simulate(days, opens, closes, funding, variant)
        result = evaluate(sim, base)
        if result["development_decision"] == "ACCEPTED":
            vm = metrics(window(sim, VAL)["ret"])
            result["validation"] = vm
            result["validation_decision"] = (
                "ACCEPTED" if vm.get("sharpe", 0) >= 0.5 and vm.get("total_return", -1) > 0 else "REJECTED"
            )
        else:
            result["validation"] = "not evaluated (development rejected) — per pre-registration"
        report[f"H-002{variant}"] = result
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
