"""H-003: H-002b rules and code, unchanged, on unused 2020-01-01 -> 2022-12-31 data.

Pre-registered in docs/research/H-003-tsmom-replication-2020-2022.md (committed before the
data was downloaded). Reuses research/h002_tsmom.py verbatim; only the data range differs.

Usage:
    uv run --group research python research/h003_tsmom_2020_2022.py --archive data/archive
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import numpy as np
import purgedcv

N_TRIALS = 4  # H-001, H-002a, H-002b, H-003


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", type=Path, default=Path("data/archive"))
    ap.add_argument("--out", type=Path, default=Path("research/results/h003/summary.json"))
    args = ap.parse_args()
    h = _load("h002_tsmom")
    start, end = h._day("2020-01-01"), h._day("2023-01-01")
    years = {y: (h._day(f"{y}-01-01"), h._day(f"{int(y) + 1}-01-01")) for y in ("2020", "2021", "2022")}

    panel = h.build_panel(args.archive, start, end)
    sim, base = h.simulate(*panel, "b"), h.simulate(*panel, "baseline")
    test, btest = h.window(sim, (start, end)), h.window(base, (start, end))
    m, bm = h.metrics(test["ret"]), h.metrics(btest["ret"])
    by_year = {y: h.metrics(h.window(sim, p)["ret"]) for y, p in years.items()}
    base_by_year = {y: h.metrics(h.window(base, p)["ret"]) for y, p in years.items()}
    loss_2022 = max(0.0, -by_year["2022"]["total_return"])
    base_loss_2022 = max(0.0, -base_by_year["2022"]["total_return"])
    criteria = {
        "Sharpe >= 0.8": m["sharpe"] >= 0.8,
        "t-stat >= 2.0": m["t_stat"] >= 2.0,
        "Sharpe >= buy-and-hold": m["sharpe"] >= bm["sharpe"],
        "max drawdown <= 30%": m["max_drawdown"] <= 0.30,
        "2022 loss <= half of buy-and-hold loss": loss_2022 <= 0.5 * base_loss_2022,
    }
    report: dict[str, object] = {
        "hypothesis": "H-003",
        "period": "2020-01-01 -> 2022-12-31 (after 31-day warm-up)",
        "symbols_with_data_first_day": int(np.sum(~np.isnan(panel[2][0]))),
        "strategy": m,
        "strategy_by_year": by_year,
        "buy_and_hold": bm,
        "buy_and_hold_by_year": base_by_year,
        "avg_gross_exposure": round(float(test["gross"].mean()), 3),
        "avg_daily_turnover": round(float(test["turnover"].mean()), 4),
        "funding_drag_ann": round(float(test["funding"].mean() * 365), 4),
        "criteria": criteria,
        "decision": "ACCEPTED" if all(criteria.values()) else "REJECTED",
    }
    if report["decision"] == "ACCEPTED":
        dev_panel = h.build_panel(args.archive)
        dev_b = h.window(h.simulate(*dev_panel, "b"), h.DEV)["ret"]
        dev_a = h.window(h.simulate(*dev_panel, "a"), h.DEV)["ret"]
        combined = np.concatenate([test["ret"], dev_b])
        sharpes = [float(r.mean() / r.std(ddof=0)) for r in (dev_a, dev_b, test["ret"])]
        report["combined_2020_2025H1"] = {
            "days": len(combined),
            "metrics": h.metrics(combined),
            "psr_sharpe_above_0": round(purgedcv.probabilistic_sharpe_ratio(combined, 0.0), 4),
            "dsr_after_4_trials": round(
                purgedcv.deflated_sharpe_ratio(combined, N_TRIALS, float(np.var(sharpes, ddof=1))), 4
            ),
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
