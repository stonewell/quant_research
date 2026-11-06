"""Tests for M1.3: the opt-in turnover-penalized long-only QP on
`MinimumVarianceAllocation` (`common/allocation_templates.py`).

100% offline: every price series comes from `common.testing`'s synthetic
generators. No network, no real market data.

The feature is OPT-IN via `params["opt_turnover_penalty"] > 0`; its knobs
(including `opt_cash_allowance`) are deliberately absent from `param_grid`, so
the default search space is unchanged and the default code path must stay
bit-identical to pre-M1.3.

The budget constraint is the BOX `1 - opt_cash_allowance <= sum(w) <= 1`
(default fully invested). It is not `sum(w) <= 1` with free cash slack: that
program collapses, because a free cash residual leaves the budget multiplier
inactive (`nu = 0`), making the variance term drive total exposure toward zero
while the L1 term resists with only a fixed force `gamma` -- a fixed point that
is independent of `w0` and decays every rebalance. See
`test_no_exposure_decay_across_rebalances` for the regression pin.
"""

import numpy as np
import pandas as pd
import pytest

from common.allocation_templates import (
    MinimumVarianceAllocation,
    _min_variance_weights,
    _sector_groups,
    _turnover_penalized_weights,
)
from common.testing import make_ar1_ohlcv

BASE_PARAMS = {"cov_lookback": 60, "rebalance_freq_days": 63}

# Synthetic universe used by most template-level tests: four AR(1) price
# series with materially different volatilities, so the minimum-variance
# solution is genuinely unequal-weighted (it is NOT the 1/N degenerate case).
_UNIVERSE_SPEC = [
    ("AAA", 0.20, 1, 0.020),
    ("BBB", 0.40, 2, 0.050),
    ("CCC", 0.15, 3, 0.030),
    ("DDD", 0.60, 4, 0.080),
]


def _four_asset_universe() -> dict:
    return {
        s: make_ar1_ohlcv(phi=p, n=300, seed=se, scale=sc)
        for s, p, se, sc in _UNIVERSE_SPEC
    }


def _vol_switch_universe(switch: int = 150, mult: float = 4.0) -> dict:
    """Four near-equal-vol assets where AAA/BBB's return volatility is
    multiplied by `mult` from bar `switch` onward. That makes the
    minimum-variance composition genuinely migrate between rebalances, which
    is what the sector-budget and prune tests need (a static universe would
    make both assertions vacuous)."""
    out = {}
    for s, (se, sc) in zip(
        ["AAA", "BBB", "CCC", "DDD"], [(1, 0.010), (2, 0.012), (3, 0.010), (4, 0.011)]
    ):
        df = make_ar1_ohlcv(phi=0.3, n=300, seed=se, scale=sc)
        closes = df["Close"].to_numpy().copy()
        log_ret = np.diff(np.log(closes))
        log_ret[switch:] *= mult
        path = closes[0] * np.exp(np.concatenate([[0.0], np.cumsum(log_ret)]))
        out[s] = pd.DataFrame(
            {"Open": path, "High": path * 1.001, "Low": path * 0.999, "Close": path},
            index=df.index,
        )
    return out


def _rebalance_rows(weights: pd.DataFrame) -> pd.DataFrame:
    """The template's own rebalance rows: any row with at least one non-NaN."""
    return weights.dropna(how="all")


# ---------------------------------------------------------------------------
# 1. Direction of the shift (hand-computed 2-asset program).
# ---------------------------------------------------------------------------
def test_turnover_penalty_shifts_weights_toward_prior_target():
    # Two assets with unequal variances -> the gamma=0 long-only min-variance
    # portfolio is deliberately UNEQUAL (0.8261 / 0.1739, hand-checkable from
    # the 2x2 closed form). Prior target = 100% A.
    cov = np.array([[0.01, 0.002], [0.002, 0.04]])
    w0 = np.array([1.0, 0.0])

    w_gamma0 = _min_variance_weights(cov)
    assert abs(w_gamma0[0] - w_gamma0[1]) > 0.5, "case must not be the 1/N degenerate solution"

    w_pen = _turnover_penalized_weights(
        cov, w0, turnover_penalty=0.02, single_cap=1.0, sector_groups=None, sector_dev=0.03
    )

    # The penalty pulls the weights back toward the prior target (w_A = 1.0).
    assert w_pen[0] > w_gamma0[0], (
        f"penalized weight on A ({w_pen[0]:.6f}) must move toward w0[0]=1.0, "
        f"not away from the gamma=0 solution ({w_gamma0[0]:.6f})"
    )
    # Non-vacuity guard: it must NOT simply return w0 either.
    assert w_pen[0] < 1.0 - 1e-4
    assert np.all(w_pen >= 0.0)
    assert w_pen.sum() <= 1.0 + 1e-9


