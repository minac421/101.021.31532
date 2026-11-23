"""Cross-checks of the adopted validation library (purgedcv, pinned) against independent formulas.

The library is young and single-maintainer (see docs/research/repo-due-diligence-2026-11.md), so
its key numbers are re-derived here from the original papers; a version bump that changes them
fails CI.
"""

import math
from itertools import combinations

import numpy as np
import pandas as pd
import pytest

purgedcv = pytest.importorskip("purgedcv")
stats = pytest.importorskip("scipy.stats")


def psr_reference(returns: np.ndarray, benchmark: float) -> float:
    """Bailey & López de Prado (2012), Eq. 7 — population std, raw (non-excess) kurtosis."""
    sr = returns.mean() / returns.std(ddof=0)
    g3 = stats.skew(returns)
    g4 = stats.kurtosis(returns, fisher=False)
    z = (sr - benchmark) * math.sqrt(len(returns) - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr**2)
    return float(stats.norm.cdf(z))


def expected_max_sharpe(n_trials: int, var_sharpe: float) -> float:
    """Bailey & López de Prado (2014): expected maximum Sharpe of n_trials under the null."""
    gamma = 0.5772156649015329
    return math.sqrt(var_sharpe) * (
        (1 - gamma) * stats.norm.ppf(1 - 1 / n_trials) + gamma * stats.norm.ppf(1 - 1 / (n_trials * math.e))
    )


@pytest.fixture
def returns() -> np.ndarray:
    rng = np.random.default_rng(7)
    return rng.standard_t(df=5, size=900) * 0.02 + 0.0012  # fat tails, small positive drift


def test_psr_matches_the_paper(returns: np.ndarray) -> None:
    for benchmark in (0.0, 0.02, 0.05):
        # agreement to 6 decimals (moment estimators differ only in floating-point order)
        assert purgedcv.probabilistic_sharpe_ratio(returns, benchmark) == pytest.approx(
            psr_reference(returns, benchmark), abs=1e-6
        )


def test_dsr_with_one_trial_is_psr_and_deflates_with_more_trials(returns: np.ndarray) -> None:
    psr0 = purgedcv.probabilistic_sharpe_ratio(returns, 0.0)
    assert purgedcv.deflated_sharpe_ratio(returns, 1, 0.001) == pytest.approx(psr0)
    values = [purgedcv.deflated_sharpe_ratio(returns, n, 0.001) for n in (2, 10, 100, 1000)]
    assert values == sorted(values, reverse=True)
    benchmark = expected_max_sharpe(10, 0.001)
    assert purgedcv.deflated_sharpe_ratio(returns, 10, 0.001) == pytest.approx(
        psr_reference(returns, benchmark), abs=1e-6
    )


def test_minimum_track_record_length_is_consistent_with_psr() -> None:
    rng = np.random.default_rng(3)
    returns = rng.standard_t(df=5, size=900) * 0.02 + 0.004
    sr = returns.mean() / returns.std(ddof=0)
    assert sr > 0
    g3, g4 = stats.skew(returns), stats.kurtosis(returns, fisher=False)
    n_min = purgedcv.min_track_record_length(sr, 0.0, 0.05, g3, g4)
    # at exactly n_min observations, PSR(0) with the same moments equals 1 - alpha
    z = sr * math.sqrt(n_min - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr**2)
    assert stats.norm.cdf(z) == pytest.approx(0.95, abs=1e-3)
    # a negative observed Sharpe can never be confirmed as positive: the required length is infinite
    assert math.isinf(purgedcv.min_track_record_length(-abs(sr), 0.0, 0.05, g3, g4))


def test_pbo_flags_pure_noise_and_spares_a_real_edge() -> None:
    # layout is (n_configs, n_obs): one row per candidate configuration
    pbos = []
    for seed in range(8):  # single PBO estimates on noise scatter widely; their mean is ~0.5
        noise = np.random.default_rng(seed).normal(0, 0.01, size=(20, 1000))
        pbos.append(purgedcv.probability_of_backtest_overfitting(noise, n_splits=10).pbo)
    assert 0.35 <= float(np.mean(pbos)) <= 0.65
    noise = np.random.default_rng(11).normal(0, 0.01, size=(20, 2000))
    edge = noise.copy()
    edge[0] += 0.004  # one configuration is genuinely better in every period
    assert purgedcv.probability_of_backtest_overfitting(edge, n_splits=10).pbo < 0.1


def test_cpcv_purges_overlapping_labels() -> None:
    n = 600
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    horizon = pd.Timedelta(days=5)  # each label looks 5 days ahead
    cv = purgedcv.CombinatorialPurgedCV(
        n_splits=6,
        n_test_groups=2,
        prediction_times=idx,
        evaluation_times=idx + horizon,
        embargo_observations=3,
    )
    splits = list(cv.split(np.zeros((n, 1))))
    assert len(splits) == len(list(combinations(range(6), 2)))  # C(6, 2) = 15
    starts = idx.values.astype("datetime64[ns]").astype(np.int64)
    ends = (idx + horizon).values.astype("datetime64[ns]").astype(np.int64)
    for train, test in splits:
        assert set(train).isdisjoint(test)
        # intervals are half-open: no training label may truly overlap a test label (no leakage)
        overlap = (starts[train][:, None] < ends[test][None, :]) & (
            ends[train][:, None] > starts[test][None, :]
        )
        assert not overlap.any()
    # our convention: purge one extra bar so labels do not even touch at the boundary
    strict_cv = purgedcv.CombinatorialPurgedCV(
        n_splits=6,
        n_test_groups=2,
        prediction_times=idx,
        evaluation_times=idx + horizon,
        purge_horizon=pd.Timedelta(days=1),
        embargo_observations=3,
    )
    for train, test in strict_cv.split(np.zeros((n, 1))):
        touch = (starts[train][:, None] <= ends[test][None, :]) & (
            ends[train][:, None] >= starts[test][None, :]
        )
        assert not touch.any()
