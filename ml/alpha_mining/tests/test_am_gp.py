"""Unit tests for amine/gp.py typed Genetic Programming factor miner.

100% offline: uses synthetic OHLCV panels only.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_AM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _AM_ROOT not in sys.path:
    sys.path.insert(0, _AM_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd

from amine.config import GPConfig
from amine.fitness import compute_pairwise_abs_rank_corr, evaluate_factor_fitness
from amine.gp import (
    GPFactorMiner,
    GPNode,
    build_terminals,
    evaluate_node,
)
from amine.labels import build_forward_labels


def _make_synthetic_ohlcv(n_dates: int = 120, n_syms: int = 10, seed: int = 42):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2021-01-04", periods=n_dates)
    cols = [f"S{i}" for i in range(n_syms)]
    rets = rng.normal(0.0004, 0.018, size=(n_dates, n_syms))
    close = pd.DataFrame(100.0 * np.exp(np.cumsum(rets, axis=0)), index=dates, columns=cols)
    open_ = close * (1.0 + rng.normal(0.0, 0.004, size=(n_dates, n_syms)))
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0.0, 0.005, size=(n_dates, n_syms))))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0.0, 0.005, size=(n_dates, n_syms))))
    volume = pd.DataFrame(
        rng.uniform(1e5, 5e6, size=(n_dates, n_syms)), index=dates, columns=cols
    )
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def test_gp_node_prefix_invariance():
    ohlcv = _make_synthetic_ohlcv(n_dates=80, n_syms=6, seed=10)
    # Build a multi-level causal tree combining ts_* and cs_* operators
    tree = GPNode(
        op="cs_zscore",
        children=(
            GPNode(
                op="ts_corr",
                children=(
                    GPNode(op="cs_rank", children=(GPNode(op="term", value="close"),)),
                    GPNode(op="ts_delta", children=(GPNode(op="term", value="volume"), GPNode(op="win", value=5))),
                    GPNode(op="win", value=10),
                ),
            ),
        ),
    )

    t_full, m_full = build_terminals(ohlcv)
    out_full = evaluate_node(tree, t_full, m_full)

    cutoff = 50
    ohlcv_pert = {k: v.copy() for k, v in ohlcv.items()}
    for k in ohlcv_pert:
        ohlcv_pert[k].iloc[cutoff:] *= 3.7

    t_pert, m_pert = build_terminals(ohlcv_pert)
    out_pert = evaluate_node(tree, t_pert, m_pert)

    # All dates strictly before cutoff must be bit-for-bit identical
    pd.testing.assert_frame_equal(out_full.iloc[:cutoff], out_pert.iloc[:cutoff])


def test_gp_seed_determinism():
    ohlcv = _make_synthetic_ohlcv(n_dates=90, n_syms=8, seed=13)
    labels = build_forward_labels(ohlcv["close"], holding_period=5, exec_lag=1)
    cfg = GPConfig(population_size=16, n_generations=2, tournament_size=3, top_k=3, seed=99)

    res1 = GPFactorMiner(cfg).mine(ohlcv, labels)
    res2 = GPFactorMiner(cfg).mine(ohlcv, labels)

    assert [f.expression for f in res1] == [f.expression for f in res2]
    assert [f.sign for f in res1] == [f.sign for f in res2]
    for f1, f2 in zip(res1, res2):
        assert np.isclose(f1.fitness, f2.fitness, atol=1e-12)


def test_gp_per_node_nan_and_inf_masking():
    ohlcv = _make_synthetic_ohlcv(n_dates=40, n_syms=6, seed=21)
    # Mark S0 untradable on rows 10..14 (NaN close) and S1 constant on rows 0..15 (ts_corr -> inf/0)
    ohlcv["close"].iloc[10:15, 0] = np.nan
    ohlcv["close"].iloc[:15, 1] = 100.0
    ohlcv["volume"].iloc[:15, 1] = 1000.0

    terminals, avail = build_terminals(ohlcv)

    # cs_rank normally fills NaN with 0.5; evaluate_node must re-mask untradable cells to NaN
    rank_node = GPNode(op="cs_rank", children=(GPNode(op="term", value="close"),))
    ranked = evaluate_node(rank_node, terminals, avail)
    assert ranked.iloc[10:15, 0].isna().all()
    assert not np.isinf(ranked.to_numpy(dtype=float)).any()

    # Division by zero / constant ts_corr must produce NaN, never ±inf
    zero_div = GPNode(
        op="div",
        children=(
            GPNode(op="term", value="close"),
            GPNode(op="sub", children=(GPNode(op="term", value="close"), GPNode(op="term", value="close"))),
        ),
    )
    div_out = evaluate_node(zero_div, terminals, avail)
    assert not np.isinf(div_out.to_numpy(dtype=float)).any()
    assert div_out.isna().all().all()


def test_gp_recovers_planted_factor():
    ohlcv = _make_synthetic_ohlcv(n_dates=110, n_syms=12, seed=55)
    # Plant a clean forward-return signal driven directly by 5-day price change (ts_delta(close, 5))
    planted = ohlcv["close"].diff(5)
    rng = np.random.default_rng(56)
    noise = pd.DataFrame(
        rng.normal(0.0, 0.05, size=planted.shape),
        index=planted.index,
        columns=planted.columns,
    )
    # Standardize planted cross-sectionally so forward returns are strongly aligned with it
    planted_z = planted.sub(planted.mean(axis=1), axis=0).div(planted.std(axis=1), axis=0)
    planted_labels = 0.04 * planted_z + 0.005 * noise
    planted_labels.iloc[-6:] = np.nan

    cfg = GPConfig(
        population_size=40,
        n_generations=4,
        tournament_size=5,
        top_k=5,
        windows=(3, 5, 10, 20),
        seed=7,
    )
    selected = GPFactorMiner(cfg).mine(ohlcv, planted_labels)
    assert len(selected) >= 1

    best_corr = max(
        compute_pairwise_abs_rank_corr(f.panel, planted, min_assets=3)
        for f in selected
    )
    assert best_corr > 0.5, f"Expected GP to recover planted factor (|corr| > 0.5), got {best_corr:.3f}"


def test_gp_rejects_degenerate_constant_and_terminal_only():
    ohlcv = _make_synthetic_ohlcv(n_dates=50, n_syms=6, seed=3)
    labels = build_forward_labels(ohlcv["close"], holding_period=5, exec_lag=1)
    _, avail = build_terminals(ohlcv)

    # Constant panel rejected with -inf
    const_df = pd.DataFrame(5.0, index=ohlcv["close"].index, columns=ohlcv["close"].columns)
    res_const = evaluate_factor_fitness(
        const_df, labels, node_count=3, availability_mask=avail
    )
    assert res_const.rejected is True
    assert res_const.fitness == -np.inf

    # Mostly-NaN panel rejected with -inf
    nan_df = pd.DataFrame(np.nan, index=ohlcv["close"].index, columns=ohlcv["close"].columns)
    res_nan = evaluate_factor_fitness(
        nan_df, labels, node_count=3, availability_mask=avail
    )
    assert res_nan.rejected is True
    assert res_nan.fitness == -np.inf

    # Terminal-only trees are never returned by GPFactorMiner
    cfg = GPConfig(population_size=12, n_generations=2, tournament_size=3, top_k=5, seed=11)
    selected = GPFactorMiner(cfg).mine(ohlcv, labels)
    for f in selected:
        assert f.node is not None
        assert not f.node.is_terminal()
        assert f.node_count > 1
        assert np.isfinite(f.fitness)


def test_gp_validity_mask_drops_neutral_fills_for_nonfinite_inputs():
    """A cell whose input terminal is non-finite must be NaN, never a fabricated 0.5/0.0.

    ``common/alpha101_operators.cs_rank`` fills missing cells with 0.5 and ``ts_*``
    operators fill with 0.0. When ``close`` is finite but ``volume`` is NaN the cell is
    tradable (availability mask True), so only the per-node validity mask can prevent the
    fabricated neutral value from entering GP fitness/IC.
    """
    ohlcv = _make_synthetic_ohlcv(n_dates=40, n_syms=6, seed=31)
    # Row 20 / column S2: close stays finite, but volume is non-finite.
    ohlcv["volume"].iloc[20, 2] = np.nan

    terminals, avail = build_terminals(ohlcv)
    assert bool(avail.iloc[20, 2]) is True
    assert np.isnan(terminals["volume"].iloc[20, 2])

    # (a) direct cross-sectional rank of volume
    rank_node = GPNode(op="cs_rank", children=(GPNode(op="term", value="volume"),))
    ranked = evaluate_node(rank_node, terminals, avail)
    assert np.isnan(ranked.iloc[20, 2]), "cs_rank NaN fill (0.5) must not survive"
    # Sibling cells in the same row/column remain valid
    assert np.isfinite(ranked.iloc[20, 3])
    assert np.isfinite(ranked.iloc[19, 2])

    # (b) NaN propagated through a nested derived node, then cross-sectionally ranked
    nested_rank = GPNode(
        op="cs_rank",
        children=(
            GPNode(
                op="div",
                children=(
                    GPNode(op="term", value="volume"),
                    GPNode(op="term", value="open"),
                ),
            ),
        ),
    )
    out = evaluate_node(nested_rank, terminals, avail)
    # div(volume, open) is NaN at the bad cell; cs_rank must not fabricate 0.5 there
    assert np.isnan(out.iloc[20, 2])
    assert np.isfinite(out.iloc[20, 3])
    assert np.isfinite(out.iloc[19, 2])
    assert not np.isinf(out.to_numpy(dtype=float)).any()