# ---------------------------------------------------------------------------
# 2/3. Cap invariant, long-only, cash budget.
# ---------------------------------------------------------------------------
def test_single_cap_invariant_on_every_rebalance_row():
    tpl = MinimumVarianceAllocation()
    # cap=0.30 with n=4 is feasible (n*cap = 1.2 >= 1) AND binding: the
    # unconstrained min-variance weight on AAA is ~0.38, so the cap genuinely
    # bites here (a non-vacuous check). A cap below 1/n would be infeasible
    # under the fully invested budget and is covered separately by
    # `test_cap_infeasibility_degrades_without_raising`.
    params = {
        **BASE_PARAMS,
        "opt_turnover_penalty": 1e-8,
        "opt_single_cap": 0.30,
        "opt_sector_dev": 0.03,
        "opt_prune": 0.0,
    }
    rows = _rebalance_rows(tpl.generate_weights(_four_asset_universe(), params))
    assert len(rows) >= 2
    assert np.nanmax(rows.to_numpy()) <= 0.30 + 1e-6
    np.testing.assert_allclose(rows.sum(axis=1).to_numpy(), 1.0, rtol=0.0, atol=1e-6)


def test_long_only_and_cash_budget():
    tpl = MinimumVarianceAllocation()
    params = {
        **BASE_PARAMS,
        "opt_turnover_penalty": 1e-8,
        "opt_single_cap": 1.0,
        "opt_sector_dev": 0.03,
        "opt_prune": 0.0,
    }
    rows = _rebalance_rows(tpl.generate_weights(_vol_switch_universe(), params))
    arr = rows.to_numpy()
    assert np.all(np.isfinite(arr))
    assert np.nanmin(arr) >= 0.0
    # Total-exposure BOX, not free cash slack: the default allowance of 0 pins
    # sum(w) to 1 (fully invested) within solver tolerance.
    sums = arr.sum(axis=1)
    assert np.nanmax(sums) <= 1.0 + 1e-6
    assert np.nanmin(sums) >= 1.0 - 1e-6


# ---------------------------------------------------------------------------
# 3b. REGRESSION PIN for the free-cash-slack defect (commit 392ab95).
# ---------------------------------------------------------------------------
def test_no_exposure_decay_across_rebalances():
    """With `sum(w) <= 1` (free cash slack) the program has `nu = 0`, so
    `2 S w + gamma * sign(w - w0) = 0`, i.e. `w ~= -(gamma/2) S^-1 sign(w - w0)`:
    the variance term drives exposure to zero and the fixed point is
    independent of `w0`. Measured exposure decayed 1.00 -> 0.94 -> 0.88 -> 0.82
    on this very universe. With the box `1 <= sum(w) <= 1` it must stay at 1.0
    on EVERY rebalance."""
    tpl = MinimumVarianceAllocation()
    params = {
        "cov_lookback": 60,
        "rebalance_freq_days": 21,
        "opt_turnover_penalty": 1e-8,
        "opt_single_cap": 1.0,
        "opt_cash_allowance": 0.0,
        "opt_sector_dev": 0.03,
        "opt_prune": 0.0,
    }
    rows = _rebalance_rows(tpl.generate_weights(_four_asset_universe(), params))
    assert len(rows) >= 5, f"need at least 5 rebalances, got {len(rows)}"
    sums = rows.sum(axis=1).to_numpy()[:5]
    np.testing.assert_allclose(sums, 1.0, rtol=0.0, atol=1e-6)
    # Non-vacuity: the composition still MOVES between rebalances, so this is
    # not a frozen all-in-one-asset book.
    first_five = rows.iloc[:5]
    assert (first_five.max() - first_five.min()).max() > 1e-3


