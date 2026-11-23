"""H-006: the frozen H-004 rules, evaluated once on the validation year 2025-07-01 -> 2026-07-31.

Pre-registered in docs/research/H-006-trend-validation-year.md (committed before any
validation-year data was downloaded). Every rule comes from research/h004_tsmom_pit.py
unchanged; only the data range of the loaders is extended. The lockbox (2026-08-01 onwards)
is never loaded: the loaders cap every range at H-002's LOCKBOX_DAY.

Usage:
    uv run --group research python research/h006_trend_validation.py --archive data/archive
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import purgedcv


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


H4 = _load("h004_tsmom_pit")
H = H4.H

N_TRIALS = 6  # no new configuration: H-006 re-evaluates the H-004 rules on unseen data
VALIDATION = (H._day("2025-07-01"), H.LOCKBOX_DAY)
COMBINED = (H._day("2020-01-01"), H.LOCKBOX_DAY)


def monthly(sim: dict[str, np.ndarray], period: tuple[int, int]) -> dict[str, float]:
    w = H.window(sim, period)
    out: dict[str, float] = {}
    for day, ret in zip(w["day"].tolist(), w["ret"].tolist(), strict=True):
        key = np.datetime64(int(day), "D").astype("datetime64[M]").astype(str)
        out[key] = out.get(key, 1.0) * (1 + ret)
    return {k: round(v - 1, 4) for k, v in out.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", type=Path, default=Path("data/archive"))
    ap.add_argument("--out", type=Path, default=Path("research/results/h006/summary.json"))
    args = ap.parse_args()

    symbols, days, a = H4.build_panel(args.archive, end_day=H.LOCKBOX_DAY)
    universe = H4.pit_universe(a["close"], a["qvol"])
    in_val = (days >= VALIDATION[0]) & (days < VALIDATION[1])
    needed = universe.any(axis=0)
    funding, has_funding = H4.add_funding(args.archive, symbols, days, needed, end_day=H.LOCKBOX_DAY)
    pit = H4.simulate(days, a["open"], a["close"], funding, has_funding, universe, "b")
    base = H4.simulate(days, a["open"], a["close"], funding, has_funding, universe, "baseline")

    m = H.metrics(H.window(pit, VALIDATION)["ret"])
    criteria = {
        "Sharpe >= 0.5": m.get("sharpe", 0) >= 0.5,
        "net total return > 0": m.get("total_return", -1) > 0,
        "max drawdown <= 30%": m.get("max_drawdown", 1) <= 0.30,
    }
    combined = H.window(pit, COMBINED)["ret"]
    # per-observation Sharpe of the five daily-return trials (H-002a, H-002b, H-003, H-004, H-005)
    h005 = json.loads((Path(__file__).parent / "results/h005/summary.json").read_text())
    trial_sharpes = h005["per_obs_sharpe_of_trials"]
    stopped = sorted(
        {
            symbols[j]
            for i in np.flatnonzero(in_val)[:-2]
            for j in np.flatnonzero(universe[i])
            if np.isnan(a["open"][i + 2, j])
        }
    )
    report: dict[str, object] = {
        "hypothesis": "H-006",
        "period": "holding days 2025-07-01 -> 2026-07-30 (data to 2026-07-31; lockbox never loaded)",
        "universe": {
            "symbols_in_range": len(symbols),
            "symbols_in_universe_during_validation": int(universe[in_val].any(axis=0).sum()),
            "universe_members_that_stopped_trading": stopped,
        },
        "strategy": H4.summarise(pit, VALIDATION),
        "buy_and_hold": H4.summarise(base, VALIDATION),
        "strategy_monthly": monthly(pit, VALIDATION),
        "buy_and_hold_monthly": monthly(base, VALIDATION),
        "criteria": criteria,
        "decision": "ACCEPTED" if all(criteria.values()) else "REJECTED",
        "diagnostic_combined_2020_02_to_2026_07": {
            **H.metrics(combined),
            "psr_sharpe_above_0": round(purgedcv.probabilistic_sharpe_ratio(combined, 0.0), 4),
            "dsr_after_6_trials": round(
                purgedcv.deflated_sharpe_ratio(combined, N_TRIALS, float(np.var(trial_sharpes, ddof=1))), 4
            ),
        },
        "diagnostic_h004_window_same_run_incl_2025_06_30": H4.summarise(pit, H4.PERIOD),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
