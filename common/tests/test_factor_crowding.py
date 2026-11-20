"""Offline unit tests for advisory-only factor crowding monitor (Track MS, MS5).

All tests run 100% offline on seeded synthetic panels.
"""

from __future__ import annotations

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd

from common.factor_crowding import (
    CROWDING_STATUS_INSUFFICIENT,
    CROWDING_STATUS_NORMAL,
    CROWDING_STATUS_QUAKE,
    compute_factor_crowding_metrics,
)


def test_crowding_detects_quant_quake_risk_on_correlated_top_group_and_turnover_spike():
    rng = np.random.default_rng(42)
    t_len, n_assets = 70, 30
    dates = pd.bdate_range("2025-01-02", periods=t_len)
    cols = [f"S{i:02d}" for i in range(n_assets)]

    # Factor selects S24..S29 as the Top-20% (6 stocks)
    factor_df = pd.DataFrame(
        np.tile(np.arange(n_assets, dtype=float), (t_len, 1)),
        index=dates,
        columns=cols,
    )

    # First 50 days: moderate independent noise for all stocks
    rets = rng.normal(0.0, 0.02, size=(t_len, n_assets))
    # Last 20 days: top-6 stocks move in lockstep (~0.95 corr) with tiny cross-sectional dispersion
    common_shock = rng.normal(0.0, 0.03, size=20)
    for j in range(24, 30):
        rets[-20:, j] = common_shock + rng.normal(0.0, 0.002, size=20)
    returns_df = pd.DataFrame(rets, index=dates, columns=cols)

    # Top-6 stocks absorb ~55% of total turnover
    turn = np.ones((t_len, n_assets), dtype=float)
    turn[:, 24:30] = 5.0  # 30 / (24 + 30) = 55.6%
    turnover_df = pd.DataFrame(turn, index=dates, columns=cols)

    res = compute_factor_crowding_metrics(factor_df, returns_df, turnover_df)
    assert res["status"] == CROWDING_STATUS_QUAKE
    assert res["available"] is True
    assert res["advisory"] is True
    assert res["gates_orders"] is False
    assert res["top_pairwise_corr"] > 0.75
    assert res["top_turnover_concentration"] > 0.40
    assert res["dispersion_compression_ratio"] < 0.55
    json.dumps(res, allow_nan=False)


def test_crowding_normal_on_independent_diversified_universe():
    rng = np.random.default_rng(7)
    t_len, n_assets = 70, 30
    dates = pd.bdate_range("2025-01-02", periods=t_len)
    cols = [f"S{i:02d}" for i in range(n_assets)]

    factor_df = pd.DataFrame(rng.normal(size=(t_len, n_assets)), index=dates, columns=cols)
    returns_df = pd.DataFrame(rng.normal(0.0, 0.02, size=(t_len, n_assets)), index=dates, columns=cols)
    turnover_df = pd.DataFrame(np.ones((t_len, n_assets)), index=dates, columns=cols)

    res = compute_factor_crowding_metrics(factor_df, returns_df, turnover_df)
    assert res["status"] == CROWDING_STATUS_NORMAL
    assert res["available"] is True
    assert res["advisory"] is True
    assert res["gates_orders"] is False
    json.dumps(res, allow_nan=False)


def test_crowding_insufficient_history_on_n_lt_15_or_short_window_and_json_native():
    rng = np.random.default_rng(13)

    # Case 1: N = 10 < 15
    dates_long = pd.bdate_range("2025-01-02", periods=40)
    cols10 = [f"S{i:02d}" for i in range(10)]
    f10 = pd.DataFrame(rng.normal(size=(40, 10)), index=dates_long, columns=cols10)
    r10 = pd.DataFrame(rng.normal(size=(40, 10)), index=dates_long, columns=cols10)
    res_n = compute_factor_crowding_metrics(f10, r10)
    assert res_n["status"] == CROWDING_STATUS_INSUFFICIENT
    assert res_n["available"] is False
    assert res_n["gates_orders"] is False
    assert res_n["advisory"] is True
    json.dumps(res_n, allow_nan=False)

    # Case 2: T = 5 < corr_window=20
    dates_short = pd.bdate_range("2025-01-02", periods=5)
    cols20 = [f"S{i:02d}" for i in range(20)]
    f_short = pd.DataFrame(rng.normal(size=(5, 20)), index=dates_short, columns=cols20)
    r_short = pd.DataFrame(rng.normal(size=(5, 20)), index=dates_short, columns=cols20)
    res_t = compute_factor_crowding_metrics(f_short, r_short)
    assert res_t["status"] == CROWDING_STATUS_INSUFFICIENT
    assert res_t["available"] is False
    assert res_t["gates_orders"] is False
    json.dumps(res_t, allow_nan=False)

    # Case 3: Empty / None
    res_empty = compute_factor_crowding_metrics(None, pd.DataFrame())
    assert res_empty["status"] == CROWDING_STATUS_INSUFFICIENT
    assert res_empty["available"] is False
    assert res_empty["gates_orders"] is False
    json.dumps(res_empty, allow_nan=False)