def test_cash_allowance_is_respected():
    """`opt_cash_allowance` widens the box downward but never above 1. This
    objective has no expected-return term, so variance always falls when
    exposure falls: the optimizer ALWAYS wants cash, and the allowance is what
    bounds how much it can take (without it the book decays to zero -- the
    defect pinned above)."""
    tpl = MinimumVarianceAllocation()
    universe = _vol_switch_universe()
    params = {
        **BASE_PARAMS,
        "opt_turnover_penalty": 1e-8,
        "opt_single_cap": 1.0,
        "opt_sector_dev": 0.03,
        "opt_prune": 0.0,
    }
    full = _rebalance_rows(tpl.generate_weights(universe, {**params, "opt_cash_allowance": 0.0}))
    allowance = _rebalance_rows(
        tpl.generate_weights(universe, {**params, "opt_cash_allowance": 0.05})
    )

    np.testing.assert_allclose(full.sum(axis=1).to_numpy(), 1.0, rtol=0.0, atol=1e-6)
    sums = allowance.sum(axis=1).to_numpy()
    assert np.all(sums >= 0.95 - 1e-6), f"exposure fell through the allowance floor: {sums}"
    assert np.all(sums <= 1.0 + 1e-6), f"exposure exceeded the fully invested budget: {sums}"
    # Strictly smaller exposure: the cash window is actually used, and it is
    # the allowance -- not the decay -- that sets the floor.
    assert np.all(sums < 1.0 - 1e-3)


def test_cap_infeasibility_degrades_without_raising():
    """`cap=0.20` with `n=4` cannot fund a fully invested book
    (`n*cap = 0.8 < 1`). Documented degrade: relax the cap to `max(cap, 1/n)`,
    warn, and never raise."""
    tpl = MinimumVarianceAllocation()
    params = {
        "cov_lookback": 60,
        "rebalance_freq_days": 21,
        "opt_turnover_penalty": 1e-8,
        "opt_single_cap": 0.20,
        "opt_sector_dev": 0.03,
        "opt_prune": 0.0,
    }
    with pytest.warns(RuntimeWarning, match="infeasible"):
        rows = _rebalance_rows(tpl.generate_weights(_four_asset_universe(), params))

    assert len(rows) >= 2
    arr = rows.to_numpy()
    np.testing.assert_allclose(arr.sum(axis=1), 1.0, rtol=0.0, atol=1e-6)
    assert np.nanmax(arr) <= max(0.20, 0.25) + 1e-6
    note = tpl.explain_weights(params)
    assert "documented degrade" in note
    assert "25.00%" in note


# ---------------------------------------------------------------------------
# 3c. The first-rebalance special case is GONE (and provably unnecessary).
# ---------------------------------------------------------------------------
def test_first_rebalance_equals_min_variance_under_penalty():
    """With `w0 == 0` and `sum(w) == 1`, `||w - w0||_1 = sum(w) = 1` is
    CONSTANT over the feasible set, so the penalty cannot change the argmin:
    the first rebalance reduces to plain min-variance automatically. The old
    special case (a hand-written min-variance branch) has been deleted."""
    cov = np.array([[0.01, 0.002], [0.002, 0.04]])
    ref = _min_variance_weights(cov)
    assert not np.allclose(ref, 0.5), "reference must not be the 1/N degenerate solution"

    for gamma in (1e-6, 1e-3):
        w = _turnover_penalized_weights(
            cov, np.zeros(2), turnover_penalty=gamma, single_cap=1.0,
            sector_groups=None, sector_dev=0.03, cash_allowance=0.0,
        )
        np.testing.assert_allclose(w, ref, rtol=0.0, atol=1e-6)
        assert abs(w.sum() - 1.0) <= 1e-6

    # End to end: the template's first rebalance is the penalty solve on the
    # first window (and is fully invested), i.e. the deleted branch is not
    # reached. NON-VACUITY: the legacy `_min_variance_weights` call on this
    # window early-stops at equal weight (its documented ftol quirk), so a
    # resurrected special case would fail the equality below.
    tpl = MinimumVarianceAllocation()
    universe = _four_asset_universe()
    params = {
        "cov_lookback": 60,
        "rebalance_freq_days": 21,
        "opt_turnover_penalty": 1e-8,
        "opt_single_cap": 1.0,
        "opt_sector_dev": 0.03,
        "opt_prune": 0.0,
    }
    rows = _rebalance_rows(tpl.generate_weights(universe, params))

    master = universe["AAA"].index
    returns = pd.DataFrame({s: df["Close"].pct_change().reindex(master) for s, df in universe.items()})
    from common.covariance import denoise_covariance

    first_date = rows.index[0]
    loc = master.get_loc(first_date)
    sub_ret = returns.iloc[loc - 60:loc]
    cov_first = denoise_covariance(sub_ret.cov().to_numpy(), n_obs=len(sub_ret))
    expected = _turnover_penalized_weights(
        cov_first, np.zeros(4), 1e-8, 1.0, None, 0.03, 0.0
    )
    np.testing.assert_allclose(rows.iloc[0].to_numpy(), expected, rtol=0.0, atol=1e-6)


