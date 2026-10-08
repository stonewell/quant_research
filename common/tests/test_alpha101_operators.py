"""Unit tests for WorldQuant 101 operators in common/alpha101_operators.py.

100% offline: Uses synthetic data only.
"""

import numpy as np
import pandas as pd
import pytest

from common.alpha101_operators import (
    adv,
    cs_demean,
    cs_indneutralize,
    cs_rank,
    cs_scale,
    cs_zscore,
    signed_power,
    ternary,
    ts_argmax,
    ts_argmin,
    ts_corr,
    ts_cov,
    ts_decay_linear,
    ts_delay,
    ts_delta,
    ts_max,
    ts_mean,
    ts_min,
    ts_prod,
    ts_rank,
    ts_std,
    ts_sum,
    vwap_proxy,
)


@pytest.fixture
def sample_data():
    dates = pd.bdate_range("2023-01-01", periods=20)
    df1 = pd.DataFrame({
        "A": np.arange(1.0, 21.0),
        "B": np.arange(20.0, 0.0, -1.0),
        "C": [10.0] * 20,
    }, index=dates)
    df2 = pd.DataFrame({
        "A": np.arange(5.0, 25.0),
        "B": np.arange(1.0, 21.0),
        "C": [5.0] * 20,
    }, index=dates)
    return df1, df2


def test_time_series_primitives(sample_data):
    df1, _ = sample_data
    # delay
    delayed = ts_delay(df1, 1)
    assert np.isnan(delayed.iloc[0]["A"])
    assert delayed.iloc[1]["A"] == 1.0

    # delta
    diff = ts_delta(df1, 2)
    assert diff.iloc[2]["A"] == 2.0
    assert diff.iloc[2]["B"] == -2.0

    # min / max / sum / mean
    assert ts_min(df1, 5).iloc[4]["A"] == 1.0
    assert ts_max(df1, 5).iloc[4]["A"] == 5.0
    assert ts_sum(df1, 3).iloc[2]["A"] == 1.0 + 2.0 + 3.0
    assert ts_mean(df1, 3).iloc[2]["A"] == 2.0


def test_ts_rank_and_extrema(sample_data):
    df1, _ = sample_data
    # For monotonically increasing series A, ts_rank should be 1.0
    rank_a = ts_rank(df1, 5)
    assert rank_a.iloc[4]["A"] == 1.0
    # For monotonically decreasing series B, ts_rank should be lowest
    assert rank_a.iloc[4]["B"] == 0.2

    # ts_argmax
    argmax = ts_argmax(df1, 5)
    # in window of 5, max for A is at index 5 (today)
    assert argmax.iloc[4]["A"] == 5.0

    # ts_argmin
    argmin = ts_argmin(df1, 5)
    # min for A is at index 1
    assert argmin.iloc[4]["A"] == 1.0


def test_ts_decay_linear(sample_data):
    df1, _ = sample_data
    # Decay linear with d=3: weights [1, 2, 3] / 6
    decay = ts_decay_linear(df1, 3)
    # At index 2 for A: (1*1 + 2*2 + 3*3)/6 = (1+4+9)/6 = 14/6 = 2.3333
    assert np.isclose(decay.iloc[2]["A"], 14.0 / 6.0)
    # Constant C series should remain 10.0
    assert np.isclose(decay.iloc[5]["C"], 10.0)


def test_ts_corr_and_cov(sample_data):
    df1, df2 = sample_data
    # A in df1 and A in df2 are perfectly correlated
    corr = ts_corr(df1, df2, 5)
    assert np.isclose(corr.iloc[5]["A"], 1.0)
    # B in df1 and B in df2 are inversely correlated
    assert np.isclose(corr.iloc[5]["B"], -1.0)


def test_cross_sectional_operators(sample_data):
    df1, _ = sample_data
    # At index 0: A=1, B=20, C=10 -> ranks should be A=1/3, C=2/3, B=1.0
    ranked = cs_rank(df1)
    assert ranked.iloc[0]["A"] < ranked.iloc[0]["C"] < ranked.iloc[0]["B"]

    # cs_scale: sum of abs values should be target
    scaled = cs_scale(df1, a=2.0)
    assert np.isclose(scaled.abs().sum(axis=1).iloc[0], 2.0)

    # cs_demean: row mean should be 0.0
    demeaned = cs_demean(df1)
    assert np.isclose(demeaned.mean(axis=1).iloc[0], 0.0)

    # cs_zscore: row mean should be 0.0, std should be 1.0
    zscored = cs_zscore(df1)
    assert np.isclose(zscored.mean(axis=1).iloc[0], 0.0)
    assert np.isclose(zscored.std(axis=1).iloc[0], 1.0)


def test_industry_neutralize(sample_data):
    df1, _ = sample_data
    groups = {"A": "tech", "B": "tech", "C": "finance"}
    ind_neu = cs_indneutralize(df1, groups)
    # A and B in tech group should sum to 0 after demeaning within group
    assert np.isclose(ind_neu.iloc[0]["A"] + ind_neu.iloc[0]["B"], 0.0)
    # C alone in finance should demean to 0
    assert np.isclose(ind_neu.iloc[0]["C"], 0.0)


def test_signed_power_and_ternary(sample_data):
    df = pd.DataFrame({"A": [-4.0, 9.0], "B": [2.0, -1.0]})
    sp = signed_power(df, 0.5)
    assert sp.iloc[0]["A"] == -2.0
    assert sp.iloc[1]["A"] == 3.0

    cond = df > 0
    tern = ternary(cond, 100.0, -100.0)
    assert tern.iloc[0]["A"] == -100.0
    assert tern.iloc[1]["A"] == 100.0
