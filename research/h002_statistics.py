"""Statistical diagnostics for H-002 on the development period (no new trial, no decision change).

Uses the adopted, cross-checked validation library (purgedcv, pinned) to answer:
* PSR — probability that the true Sharpe is above zero given non-normal daily returns;
* DSR — the same after deflating for the number of trials run on this data (3 so far);
* MinTRL — how many days of track record are needed for 95 % confidence that Sharpe > 0.

Usage:
    uv run --group research python research/h002_statistics.py --archive data/archive
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import purgedcv
from scipy import stats

N_TRIALS = 3  # H-001, H-002a, H-002b on this dataset (docs/research/README.md)


def _load_h002():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("h002_tsmom", Path(__file__).with_name("h002_tsmom.py"))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", type=Path, default=Path("data/archive"))
    ap.add_argument("--out", type=Path, default=Path("research/results/h002/statistics.json"))
    args = ap.parse_args()
    h = _load_h002()
    panel = h.build_panel(args.archive)
    dev = {v: h.window(h.simulate(*panel, v), h.DEV)["ret"] for v in ("a", "b")}
    per_obs_sharpes = [float(r.mean() / r.std(ddof=0)) for r in dev.values()]
    var_sharpe = float(np.var(per_obs_sharpes, ddof=1))
    report = {"n_trials": N_TRIALS, "var_sharpe_per_obs_across_h002_variants": var_sharpe}
    for variant, r in dev.items():
        sr = float(r.mean() / r.std(ddof=0))
        skew, kurt = float(stats.skew(r)), float(stats.kurtosis(r, fisher=False))
        min_trl = purgedcv.min_track_record_length(sr, 0.0, 0.05, skew, kurt)
        report[f"H-002{variant}"] = {
            "days": len(r),
            "sharpe_annualised": round(sr * math.sqrt(365), 3),
            "skew": round(skew, 3),
            "kurtosis": round(kurt, 3),
            "psr_sharpe_above_0": round(purgedcv.probabilistic_sharpe_ratio(r, 0.0), 4),
            "dsr_after_3_trials": round(purgedcv.deflated_sharpe_ratio(r, N_TRIALS, var_sharpe), 4),
            "min_track_record_days_95pct": None if math.isinf(min_trl) else round(min_trl),
            "min_track_record_years_95pct": None if math.isinf(min_trl) else round(min_trl / 365, 2),
        }
    args.out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