# ---------------------------------------------------------------------------
# 4. Per-sector deviation budget.
# ---------------------------------------------------------------------------
def test_sector_deviation_budget_is_enforced():
    sector_metadata = pytest.importorskip(
        "common.sector_metadata", reason="common/sector_metadata.py absent: sector budget not testable"
    )
    assert hasattr(sector_metadata, "resolve_sector")

    universe = _vol_switch_universe()
    sector_map = {"AAA": "banking", "BBB": "banking", "CCC": "oil", "DDD": "oil"}
    params = {
        **BASE_PARAMS,
        "opt_turnover_penalty": 1e-8,
        "opt_single_cap": 1.0,
        "opt_sector_dev": 0.03,
        "opt_prune": 0.0,
    }

    with_map = _rebalance_rows(
        MinimumVarianceAllocation().generate_weights(universe, {**params, "opt_sector_map": sector_map})
    )
    devs = []
    for prev_date, date in zip(with_map.index[:-1], with_map.index[1:]):
        for group in (["AAA", "BBB"], ["CCC", "DDD"]):
            devs.append(
                abs(with_map.loc[date, group].sum() - with_map.loc[prev_date, group].sum())
            )
    assert devs, "need at least two rebalance rows"
    assert max(devs) <= 0.03 + 1e-6, f"sector deviation {max(devs):.6f} exceeded the budget"

    # Non-vacuity: without the map every symbol collapses into resolve_sector's
    # single 'other' bucket, so only the TOTAL is budgeted and the sector split
    # is free to blow through 3%.
    no_map = _rebalance_rows(
        MinimumVarianceAllocation().generate_weights(universe, {**params, "opt_sector_map": None})
    )
    free_devs = []
    for prev_date, date in zip(no_map.index[:-1], no_map.index[1:]):
        for group in (["AAA", "BBB"], ["CCC", "DDD"]):
            free_devs.append(abs(no_map.loc[date, group].sum() - no_map.loc[prev_date, group].sum()))
    assert max(free_devs) > 0.03, "test would be vacuous: no-map run never exceeded the budget"


def test_sector_groups_are_built_from_resolve_sector():
    pytest.importorskip("common.sector_metadata")
    symbols = ["AAA", "BBB", "600000.SH", "000001.SZ"]
    groups = _sector_groups(symbols)
    # 'AAA'/'BBB' are not A-share codes -> both land in resolve_sector's
    # 'other' bucket; the two A-share codes get their ticker-prefix
    # pseudo-classes ('sec_60' / 'sec_00').
    assert [g.tolist() for g in groups] == [[0, 1], [2], [3]]
    assert all(isinstance(g, np.ndarray) for g in groups)
    # An explicit sector map overrides the fallback resolution.
    mapped = _sector_groups(symbols, {"AAA": "banking", "BBB": "banking"})
    assert [g.tolist() for g in mapped][0] == [0, 1]


# ---------------------------------------------------------------------------
# 5. Sparse contract: NaN on non-rebalance dates, explicit 0.0 on rebalance rows.
# ---------------------------------------------------------------------------
def test_nan_vs_explicit_zero_on_rebalance_rows():
    universe = {s: make_ar1_ohlcv(phi=0.3, n=300, seed=se, scale=0.03)
                for s, se in zip(["AAA", "BBB", "CCC"], [1, 2, 3])}
    master = universe["AAA"].index
    # DDD lists late: it is INVALID for the early lookback windows.
    universe["DDD"] = make_ar1_ohlcv(
        phi=0.3, n=len(master) - 200, seed=9, scale=0.04, start=str(master[200].date())
    )

    params = {**BASE_PARAMS, "opt_turnover_penalty": 1e-8, "opt_single_cap": 1.0,
              "opt_sector_dev": 0.03, "opt_prune": 0.0}
    weights = MinimumVarianceAllocation().generate_weights(universe, params)

    rows = _rebalance_rows(weights)
    assert len(rows) >= 3
    early = rows.index[rows["DDD"].eq(0.0)]
    assert len(early) > 0, "expected at least one rebalance before DDD has a full lookback"
    # Explicit 0.0 -- NOT NaN. A NaN cell here would be forward-filled by
    # allocation_backtester, silently keeping a stale DDD position.
    assert rows.loc[early, "DDD"].notna().all()
    assert (rows.loc[early, "DDD"] == 0.0).all()
    # ... and the row is otherwise dense/valid.
    assert rows.loc[early, ["AAA", "BBB", "CCC"]].notna().all().all()
    assert rows.loc[early, ["AAA", "BBB", "CCC"]].sum(axis=1).gt(0.0).all()

    # Non-rebalance dates are entirely NaN -- the template never forward-fills.
    non_rebal = weights.loc[~weights.index.isin(rows.index)]
    assert non_rebal.isna().all().all()
    assert len(weights) == len(master)


