"""Unit tests for the offline look-ahead and cutoff equivalence harness (Track MS, MS12).

All tests run 100% offline on seeded synthetic data.
"""

from __future__ import annotations

import os
import sys
import warnings

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.cross_sectional import (
    cs_lowdin_orthogonalize,
    cs_mad_winsorize,
    cs_neutralize_ols,
    cs_rank_gaussian_or_uniform,
)
from common.intraday_features import (
    IntradayFeatureConfig,
    IntradayIncompleteWarning,
    _parse_time_cutoff,
    compute_daily_intraday_features,
)
from common.lookahead_checks import (
    LookaheadViolation,
    assert_cutoff_truncation_equivalent,
    assert_future_append_invariant,
)


def _make_daily_panel(t_len: int = 30, n_cols: int = 12, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-02", periods=t_len)
    cols = [f"S{i:02d}" for i in range(n_cols)]
    return pd.DataFrame(rng.normal(0.0, 1.0, size=(t_len, n_cols)), index=dates, columns=cols)


def _make_5m_bars(n_days: int = 3, seed: int = 7) -> pd.DataFrame:
    """Build synthetic A-share 5m OHLCV bars (48 bars/day: 09:35-11:30, 13:05-15:00)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-03-03", periods=n_days)
    timestamps: list[pd.Timestamp] = []
    for d in dates:
        morning = pd.date_range(f"{d.date()} 09:35", f"{d.date()} 11:30", freq="5min")
        afternoon = pd.date_range(f"{d.date()} 13:05", f"{d.date()} 15:00", freq="5min")
        timestamps.extend(morning)
        timestamps.extend(afternoon)

    n = len(timestamps)
    ret = rng.normal(0.0, 0.002, size=n)
    close = 100.0 * np.exp(np.cumsum(ret))
    open_ = np.r_[100.0, close[:-1]]
    high = np.maximum(open_, close) * (1.0 + rng.uniform(0.0, 0.002, size=n))
    low = np.minimum(open_, close) * (1.0 - rng.uniform(0.0, 0.002, size=n))
    vol = rng.integers(1000, 50000, size=n, dtype=np.int64)

    return pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": vol,
        },
        index=pd.DatetimeIndex(timestamps),
    )


# =============================================================================
# 1. assert_future_append_invariant
# =============================================================================


class TestFutureAppendInvariant:
    def test_causal_rolling_passes_int_label_and_list_cuts(self):
        df = _make_daily_panel(25, 6)
        fn = lambda d: d.rolling(5).mean()

        assert_future_append_invariant(fn, df, 10)
        assert_future_append_invariant(fn, df, df.index[12])
        assert_future_append_invariant(fn, df, [5, 10, df.index[18]])

    def test_all_ms1_primitives_pass_future_append(self):
        df = _make_daily_panel(20, 40, seed=11)
        exp = _make_daily_panel(20, 40, seed=12)
        f2 = _make_daily_panel(20, 40, seed=13)
        cuts = [5, 12]

        assert_future_append_invariant(cs_mad_winsorize, df, cuts)
        assert_future_append_invariant(
            lambda d: cs_rank_gaussian_or_uniform(d, method="gaussian"), df, cuts
        )
        assert_future_append_invariant(
            lambda d: cs_neutralize_ols(d, {"e": exp.loc[d.index]}), df, cuts
        )
        assert_future_append_invariant(
            lambda d: cs_lowdin_orthogonalize({"a": d, "b": f2.loc[d.index]})["a"],
            df,
            cuts,
        )

    @pytest.mark.parametrize(
        "leaky_fn",
        [
            lambda d: d.shift(-1),
            lambda d: d.rolling(3, center=True).mean(),
            lambda d: (d - d.mean(axis=0)) / d.std(axis=0),
            lambda d: d.where(pd.Series(d.index != d.index[10], index=d.index)).bfill(),
            lambda d: d.iloc[:-1],  # index leak: last history row dropped on truncated input
        ],
        ids=["shift_neg1", "rolling_center", "full_sample_zscore", "bfill", "index_leak"],
    )
    def test_leaky_transforms_raise_lookahead_violation(self, leaky_fn):
        df = _make_daily_panel(20, 5, seed=99)
        with pytest.raises(LookaheadViolation):
            assert_future_append_invariant(leaky_fn, df, 10)

    def test_value_error_input_not_mutated_and_atol_passthrough(self):
        df = _make_daily_panel(15, 4)
        snapshot = df.copy(deep=True)

        # No future rows (cut at last index) -> ValueError
        with pytest.raises(ValueError):
            assert_future_append_invariant(lambda d: d, df, len(df) - 1)
        # Negative cut -> ValueError
        with pytest.raises(ValueError):
            assert_future_append_invariant(lambda d: d, df, -1)

        # Input must not be mutated even if fn mutates its argument
        def mutating_causal(d: pd.DataFrame) -> pd.DataFrame:
            d.iloc[0, 0] = 999.0
            return d

        assert_future_append_invariant(mutating_causal, df, 7)
        pd.testing.assert_frame_equal(df, snapshot)

        # atol pass-through
        def tiny_drift(d: pd.DataFrame) -> pd.DataFrame:
            return d + 1e-7 * len(d)

        with pytest.raises(LookaheadViolation):
            assert_future_append_invariant(tiny_drift, df, 7, atol=0.0)
        assert_future_append_invariant(tiny_drift, df, 7, atol=1e-4)


# =============================================================================
# 2. assert_cutoff_truncation_equivalent
# =============================================================================


class TestCutoffTruncationEquivalent:
    def test_causal_per_day_close_passes_both_modes(self):
        bars = _make_5m_bars(3)

        def causal_last(b: pd.DataFrame, cutoff: str) -> pd.Series:
            c = _parse_time_cutoff(cutoff)
            sub = b.loc[b.index.time <= c]
            return sub["Close"].groupby(sub.index.normalize()).last()

        assert_cutoff_truncation_equivalent(causal_last, bars, "14:15", method="truncate")
        assert_cutoff_truncation_equivalent(causal_last, bars, "14:15", method="perturb")

    @pytest.mark.parametrize("method", ["truncate", "perturb"])
    @pytest.mark.parametrize(
        "leaky_fn",
        [
            lambda b, c: b["Close"].groupby(b.index.normalize()).last(),
            lambda b, c: (
                b.loc[b.index.time <= _parse_time_cutoff(c), "Volume"]
                .groupby(lambda ts: ts.normalize())
                .sum()
                / b["Volume"].groupby(b.index.normalize()).sum()
            ),
            lambda b, c: b["Close"].groupby(b.index.normalize()).transform("last"),
        ],
        ids=["uncut_last", "full_day_vol_denom", "bar_level_transform_last"],
    )
    def test_leaks_raise_in_both_modes(self, method: str, leaky_fn):
        bars = _make_5m_bars(3)
        with pytest.raises(LookaheadViolation):
            assert_cutoff_truncation_equivalent(leaky_fn, bars, "14:15", method=method)

    def test_bar_level_causal_cumsum_passes_truncate(self):
        bars = _make_5m_bars(3)
        # Ignores cutoff_time but is strictly causal bar-by-bar; default project slices <= c.
        fn = lambda b, c: b["Volume"].groupby(b.index.normalize()).cumsum()
        assert_cutoff_truncation_equivalent(fn, bars, "14:15", method="truncate")

    @pytest.mark.parametrize("cutoff", ["14:15", "14:00", "11:30"])
    def test_compute_daily_intraday_features_passes_perturb(self, cutoff: str):
        bars = _make_5m_bars(3)
        fn = lambda b, c: compute_daily_intraday_features(
            b, IntradayFeatureConfig(cutoff_time=c, enable_chan_5m=False)
        )
        assert_cutoff_truncation_equivalent(fn, bars, cutoff, method="perturb")

    def test_compute_daily_intraday_features_truncate_false_failure_pinned(self):
        bars = _make_5m_bars(3)
        fn = lambda b, c: compute_daily_intraday_features(
            b, IntradayFeatureConfig(cutoff_time=c, enable_chan_5m=False)
        )
        with pytest.warns(IntradayIncompleteWarning):
            with pytest.raises(LookaheadViolation):
                assert_cutoff_truncation_equivalent(fn, bars, "14:15", method="truncate")

    def test_validation_errors_and_int64_volume_no_futurewarning(self):
        bars = _make_5m_bars(2)
        fn = lambda b, c: b["Close"].groupby(b.index.normalize()).last()

        # "15:00" resolves to None -> ValueError
        with pytest.raises(ValueError):
            assert_cutoff_truncation_equivalent(fn, bars, "15:00", method="perturb")

        # No post-cutoff rows -> ValueError
        morning_only = bars.loc[bars.index.time <= _parse_time_cutoff("11:30")]
        with pytest.raises(ValueError):
            assert_cutoff_truncation_equivalent(fn, morning_only, "11:30", method="perturb")

        # Non-DatetimeIndex -> TypeError
        bad_df = bars.reset_index(drop=True)
        with pytest.raises(TypeError):
            assert_cutoff_truncation_equivalent(fn, bad_df, "14:15", method="perturb")

        # int64 Volume column must stay int64 without FutureWarning
        assert bars["Volume"].dtype == np.int64
        seen_dtypes: list[np.dtype] = []

        def check_dtype_causal(b: pd.DataFrame, cutoff: str) -> pd.Series:
            seen_dtypes.append(b["Volume"].dtype)
            c = _parse_time_cutoff(cutoff)
            sub = b.loc[b.index.time <= c]
            return sub["Volume"].groupby(sub.index.normalize()).sum()

        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            assert_cutoff_truncation_equivalent(
                check_dtype_causal, bars, "14:15", method="perturb"
            )
        assert all(dt == np.int64 for dt in seen_dtypes)
