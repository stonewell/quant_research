"""Unit tests for asymmetric rank-band selection and delta-score gate.

All tests run 100% offline with synthetic data.
"""

import os
import sys
import pytest
import numpy as np
import pandas as pd

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
for _sub in ("pipeline", "ml"):
    _p = os.path.join(_PROJECT_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from common.selection_band import (
    RankBandSelection,
    apply,
    compute_percentile_ranks,
    _validate_bands,
    _sparse_from_daily,
)
from common.allocation_templates import CrossSectionalMomentumAllocation
from common.testing import make_ohlcv_from_closes


def test_band_validation():
    """(a) Validate band parameter domain checks."""
    # buy_band_pct >= sell_band_pct raises ValueError
    with pytest.raises(ValueError, match="strictly less than"):
        _validate_bands(0.30, 0.30, 0.35)
    with pytest.raises(ValueError, match="strictly less than"):
        _validate_bands(0.40, 0.20, 0.35)

    # Invalid range bounds
    with pytest.raises(ValueError, match="buy_band_pct must be in"):
        _validate_bands(0.0, 0.30, 0.35)
    with pytest.raises(ValueError, match="buy_band_pct must be in"):
        _validate_bands(1.5, 2.0, 0.35)
    with pytest.raises(ValueError, match="sell_band_pct must be in"):
        _validate_bands(0.10, 0.0, 0.35)
    with pytest.raises(ValueError, match="sell_band_pct must be in"):
        _validate_bands(0.10, 1.2, 0.35)
    with pytest.raises(ValueError, match="delta_score_sigma must be non-negative"):
        _validate_bands(0.10, 0.30, -0.1)

    # Valid configurations succeed without error
    _validate_bands(0.10, 0.30, 0.35)
    _validate_bands(0.05, 0.25, 0.0)


def test_percentile_ranks_deterministic_tie_break():
    """(b) Deterministic tie-breaking by descending score, then ascending symbol name."""
    scores = pd.Series({"B": 10.0, "A": 10.0, "C": 5.0, "D": 20.0})
    ranks = compute_percentile_ranks(scores)
    # Sorted: D (20.0, rank 0/4 = 0.0), A (10.0, rank 1/4 = 0.25), B (10.0, rank 2/4 = 0.5), C (5.0, rank 3/4 = 0.75)
    assert ranks["D"] == 0.0
    assert ranks["A"] == 0.25
    assert ranks["B"] == 0.50
    assert ranks["C"] == 0.75


def test_initial_allocation_entry_band():
    """(c) On date 0, only assets with rank <= buy_band_pct enter with equal weight 1/n."""
    dates = pd.date_range("2020-01-01", periods=1, freq="21D")
    # 10 assets with scores 1 to 10
    symbols = [f"S{i}" for i in range(10)]
    scores_data = {f"S{i}": [float(i)] for i in range(10)}
    scores = pd.DataFrame(scores_data, index=dates)
    # Ranks: S9=0.0, S8=0.1, S7=0.2, ...
    # buy_band_pct = 0.15 -> S9 and S8 (rank 0.0 and 0.10 <= 0.15)
    res = RankBandSelection.apply(scores, buy_band_pct=0.15, sell_band_pct=0.40, delta_score_sigma=0.35)
    assert set(res.loc[dates[0]][res.loc[dates[0]] > 0].index) == {"S9", "S8"}
    np.testing.assert_allclose(res.loc[dates[0], "S9"], 0.5)
    np.testing.assert_allclose(res.loc[dates[0], "S8"], 0.5)
    # All other assets must be 0.0
    for i in range(8):
        assert res.loc[dates[0], f"S{i}"] == 0.0


def test_retention_in_sell_band():
    """(d) Held asset retained when rank falls between buy_band_pct and sell_band_pct."""
    dates = pd.date_range("2020-01-01", periods=2, freq="21D")
    # 10 assets
    # Date 0: S9 and S8 are top 2
    # Date 1: S7 rises to top (rank 0.0), S9 slips to rank 2 (0.2), S8 slips to rank 3 (0.3)
    # With buy_band_pct=0.15, sell_band_pct=0.35:
    # S9 (0.2) and S8 (0.3) are <= 0.35, so both are retained! S7 enters (<= 0.15).
    d0 = {f"S{i}": float(i) for i in range(10)}
    d1 = {f"S{i}": float(i) for i in range(10)}
    d1["S7"] = 100.0  # rank 0.0
    d1["S9"] = 50.0   # rank 0.10
    d1["S8"] = 40.0   # rank 0.20 (<= 0.35, retained)
    scores = pd.DataFrame([d0, d1], index=dates)

    res = RankBandSelection.apply(scores, buy_band_pct=0.15, sell_band_pct=0.35, delta_score_sigma=0.0)
    # Held on date 1: S9, S8 retained + S7 entered
    assert set(res.loc[dates[1]][res.loc[dates[1]] > 0].index) == {"S9", "S8", "S7"}
    np.testing.assert_allclose(res.loc[dates[1], "S9"], 1.0 / 3.0)


def test_exit_when_rank_exceeds_sell_band_nan_vs_zero_contract():
    """(e) Held asset exiting must write explicit 0.0, never NaN!"""
    dates = pd.date_range("2020-01-01", periods=2, freq="21D")
    # Date 0: S9 is top asset (rank 0.0)
    # Date 1: S9 crashes to bottom (score -100, rank 0.90 > sell_band 0.30)
    d0 = {f"S{i}": float(i) for i in range(10)}
    d1 = {f"S{i}": float(i) for i in range(10)}
    d1["S9"] = -100.0  # drops out
    d1["S8"] = 100.0   # enters
    scores = pd.DataFrame([d0, d1], index=dates)

    res = RankBandSelection.apply(scores, buy_band_pct=0.15, sell_band_pct=0.30, delta_score_sigma=0.0)
    row1 = res.loc[dates[1]]
    assert not row1.isna().any(), "Rebalance row must never contain NaN"
    assert row1["S9"] == 0.0, "Exited asset S9 must be explicit 0.0, not NaN"
    assert row1["S8"] > 0.0, "New entrant S8 must have positive weight"


def test_delta_score_gate_veto():
    """(f) Delta-score gate vetoes swap when score difference is within delta_score_sigma * sigma."""
    dates = pd.date_range("2020-01-01", periods=2, freq="21D")
    # Date 0: S0 is held (rank 0.0 <= 0.15)
    scores_d0 = {"S0": 100.0, "S1": 50.0, "S2": 0.0, "S3": -20.0, "S4": -50.0}
    # On Date 1, S0 drops into exit band (rank 2/5 = 0.40 > 0.35), S1 is in buy band (rank 0/5 = 0.0 <= 0.15)
    # S1 - S0 is 0.5, but sigma is ~28 -> threshold is 28 * 1.0 -> VETO
    scores_d1 = {"S1": 10.5, "S2": 10.2, "S0": 10.0, "S3": -20.0, "S4": -50.0}
    scores = pd.DataFrame([scores_d0, scores_d1], index=dates)

    res = RankBandSelection.apply(scores, buy_band_pct=0.15, sell_band_pct=0.35, delta_score_sigma=1.0)
    # S0 was vetoed from exiting, and S1 was vetoed from entering! S0 remains held.
    # Because held set and weights are unchanged from Date 0, Date 1 row is compressed to NaN per sparse weights contract.
    assert res.loc[dates[1]].isna().all()
    ffilled = res.ffill()
    assert ffilled.loc[dates[1], "S0"] == 1.0
    assert ffilled.loc[dates[1], "S1"] == 0.0


def test_delta_score_gate_approval():
    """(g) Delta-score gate approves swap when score difference exceeds threshold."""
    dates = pd.date_range("2020-01-01", periods=2, freq="21D")
    scores_d0 = {"S0": 100.0, "S1": 50.0, "S2": 0.0, "S3": -20.0, "S4": -50.0}
    # S1 exceeds S0 by 90.0, well above threshold (0.35 * ~49 ~ 17.15)
    scores_d1 = {"S1": 80.0, "S2": 20.0, "S0": -10.0, "S3": -20.0, "S4": -50.0}
    scores = pd.DataFrame([scores_d0, scores_d1], index=dates)

    res = RankBandSelection.apply(scores, buy_band_pct=0.15, sell_band_pct=0.35, delta_score_sigma=0.35)
    assert res.loc[dates[1], "S1"] == 1.0
    assert res.loc[dates[1], "S0"] == 0.0


def test_sparse_weights_row_omission():
    """(h) Consecutive identical rebalance rows are compressed to NaN per sparse weights contract."""
    dates = pd.date_range("2020-01-01", periods=3, freq="21D")
    # Identical scores on date 0, 1, 2
    d = {f"S{i}": float(i) for i in range(5)}
    scores = pd.DataFrame([d, d, d], index=dates)

    res = RankBandSelection.apply(scores, buy_band_pct=0.30, sell_band_pct=0.50, delta_score_sigma=0.35)
    # Date 0 must be present
    assert not res.loc[dates[0]].isna().all()
    # Date 1 and Date 2 must be all NaN (omitted because holdings/weights identical)
    assert res.loc[dates[1]].isna().all()
    assert res.loc[dates[2]].isna().all()


def test_cross_sectional_momentum_flag_off_byte_identity():
    """(i) CrossSectionalMomentumAllocation with enable_rank_band=False is byte-identical to baseline."""
    rng = np.random.default_rng(42)
    universe = {}
    for sym in ("A", "B", "C", "D", "E"):
        closes = 100 + np.cumsum(rng.normal(0.05, 1.0, 300))
        universe[sym] = make_ohlcv_from_closes(closes, start="2020-01-01")

    params_baseline = {"mom_lookback": 63, "top_n_fraction": 0.4, "rebalance_freq_days": 21}
    params_flag_off = {
        "mom_lookback": 63,
        "top_n_fraction": 0.4,
        "rebalance_freq_days": 21,
        "enable_rank_band": False,
    }

    template = CrossSectionalMomentumAllocation()
    weights_base = template.generate_weights(universe, params_baseline)
    weights_flag_off = template.generate_weights(universe, params_flag_off)

    pd.testing.assert_frame_equal(weights_base, weights_flag_off, check_exact=True)

    # Equality between the two calls alone would still hold if the flag were silently
    # ignored (both calls would then take the SAME branch), so also pin the actual
    # top-N contract: exactly ``top_n`` positively-weighted names per rebalance row, each
    # at ``1 / top_n``, and a dense (0.0-filled, never NaN) row -- the shape the rank-band
    # path deliberately does NOT have.
    top_n = max(1, int(5 * 0.4))
    # Top-N rows are dense (0.0 for non-winners), so filter by weight sum, not by NaN-ness.
    invested = weights_base[weights_base.sum(axis=1) > 0]
    assert not invested.empty
    for _, row in invested.iterrows():
        assert row.notna().all(), "top-N rebalance row must be dense (0.0 for non-winners)"
        nonzero = row[row > 0]
        assert len(nonzero) == top_n, f"expected exactly {top_n} winners, got {len(nonzero)}"
        np.testing.assert_allclose(nonzero.to_numpy(), 1.0 / top_n)


def test_cross_sectional_momentum_flag_on_runs_rank_band():
    """(j) CrossSectionalMomentumAllocation with enable_rank_band=True executes rank band.

    Pins rank-band-exclusive behaviour:
    1. Rebalance rows exhibit hysteresis where held names whose rank falls between
       top_n and sell_band_pct are retained, producing rebalance rows with > top_n
       holdings (e.g. 3 holdings vs top_n=2).
    2. Weight series strictly differs from the top-N equal-weight baseline.
    """
    rng = np.random.default_rng(99)
    universe = {}
    for sym in ("A", "B", "C", "D", "E"):
        closes = 100 + np.cumsum(rng.normal(0.05, 1.0, 300))
        universe[sym] = make_ohlcv_from_closes(closes, start="2020-01-01")

    params_flag_on = {
        "mom_lookback": 63,
        "top_n_fraction": 0.4,
        "rebalance_freq_days": 21,
        "enable_rank_band": True,
        "buy_band_pct": 0.20,
        "sell_band_pct": 0.50,
        "delta_score_sigma": 0.35,
    }
    params_flag_off = dict(params_flag_on, enable_rank_band=False)

    template = CrossSectionalMomentumAllocation()
    weights_flag_on = template.generate_weights(universe, params_flag_on)
    weights_flag_off = template.generate_weights(universe, params_flag_off)

    top_n = max(1, int(len(universe) * params_flag_on["top_n_fraction"]))

    # 1. Output must not be identical to plain top-N baseline
    assert not weights_flag_on.equals(weights_flag_off), "Rank-band output must differ from top-N baseline"

    rebal_rows = weights_flag_on.dropna(how="all")
    assert not rebal_rows.empty

    # 2. Pin behaviour ONLY the band can produce: at least one rebalance row has
    # positive weight count != top_n due to buffer retention / hysteresis
    counts = [len(row[row > 0]) for _, row in rebal_rows.iterrows() if row.sum() > 0]
    assert any(c != top_n for c in counts), f"Expected at least one rebalance row with count != {top_n}, got {counts}"

    # Specifically, on 2020-11-19, asset 'D' was held previously and slipped past top_n but stayed <= sell_band_pct,
    # so 3 assets are held at 1/3 weight, whereas top-N holds exactly 2 assets at 1/2 weight
    dt_target = pd.Timestamp("2020-11-19")
    if dt_target in weights_flag_on.index:
        row_target = weights_flag_on.loc[dt_target]
        pos_target = row_target[row_target > 0]
        assert len(pos_target) == 3, f"Expected 3 held assets on {dt_target}, got {len(pos_target)}"
        assert "D" in pos_target.index, "Held asset D must be retained via sell-band hysteresis"

    for date, row in rebal_rows.iterrows():
        s = row.sum()
        if s > 0:
            np.testing.assert_allclose(s, 1.0, atol=1e-7)


def test_held_nan_score_retained_and_unheld_nan_ineligible():
    """(k) Held asset with NaN score is retained; unheld NaN asset remains ineligible."""
    dates = pd.date_range("2020-01-01", periods=2, freq="21D")
    # Date 0: S0 and S1 top ranks, S2 and S3 lower
    d0 = {"S0": 100.0, "S1": 80.0, "S2": 10.0, "S3": 5.0}
    # Date 1:
    # S0 (held) has NaN score (e.g. trading suspension) -> must be RETAINED with weight > 0
    # S1 (held) drops rank
    # S2 (unheld) enters with top score
    # S3 (unheld) has NaN score -> must NOT enter (ineligible)
    d1 = {"S0": np.nan, "S1": 10.0, "S2": 100.0, "S3": np.nan}
    scores = pd.DataFrame([d0, d1], index=dates)

    res = RankBandSelection.apply(
        scores, buy_band_pct=0.30, sell_band_pct=0.60, delta_score_sigma=0.0
    )

    # Date 0: S0 and S1 are held with equal weight
    assert res.loc[dates[0], "S0"] == 0.5
    assert res.loc[dates[0], "S1"] == 0.5
    assert res.loc[dates[0], "S2"] == 0.0
    assert res.loc[dates[0], "S3"] == 0.0

    # Date 1:
    # S0 was held and has NaN score -> must be RETAINED, weight > 0
    # S2 entered (rank 0/2 <= 0.30)
    # S3 was NOT held and has NaN score -> must NOT enter, weight == 0.0
    row1 = res.loc[dates[1]]
    assert row1["S0"] > 0.0, "Held asset S0 with NaN score must be retained"
    assert row1["S3"] == 0.0, "Unheld asset S3 with NaN score must remain ineligible"
    assert row1["S2"] > 0.0, "Valid entrant S2 must enter"
    assert not row1.isna().any(), "Rebalance row must be fully populated with weights (no NaN cells)"
    np.testing.assert_allclose(row1.sum(), 1.0, atol=1e-7)