# ---------------------------------------------------------------------------
# 6. Prune: a sub-threshold trade snaps back to exactly the prior weight.
# ---------------------------------------------------------------------------
def test_prune_snaps_sub_threshold_trades_to_prior_weight():
    universe = _vol_switch_universe()
    base = {**BASE_PARAMS, "opt_turnover_penalty": 1e-8, "opt_single_cap": 1.0,
            "opt_sector_dev": 0.03}

    no_prune = _rebalance_rows(
        MinimumVarianceAllocation().generate_weights(universe, {**base, "opt_prune": 0.0})
    )
    pruned = _rebalance_rows(
        MinimumVarianceAllocation().generate_weights(universe, {**base, "opt_prune": 0.02})
    )

    prev, cur = no_prune.index[0], no_prune.index[1]
    deltas = (no_prune.loc[cur] - no_prune.loc[prev]).abs()
    small = deltas[(deltas > 1e-6) & (deltas < 0.02)]
    assert len(small) > 0, f"no sub-threshold move to prune (deltas={deltas.to_dict()})"

    for sym in small.index:
        assert pruned.loc[cur, sym] == pruned.loc[prev, sym], (
            f"{sym}: unpruned delta {deltas[sym]:.6f} < opt_prune but the weight did not "
            f"snap back to the prior value"
        )
    # Non-vacuity: the row is not simply a copy of the previous row.
    assert (pruned.loc[cur] != pruned.loc[prev]).any()

    # And the unpruned run really does differ there.
    for sym in small.index:
        assert no_prune.loc[cur, sym] != no_prune.loc[prev, sym]


# ---------------------------------------------------------------------------
# 7. Solver failure degrades, never raises.
# ---------------------------------------------------------------------------
def test_solver_failure_degrades_without_raising(monkeypatch):
    class _FakeResult:
        success = False
        message = "forced failure"
        x = None
        fun = None

    def _always_fail(*args, **kwargs):
        return _FakeResult()

    monkeypatch.setattr("scipy.optimize.minimize", _always_fail)

    tpl = MinimumVarianceAllocation()
    params = {**BASE_PARAMS, "opt_turnover_penalty": 1e-3, "opt_single_cap": 0.3,
              "opt_sector_dev": 0.03, "opt_prune": 0.0}
    weights = tpl.generate_weights(_four_asset_universe(), params)  # must not raise

    rows = _rebalance_rows(weights)
    assert len(rows) >= 2
    arr = rows.to_numpy()
    assert np.all(np.isfinite(arr))
    assert np.nanmin(arr) >= 0.0
    assert np.nanmax(arr) <= 0.3 + 1e-6
    assert np.nanmax(arr.sum(axis=1)) <= 1.0 + 1e-6
    # Rung 4/5 of the ladder: every symbol gets min(1/n, cap) = 0.25 -> capped 0.25? n=4 -> 0.25 > cap 0.3? no.
    assert np.allclose(arr, np.minimum(1.0 / 4, 0.3), atol=1e-9)


# ---------------------------------------------------------------------------
# 8. Disabled penalty: bit-identical to the pre-M1.3 implementation.
# ---------------------------------------------------------------------------
# Captured from `git show HEAD:common/allocation_templates.py` (pre-M1.3) on
# the exact universe/params used below. Hard-coded so BOTH a guard removal and
# a drift of the legacy solver are caught.
_LEGACY_EXPECTED = np.array([
    [0.3784238473758066, 0.24856835199546726, 0.3427105935366786, 0.030297207092047612],
    [0.37339757403763624, 0.25371948617503504, 0.34529803853377194, 0.02758490125355692],
    [0.36297269308180274, 0.2729556785771967, 0.3389547177401662, 0.02511691060083423],
    [0.35602794155498946, 0.2826808653151289, 0.3343669726253544, 0.02692422050452735],
    [0.352686402580933, 0.2909964878786846, 0.33112644591287055, 0.025190663627511913],
    [0.35088872675101374, 0.2939897059151537, 0.32724820566612056, 0.02787336166771208],
    [0.3542103826767928, 0.28256099365068293, 0.33381227671454883, 0.02941634695797548],
    [0.3554393843269077, 0.27177266681523765, 0.34061163316076715, 0.03217631569708754],
    [0.35448310563830343, 0.28017856221495524, 0.33946407542854207, 0.025874256718199318],
    [0.49531850063702826, 0.05506194290630345, 0.4264513658285806, 0.023168190628087566],
    [0.5156128456491242, 0.0442822218054604, 0.41765259735856486, 0.022452335186850487],
    [0.5029847481215293, 0.057829299966479224, 0.41190452221999774, 0.027281429691993653],
])


