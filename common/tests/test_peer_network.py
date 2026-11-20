"""Offline unit tests for dynamic peer network and momentum spillover (Track MS, MS3).

All tests run 100% offline on seeded synthetic return panels.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.lookahead_checks import assert_future_append_invariant
from common.peer_network import (
    build_lead_lag_adjacency,
    build_partial_corr_adjacency,
    compute_peer_chip_interaction_alpha,
    compute_peer_momentum_spillover,
)


def _make_dates(n: int) -> pd.DatetimeIndex:
    return pd.bdate_range("2025-01-02", periods=n)


def test_partial_corr_adjacency_properties():
    rng = np.random.default_rng(7)
    t_len = 60
    f1 = rng.normal(0.0, 0.02, size=t_len)
    f2 = rng.normal(0.0, 0.02, size=t_len)
    df = pd.DataFrame(
        {
            "A": f1 + rng.normal(0.0, 0.005, size=t_len),
            "B": f1 + rng.normal(0.0, 0.005, size=t_len),
            "C": f1 + rng.normal(0.0, 0.005, size=t_len),
            "D": f2 + rng.normal(0.0, 0.005, size=t_len),
            "E": f2 + rng.normal(0.0, 0.005, size=t_len),
            "ISO": np.zeros(t_len),  # constant isolated node
        },
        index=_make_dates(t_len),
    )

    adj = build_partial_corr_adjacency(df, top_k=2, positive_only=True, min_obs=10)
    np.testing.assert_allclose(np.diag(adj.to_numpy()), 0.0, atol=1e-12)
    assert (adj.to_numpy() >= 0.0).all()
    assert adj.loc["ISO"].sum() == 0.0

    for sym in ["A", "B", "C", "D", "E"]:
        row = adj.loc[sym]
        assert int((row > 0.0).sum()) <= 2
        if row.sum() > 0.0:
            assert row.sum() == pytest.approx(1.0, abs=1e-10)


def test_partial_corr_near_singular_duplicated_columns_safe():
    rng = np.random.default_rng(13)
    base = rng.normal(0.0, 0.02, size=30)
    df = pd.DataFrame(
        {
            "S1": base,
            "S2": base.copy(),  # exact duplicate
            "S3": base * 2.0,   # exact collinear
            "S4": rng.normal(0.0, 0.02, size=30),
        },
        index=_make_dates(30),
    )
    adj = build_partial_corr_adjacency(df, top_k=2)
    assert np.isfinite(adj.to_numpy()).all()
    np.testing.assert_allclose(np.diag(adj.to_numpy()), 0.0, atol=1e-12)


def test_lead_lag_adjacency_identifies_true_leader():
    rng = np.random.default_rng(21)
    t_len = 80
    leader = rng.normal(0.0, 0.02, size=t_len)
    follower = np.zeros(t_len)
    follower[1:] = 0.85 * leader[:-1] + rng.normal(0.0, 0.004, size=t_len - 1)
    noise1 = rng.normal(0.0, 0.02, size=t_len)
    noise2 = rng.normal(0.0, 0.02, size=t_len)

    df = pd.DataFrame(
        {"LEADER": leader, "FOLLOWER": follower, "N1": noise1, "N2": noise2},
        index=_make_dates(t_len),
    )
    adj = build_lead_lag_adjacency(df, top_k=2, positive_only=True, min_obs=15)
    assert adj.loc["FOLLOWER", "LEADER"] > 0.5
    assert adj.loc["FOLLOWER", "LEADER"] == adj.loc["FOLLOWER"].max()


def test_peer_momentum_spillover_positive_on_laggard_after_peer_rally():
    rng = np.random.default_rng(33)
    t_len = 30
    common = rng.normal(0.0, 0.015, size=t_len)
    df = pd.DataFrame(
        {
            "A": common + rng.normal(0.0, 0.002, size=t_len),
            "B": common + rng.normal(0.0, 0.002, size=t_len),
            "C": common + rng.normal(0.0, 0.002, size=t_len),
            "ISO": np.zeros(t_len),
        },
        index=_make_dates(t_len),
    )
    # On the last day, A & B rally +5% while laggard C stays flat 0%
    df.iloc[-1, 0] = 0.05
    df.iloc[-1, 1] = 0.05
    df.iloc[-1, 2] = 0.0

    pm = compute_peer_momentum_spillover(df, window=20, top_k=2, adjacency_method="partial_corr")
    # Warmup rows strictly NaN
    assert pm.iloc[:19].isna().all().all()
    # Isolated node stays NaN (never 0.0)
    assert pm["ISO"].isna().all()
    # Laggard C has positive peer spillover; leaders A/B have negative spillover
    assert pm.iloc[-1]["C"] > 0.02
    assert pm.iloc[-1]["A"] < 0.0


def test_peer_chip_interaction_alpha_formula_and_nan_preservation():
    dates = _make_dates(10)
    cols = ["A", "B", "C", "D"]
    peer_mom = pd.DataFrame(
        np.tile(np.array([0.05, 0.02, -0.01, np.nan]), (10, 1)),
        index=dates,
        columns=cols,
    )
    # Narrowing chip spread over time -> ts_delta < 0 -> -ts_delta > 0
    chip_spread = pd.DataFrame(
        np.column_stack([np.linspace(0.30, 0.10, 10) for _ in range(4)]),
        index=dates,
        columns=cols,
    )
    alpha = compute_peer_chip_interaction_alpha(peer_mom, chip_spread, delta_window=5)
    assert alpha.iloc[:5].isna().all().all()
    assert alpha["D"].isna().all()  # NaN preserved, never 0.5
    assert alpha.iloc[-1]["A"] > alpha.iloc[-1]["B"] > 0.0


def test_peer_network_causality_via_ms12_harness():
    rng = np.random.default_rng(55)
    t_len = 35
    f = rng.normal(0.0, 0.015, size=t_len)
    rets = pd.DataFrame(
        {
            f"S{i}": f + rng.normal(0.0, 0.005, size=t_len)
            for i in range(5)
        },
        index=_make_dates(t_len),
    )
    chip = pd.DataFrame(
        rng.uniform(0.05, 0.30, size=(t_len, 5)),
        index=rets.index,
        columns=rets.columns,
    )

    assert_future_append_invariant(
        lambda d: compute_peer_momentum_spillover(d, window=15, top_k=2, adjacency_method="partial_corr"),
        rets,
        [20, 28],
    )
    assert_future_append_invariant(
        lambda d: compute_peer_momentum_spillover(d, window=15, top_k=2, adjacency_method="lead_lag"),
        rets,
        [20, 28],
    )
    assert_future_append_invariant(
        lambda d: compute_peer_chip_interaction_alpha(d, chip.loc[d.index], delta_window=5),
        rets,
        [15, 25],
    )