def test_disabled_penalty_helper_reduces_to_legacy_min_variance():
    rng = np.random.default_rng(7)
    a = rng.normal(size=(6, 6))
    cov = a @ a.T / 50.0 + np.eye(6) * 0.01
    w0 = np.full(6, 1.0 / 6)

    legacy = _min_variance_weights(cov)
    disabled = _turnover_penalized_weights(cov, w0, 0.0, 0.20, None, 0.03)
    np.testing.assert_allclose(disabled, legacy, rtol=1e-9, atol=0.0)


def test_disabled_penalty_generate_weights_is_bit_identical():
    universe = _four_asset_universe()
    tpl = MinimumVarianceAllocation()
    params = {"cov_lookback": 60, "rebalance_freq_days": 21}

    implicit = tpl.generate_weights(universe, dict(params))
    explicit = tpl.generate_weights(universe, {**params, "opt_turnover_penalty": 0.0})
    assert implicit.equals(explicit)
    # ... and both equal the PRE-M1.3 output, digit for digit.
    np.testing.assert_array_equal(_rebalance_rows(implicit).to_numpy(), _LEGACY_EXPECTED)
    # The other new knobs are inert while the penalty is off.
    off_with_knobs = tpl.generate_weights(
        universe,
        {**params, "opt_turnover_penalty": 0.0, "opt_single_cap": 0.05,
         "opt_sector_dev": 0.001, "opt_prune": 0.5},
    )
    assert off_with_knobs.equals(implicit)


def test_opt_params_are_not_in_the_search_grid():
    grid = MinimumVarianceAllocation().param_grid
    assert set(grid) == {"cov_lookback", "rebalance_freq_days"}
    for key in (
        "opt_turnover_penalty",
        "opt_single_cap",
        "opt_cash_allowance",
        "opt_sector_dev",
        "opt_prune",
    ):
        assert key not in grid
    # The new cash-allowance knob defaults to fully invested.
    assert MinimumVarianceAllocation().opt_cash_allowance == 0.0


# ---------------------------------------------------------------------------
# 9. Degenerate universes.
# ---------------------------------------------------------------------------
def test_single_symbol_universe_unchanged():
    universe = {"AAA": make_ar1_ohlcv(phi=0.3, n=200, seed=5, scale=0.02)}
    tpl = MinimumVarianceAllocation()
    base = {"cov_lookback": 60, "rebalance_freq_days": 63}

    legacy = _rebalance_rows(tpl.generate_weights(universe, dict(base)))
    assert len(legacy) > 0
    assert np.allclose(legacy["AAA"].to_numpy(), 1.0)

    penalized = _rebalance_rows(
        tpl.generate_weights(
            universe, {**base, "opt_turnover_penalty": 1e-3, "opt_single_cap": 0.6}
        )
    )
    # The cap 0.6 is infeasible for n=1 under the fully invested budget
    # (1*0.6 < 1), so the documented degrade relaxes it to 1.0: a single-asset
    # book is 100% that asset, no cash residual.
    assert np.allclose(penalized["AAA"].to_numpy(), 1.0)


def test_all_nan_window_returns_empty_frames_without_raising():
    # No usable history at all -> every rebalance row is skipped, exactly as
    # before M1.3 ("continue" without writing a row).
    universe = {
        "AAA": pd.DataFrame({"Close": [np.nan] * 200},
                            index=pd.bdate_range("2020-01-01", periods=200)),
        "BBB": pd.DataFrame(),
    }
    tpl = MinimumVarianceAllocation()
    base = {"cov_lookback": 60, "rebalance_freq_days": 21}

    for extra in ({}, {"opt_turnover_penalty": 1e-3}):
        out = tpl.generate_weights(universe, {**base, **extra})
        assert isinstance(out, pd.DataFrame)
        assert out.isna().all().all()
