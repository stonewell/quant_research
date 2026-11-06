"""Acceptance tests for the M4.1 policy kernel (`common/position_policy.py`).

Gate under test -- `docs/development_plan.md` §2.16 `M4.1`: the new pure kernel
must reproduce `common/allocation_templates.py::apply_asset_inertia` (and its
private `_step_asset_inertia`) **bit-for-bit**, because the phase goal is that
the mock `kernel_apply_asset_inertia` is cell-identical to the production
function it will later replace (M4.2/M4.3 flip the call sites).

Two rules this file refuses to bend:

  * **`NaN` is never `0.0`.** In the sparse-weights contract a `NaN` cell means
    "no instruction, carry the previous weight" and a `0.0` cell means "exit
    this position". The differential harness therefore compares the NaN mask
    and the real values SEPARATELY, and `test_differential_helper_rejects_nan_vs_zero`
    is a meta-test proving the harness itself cannot silently equate them.
  * **The assertion is never weakened to make a seed pass.** Every parametrized
    seed runs the full frame through both implementations with the same kwargs
    and demands identical index, identical columns, identical NaN positions and
    identical values (including identical real `0.0` cells).

The M4.1 kernel is DEAD CODE: `test_kernel_has_no_callers_in_templates_or_blend_base`
statically guards that `common/allocation_templates.py` and
`pipeline/research/blend/blend_base.py` do not import it.

Disclosed finding recorded here rather than hidden (see
`test_reapplying_kernel_to_its_own_output`): re-applying the inertia filter to
its own output is **not bit-exact** -- but that is a property of the reference
algorithm, not of the kernel. A committed buy writes
`current + (ideal - current)`, which differs from `ideal` by up to 1 ULP, so the
second pass reproduces the frame to ~2.2e-16 and never changes a NaN position or
a zero/close pattern. The kernel's second pass is bit-identical to
`apply_asset_inertia`'s second pass (asserted), so parity holds; only the
strict "f(f(x)) == f(x) bit-for-bit" phrasing is unprovable for either side.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.allocation_templates import apply_asset_inertia
from common.position_policy import (
    AssetPolicy,
    AssetState,
    BlockingReason,
    Decision,
    PolicyAction,
    PolicyContext,
    PortfolioSequencer,
    PositionPolicyConfig,
    PositionState,
    SparseProjector,
    kernel_apply_asset_inertia,
)

# Number of randomized differential seeds. The gate asks for at least 200.
SEEDS = list(range(240))


# ---------------------------------------------------------------------------
# Differential harness helpers
# ---------------------------------------------------------------------------


def _first_mismatch(ref: pd.DataFrame, got: pd.DataFrame) -> str:
    """Human-readable first differing cell, or `""` when the frames match."""
    na_ref = ref.isna().to_numpy()
    na_got = got.isna().to_numpy()
    bad = np.argwhere(na_ref != na_got)
    if len(bad):
        r, c = bad[0]
        ref_v = "NaN" if na_ref[r, c] else repr(float(ref.iat[r, c]))
        got_v = "NaN" if na_got[r, c] else repr(float(got.iat[r, c]))
        return (
            f"NaN vs value at (row={ref.index[r]!r}, col={ref.columns[c]!r}): "
            f"ref={ref_v} got={got_v}"
        )
    a = ref.to_numpy(dtype=float)
    b = got.to_numpy(dtype=float)
    bad = np.argwhere(~(a == b))
    if len(bad):
        r, c = bad[0]
        return (
            f"value at (row={ref.index[r]!r}, col={ref.columns[c]!r}): "
            f"ref={a[r, c]!r} got={b[r, c]!r} delta={b[r, c] - a[r, c]!r}"
        )
    return ""


def _assert_cell_identical(ref: pd.DataFrame, got: pd.DataFrame, *, context: str) -> None:
    """The M4.1 gate: identical cells, with NaN positions checked separately.

    `NaN == 0.0` is treated as a FAILURE (see the module docstring): a missing
    instruction row and a real exit are different instructions to the backtester.
    """
    assert ref.index.equals(got.index), f"{context}: index differs"
    assert ref.columns.equals(got.columns), f"{context}: columns differ"
    mismatch = _first_mismatch(ref, got)
    assert (ref.isna() == got.isna()).all().all(), (
        f"{context}: NaN positions differ (NaN is NOT 0.0) -- {mismatch}"
    )
    try:
        np.testing.assert_array_equal(ref.to_numpy(), got.to_numpy())
    except AssertionError as exc:  # pragma: no cover - only on a real defect
        raise AssertionError(f"{context}: values differ -- {mismatch}\n{exc}") from None


def _assert_no_non_sell_close_promotion(
    ref: pd.DataFrame, got: pd.DataFrame, ideal: pd.DataFrame, *, context: str
) -> None:
    """Invariant 2, stated explicitly: a non-SELL must never be promoted to a close.

    For every cell whose RAW ideal target is a genuine holding (`> 1e-4`), a
    kernel output of exactly `0.0` is allowed only where `apply_asset_inertia`
    also produces exactly `0.0`. (Fully implied by cell equality; named here so
    a regression reports the mechanism, not just "frames differ".)
    """
    real_target = ideal.to_numpy(dtype=float) > 1e-4
    ref_zero = ref.to_numpy(dtype=float) == 0.0
    got_zero = got.to_numpy(dtype=float) == 0.0
    violations = np.argwhere(real_target & got_zero & ~ref_zero)
    assert not len(violations), (
        f"{context}: kernel closed a live position the reference kept "
        f"(non-SELL promoted to a close) at {[tuple(x) for x in violations[:5]]}"
    )


def _kw_summary(kwargs: dict) -> str:
    """Compact, seed-reproducible summary of the kwargs (no giant frame reprs)."""
    parts = []
    for key in sorted(kwargs):
        value = kwargs[key]
        if isinstance(value, pd.DataFrame):
            parts.append(f"{key}=DataFrame(shape={value.shape})")
        elif isinstance(value, pd.Series):
            parts.append(
                f"{key}=Series(len={len(value)}, dtype={value.dtype}, "
                f"n_true={int(value.astype(bool).sum())})"
            )
        elif isinstance(value, dict):
            parts.append(f"{key}=dict({len(value)} entries)")
        else:
            parts.append(f"{key}={value!r}")
    return "{" + ", ".join(parts) + "}"


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------


def _make_daily_frame(rng, n_rows: int, n_risky: int, *, sparseish: bool, with_cash: bool):
    """A realistic dense target-weight frame.

    Risky weights live in [0, 0.4] and their row sum is capped at 0.98, with
    `CASH` as the exact complement when present (the only shape real templates
    produce). Most bars are a small random walk so the inertia dead band and the
    tenure locks actually engage; ~15% of bars jump, so sells/buys clear their
    thresholds. `sparseish=True` adds exact `0.0` cells and repeated identical
    rows -- the "unchanged -> stay frozen (and later stay NaN)" path.
    """
    symbols = [f"SYM{i:02d}" for i in range(n_risky)]
    index = pd.bdate_range("2021-03-01", periods=n_rows)
    current = rng.uniform(0.0, 0.30, n_risky)
    rows = np.empty((n_rows, n_risky), dtype=float)
    for t in range(n_rows):
        if t > 0 and rng.random() < 0.15:
            current = np.clip(rng.uniform(0.0, 0.40, n_risky), 0.0, 0.45)
        else:
            current = np.clip(current + rng.normal(0.0, 0.04, n_risky), 0.0, 0.40)
        if sparseish and rng.random() < 0.5:
            current = current.copy()
            current[rng.random(n_risky) < 0.35] = 0.0
        rows[t] = current

    if sparseish:
        for t in (5, 11, 12, 19):
            if 0 < t < n_rows:
                rows[t] = rows[t - 1]  # byte-identical repeat rows
        rows[:, 0][::7] = 0.0  # exact zeros scattered through a column

    totals = rows.sum(axis=1, keepdims=True)
    rows = rows * np.minimum(1.0, 0.98 / np.maximum(totals, 1e-12))
    frame = pd.DataFrame(np.round(rows, 8), index=index, columns=symbols)
    if with_cash:
        frame["CASH"] = np.round(1.0 - frame.sum(axis=1), 8)
    return frame


def _build_case(seed: int):
    """Deterministically build one (frame, kernel kwargs, reference kwargs) case.

    Everything except the constant-`bool` emergency mask is shared verbatim
    between the two implementations; for that one case the reference (which
    cannot index a bare `bool`) gets the equivalent constant `Series`, so the
    comparison still exercises the same instruction.
    """
    rng = np.random.default_rng(seed)
    n_rows = int(rng.integers(30, 61))
    n_risky = int(rng.integers(6, 9))
    sparseish = (seed % 3 == 0)

    cash_proxy = ["CASH", None, "MISSING"][int(rng.integers(0, 3))]
    with_cash = True if cash_proxy == "CASH" else bool(rng.random() < 0.6)
    daily = _make_daily_frame(
        rng, n_rows=n_rows, n_risky=n_risky, sparseish=sparseish, with_cash=with_cash
    )

    if cash_proxy == "CASH":
        # Deliberately make some bars' CASH cell inconsistent with
        # `1 - risky sum` (an unallocated/floating residual is what real
        # templates emit before this filter). Without this, a defect that
        # rewrites the residual unconditionally would be invisible on bars whose
        # residual is already idempotent; with it, the rewrite is observable.
        # Only done when CASH is the cash proxy, so the long-only / sum<=1
        # invariants (which sum the RISKY columns) are untouched.
        below = list(daily.index[::3])
        above = list(daily.index[::5])
        daily.loc[below, "CASH"] = (daily.loc[below, "CASH"] - 0.01).clip(lower=0.0)
        daily.loc[above, "CASH"] = daily.loc[above, "CASH"] + 0.015

    kwargs = {"cash_proxy": cash_proxy}

    # -- min_weight_change: float / dict / Series / date-indexed DataFrame ----
    threshold_mode = int(rng.integers(0, 4))
    if threshold_mode == 0:
        kwargs["min_weight_change"] = float(rng.choice([0.0, 0.02, 0.05, 0.10]))
    elif threshold_mode == 1:
        symbols = list(daily.columns)
        picks = rng.choice(len(symbols), size=max(2, len(symbols) - 2), replace=False)
        kwargs["min_weight_change"] = {
            symbols[int(i)]: float(rng.choice([0.01, 0.02, 0.05, 0.10])) for i in picks
        }
    elif threshold_mode == 2:
        kwargs["min_weight_change"] = pd.Series(
            rng.uniform(0.0, 0.10, size=len(daily.columns)), index=daily.columns
        )
    else:
        kwargs["min_weight_change"] = pd.DataFrame(
            rng.uniform(0.0, 0.10, size=daily.shape), index=daily.index, columns=daily.columns
        )

    kwargs["asymmetric_sells"] = bool(rng.integers(0, 2))
    kwargs["asymmetric_sell_ratio"] = float(rng.choice([0.60, 0.35]))
    kwargs["entry_threshold"] = [None, 0.05][int(rng.integers(0, 2))]
    kwargs["min_holding_bars"] = int(rng.choice([0, 2, 7]))

    risky = [c for c in daily.columns if c != cash_proxy]

    # -- emergency_mask: None / constant bool / per-row Series / per-cell DF ---
    emergency_mode = int(rng.integers(0, 4))
    ref_kwargs = dict(kwargs)
    if emergency_mode == 0:
        kwargs["emergency_mask"] = None
    elif emergency_mode == 1:
        flag = bool(rng.integers(0, 2))
        kwargs["emergency_mask"] = flag
        # The reference indexes `emergency_mask.iloc[t]`; a bare bool would
        # raise. The constant Series is the same instruction (global flag).
        ref_kwargs["emergency_mask"] = pd.Series(flag, index=daily.index)
    elif emergency_mode == 2:
        flags = pd.Series(rng.random(len(daily)) < 0.10, index=daily.index)
        flags.iloc[2] = True  # at least one all-True (= global) row
        flags.iloc[5] = False
        kwargs["emergency_mask"] = flags
        ref_kwargs["emergency_mask"] = flags
    else:
        mask = pd.DataFrame(
            rng.random((len(daily), len(risky))) < 0.10,
            index=daily.index,
            columns=risky,
        )
        mask.iloc[3] = True  # all-True row -> global emergency
        mask.iloc[7] = False
        if len(risky) > 1:
            mask.iloc[9] = [True] + [False] * (len(risky) - 1)  # mixed row
        kwargs["emergency_mask"] = mask
        ref_kwargs["emergency_mask"] = mask

    return daily, kwargs, ref_kwargs


# ---------------------------------------------------------------------------
# A. Randomized differential harness -- the hard gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_kernel_is_cell_identical_to_apply_asset_inertia(seed):
    """M4.1 gate: 240 seeds, whole matrix of threshold/cash/emergency/tenure shapes."""
    daily, kwargs, ref_kwargs = _build_case(seed)
    context = f"seed={seed} kwargs={_kw_summary(kwargs)}"

    ref = apply_asset_inertia(daily, **ref_kwargs)
    got = kernel_apply_asset_inertia(daily, **kwargs)

    # Invariant 2, stated explicitly before the equality gate so a regression
    # names the mechanism (see helper docstring).
    _assert_no_non_sell_close_promotion(ref, got, daily, context=context)

    # Same input -> same output (plan §2.16 M4.1 invariant 5, determinism half).
    again = kernel_apply_asset_inertia(daily, **kwargs)
    pd.testing.assert_frame_equal(got, again)

    assert ref.index.equals(got.index) and ref.columns.equals(got.columns), context
    assert (ref.isna() == got.isna()).all().all(), (
        f"{context}: NaN positions differ (NaN is NOT 0.0) -- {_first_mismatch(ref, got)}"
    )
    try:
        np.testing.assert_array_equal(ref.to_numpy(), got.to_numpy())
    except AssertionError as exc:  # pragma: no cover - only on a real defect
        raise AssertionError(
            f"{context}: values differ -- {_first_mismatch(ref, got)}\n{exc}"
        ) from None


def test_differential_helper_rejects_nan_vs_zero():
    """Meta-test: the harness itself must never treat `NaN == 0.0` as equal."""
    index = pd.bdate_range("2024-01-01", periods=2)
    left = pd.DataFrame({"AAA": [0.25, np.nan]}, index=index)
    right = pd.DataFrame({"AAA": [0.25, 0.0]}, index=index)
    with pytest.raises(AssertionError, match="NaN"):
        _assert_cell_identical(left, right, context="nan-vs-zero")
    # ... and a real 0.0 on both sides IS equal.
    zero = pd.DataFrame({"AAA": [0.25, 0.0]}, index=index)
    _assert_cell_identical(zero, zero.copy(), context="zero-vs-zero")


# ---------------------------------------------------------------------------
# B. Named edge cases
# ---------------------------------------------------------------------------


def test_holding_age_increments_even_on_global_emergency_bar():
    """B1: the tenure clock is bumped BEFORE the emergency short-circuit."""
    sequencer = PortfolioSequencer()
    ideal = pd.Series({"AAA": 0.20, "BBB": 0.10, "CASH": 0.70})
    current = pd.Series({"AAA": 0.30, "BBB": 0.05, "CASH": 0.65})
    holding_age = {"AAA": 0, "BBB": 0}
    last_buy_bar = {"AAA": 0, "BBB": -999}

    out = sequencer.sequence(
        ideal,
        current,
        ctx=PolicyContext(bar_index=7),
        risky_symbols=["AAA", "BBB"],
        symbols=["AAA", "BBB", "CASH"],
        thresh_t=pd.Series(0.02, index=["AAA", "BBB"]),
        holding_age=holding_age,
        last_buy_bar=last_buy_bar,
        cfg=PositionPolicyConfig(),
        cash_proxy="CASH",
        em_row=True,  # global emergency
    )

    # Both incumbents age by one bar despite nothing being evaluated.
    assert holding_age == {"AAA": 1, "BBB": 1}
    # The emergency pass-through trades nothing, so no buy ever stamps the clock.
    assert last_buy_bar == {"AAA": 0, "BBB": -999}
    pd.testing.assert_series_equal(out, ideal)

    # Same ordering at the per-symbol layer (the sequencer must not be the only
    # place it holds): a symbol with no holding resets its age to 0.
    decision = AssetPolicy().inertia_step(
        "AAA",
        pd.Series({"AAA": 0.0, "BBB": 0.10}),
        pd.Series({"AAA": 0.30, "BBB": 0.10}),
        0.02,
        PositionPolicyConfig(),
        ctx=PolicyContext(bar_index=3),
        holding_age={"AAA": 2, "BBB": 0},
        last_buy_bar={"AAA": 0, "BBB": -999},
        symbol_emergency=True,
        is_global_emergency=True,
    )
    assert decision.next_state is PositionState.FLAT
    assert decision.target_weight == 0.0  # ideal row passed through verbatim
    assert decision.detail["holding_age"] == 3


def test_global_emergency_returns_ideal_weights_verbatim():
    """B2: no tenure filtering, no buy/sell gating, no cash residual rewrite."""
    index = pd.bdate_range("2024-02-01", periods=2)
    # CASH is deliberately inconsistent with the risky sum (0.10 vs 0.30, then
    # 0.05 vs 0.10): a residual rewrite would silently overwrite both cells.
    daily = pd.DataFrame(
        {"AAA": [0.40, 0.00], "BBB": [0.30, 0.90], "CASH": [0.10, 0.05]}, index=index
    )

    got = kernel_apply_asset_inertia(
        daily, min_weight_change=0.02, min_holding_bars=5, emergency_mask=True
    )
    pd.testing.assert_frame_equal(got, daily)
    assert got["CASH"].iloc[1] == 0.05  # NOT 1 - (0.00 + 0.90) = 0.10
    assert got["AAA"].iloc[1] == 0.0  # a real exit passes, tenure notwithstanding
    assert got["BBB"].iloc[1] == 0.90  # a 0.30 -> 0.90 buy passes, capacity notwithstanding

    # Control: without the emergency, the same row is tenure-filtered and gated.
    plain = kernel_apply_asset_inertia(daily, min_weight_change=0.02, min_holding_bars=5)
    assert plain["AAA"].iloc[1] == 0.40  # mother-position sell locked by age
    assert plain["BBB"].iloc[1] == pytest.approx(0.60)  # buy capped by freed capacity
    assert plain["CASH"].iloc[1] == pytest.approx(0.0)  # residual rewritten


def test_scalar_emergency_and_series_emergency_global_vs_mixed():
    """B3: an all-True risky row is a GLOBAL emergency; a mixed row is per-symbol."""
    index = pd.bdate_range("2024-03-01", periods=2)
    daily = pd.DataFrame(
        {"AAA": [0.30, 0.0], "BBB": [0.30, 0.0], "CASH": [0.40, 1.0]}, index=index
    )

    # (a) constant scalar == constant per-row Series == all-True per-cell frame
    scalar = kernel_apply_asset_inertia(
        daily, min_weight_change=0.02, min_holding_bars=5, emergency_mask=True
    )
    row_series = kernel_apply_asset_inertia(
        daily,
        min_weight_change=0.02,
        min_holding_bars=5,
        emergency_mask=pd.Series([False, True], index=index),
    )
    all_true = kernel_apply_asset_inertia(
        daily,
        min_weight_change=0.02,
        min_holding_bars=5,
        emergency_mask=pd.DataFrame({"AAA": [False, True], "BBB": [False, True]}, index=index),
    )
    for frame in (scalar, row_series, all_true):
        pd.testing.assert_frame_equal(frame, daily)
        assert frame["CASH"].iloc[1] == 1.0  # no residual rewrite

    # (b) a MIXED per-symbol row is per-symbol: AAA's emergency bypasses the
    # tenure lock, BBB's sell is still locked.
    mixed = kernel_apply_asset_inertia(
        daily,
        min_weight_change=0.02,
        min_holding_bars=5,
        emergency_mask=pd.DataFrame({"AAA": [False, True], "BBB": [False, False]}, index=index),
    )
    assert mixed["AAA"].iloc[1] == 0.0
    assert mixed["BBB"].iloc[1] == 0.30  # suppressed by min_holding_bars
    assert mixed["CASH"].iloc[1] == pytest.approx(0.70)  # residual IS rewritten here


def test_asymmetric_sell_band_narrows_the_trim_hurdle():
    """B4: the `asymmetric_sell_ratio` band is a FRACTION of the threshold.

    `sell_thresh = thresh * ratio` (0.60 < 1), i.e. the opt-in band is TIGHTER
    than the symmetric one: a trim of 0.045 with `thresh=0.05` clears 0.03 and is
    executed when `asymmetric_sells=True`, and is frozen by the full 0.05 band
    when it is False. Both directions are pinned against the reference so the
    semantics cannot drift away from `apply_asset_inertia`.
    """
    index = pd.bdate_range("2024-04-01", periods=2)
    daily = pd.DataFrame({"AAA": [0.30, 0.255], "CASH": [0.70, 0.745]}, index=index)

    asymmetric = kernel_apply_asset_inertia(
        daily, min_weight_change=0.05, asymmetric_sells=True, asymmetric_sell_ratio=0.60
    )
    symmetric = kernel_apply_asset_inertia(daily, min_weight_change=0.05, asymmetric_sells=False)

    assert asymmetric["AAA"].iloc[1] == pytest.approx(0.255)  # trim executed
    assert symmetric["AAA"].iloc[1] == pytest.approx(0.30)  # trim blocked, weight frozen

    pd.testing.assert_frame_equal(
        asymmetric,
        apply_asset_inertia(
            daily, min_weight_change=0.05, asymmetric_sells=True, asymmetric_sell_ratio=0.60
        ),
    )
    pd.testing.assert_frame_equal(
        symmetric, apply_asset_inertia(daily, min_weight_change=0.05, asymmetric_sells=False)
    )


def test_entry_threshold_applies_only_to_new_positions():
    """B5: `entry_threshold` replaces the buy band only when `current <= 1e-4`."""
    index = pd.bdate_range("2024-05-01", periods=2)
    # NEW starts flat and wants 0.03 (< the 0.05 band); ADD is already held and
    # wants another 0.03 (< the same band).
    daily = pd.DataFrame(
        {"NEW": [0.0, 0.03], "ADD": [0.10, 0.13], "CASH": [0.90, 0.84]}, index=index
    )

    with_entry = kernel_apply_asset_inertia(
        daily, min_weight_change=0.05, entry_threshold=0.02
    )
    without_entry = kernel_apply_asset_inertia(daily, min_weight_change=0.05)

    assert with_entry["NEW"].iloc[1] == pytest.approx(0.03)  # new entry allowed
    assert with_entry["ADD"].iloc[1] == pytest.approx(0.10)  # add NOT allowed
    assert with_entry["CASH"].iloc[1] == pytest.approx(0.87)

    # Without the gate neither trades: the whole row is frozen at bar 0's weights.
    np.testing.assert_array_equal(
        without_entry.iloc[1].to_numpy(dtype=float),
        without_entry.iloc[0].to_numpy(dtype=float),
    )


def test_min_holding_bars_suppresses_sells_and_emergency_bypasses():
    """B6: (a) mother-position sell by age, (b) partial trim after a recent add."""
    # -- (a) mother-position sell: age 1 < min_holding_bars --------------------
    index_a = pd.bdate_range("2024-06-03", periods=2)
    exit_frame = pd.DataFrame({"AAA": [0.30, 0.0], "CASH": [0.70, 1.0]}, index=index_a)

    locked_exit = kernel_apply_asset_inertia(
        exit_frame, min_weight_change=0.02, min_holding_bars=3
    )
    assert locked_exit["AAA"].iloc[1] == pytest.approx(0.30)  # sell suppressed
    assert locked_exit["CASH"].iloc[1] == pytest.approx(0.70)  # frozen, no residual write

    unlocked_exit = kernel_apply_asset_inertia(
        exit_frame, min_weight_change=0.02, min_holding_bars=0
    )
    assert unlocked_exit["AAA"].iloc[1] == pytest.approx(0.0)  # control: exits

    emergency_exit = kernel_apply_asset_inertia(
        exit_frame,
        min_weight_change=0.02,
        min_holding_bars=3,
        emergency_mask=pd.Series([False, True], index=index_a),
    )
    assert emergency_exit["AAA"].iloc[1] == pytest.approx(0.0)  # emergency bypasses the lock

    # -- (b) partial trim after a recent add ----------------------------------
    index = pd.bdate_range("2024-06-03", periods=5)
    daily = pd.DataFrame(
        {
            "AAA": [0.10, 0.10, 0.25, 0.20, 0.15],
            "CASH": [0.90, 0.90, 0.75, 0.80, 0.85],
        },
        index=index,
    )

    locked = kernel_apply_asset_inertia(daily, min_weight_change=0.02, min_holding_bars=2)
    unlocked = kernel_apply_asset_inertia(daily, min_weight_change=0.02, min_holding_bars=0)
    # (b) bar 3 trims a position that was ADDED at bar 2: age is old enough
    # (3 >= 2) but `bar_index - last_buy_bar == 1 < 2`, so the partial trim is
    # suppressed. Bar 4 is 2 bars after the add and trims normally.
    assert locked["AAA"].iloc[2] == pytest.approx(0.25)
    assert locked["AAA"].iloc[3] == pytest.approx(0.25)  # suppressed trim
    assert locked["AAA"].iloc[4] == pytest.approx(0.15)  # released trim

    # Control: with no lock, the bar-3 trim executes.
    assert unlocked["AAA"].iloc[3] == pytest.approx(0.20)

    # Emergency on bar 3 bypasses the tenure suppression (and, being a uniform
    # row for the only risky symbol, is a global emergency -> ideal passthrough).
    emergency = pd.Series([False, False, False, True, False], index=index)
    bypassed = kernel_apply_asset_inertia(
        daily, min_weight_change=0.02, min_holding_bars=2, emergency_mask=emergency
    )
    assert bypassed["AAA"].iloc[3] == pytest.approx(0.20)

    # The suppressed branch must be labelled, never turned into a trade.
    decision = AssetPolicy().inertia_step(
        "AAA",
        pd.Series({"AAA": 0.0}),
        pd.Series({"AAA": 0.30}),
        0.02,
        PositionPolicyConfig(min_holding_bars=3),
        ctx=PolicyContext(bar_index=1),
        holding_age={"AAA": 1},
        last_buy_bar={"AAA": -999},
    )
    assert decision.action == PolicyAction.HOLD_FILTERED
    assert decision.blocking_reason is BlockingReason.TENURE_LOCKED
    assert decision.target_weight == pytest.approx(0.30)


def test_last_buy_bar_resets_only_on_a_full_exit():
    """B7: `last_buy_bar = -999` is a full-exit event, not a trim event."""
    sequencer = PortfolioSequencer()
    cfg = PositionPolicyConfig()
    thresh = pd.Series(0.02, index=["AAA", "BBB"])
    symbols = ["AAA", "BBB", "CASH"]
    risky = ["AAA", "BBB"]

    def step(ideal, current, last_buy_bar):
        return sequencer.sequence(
            pd.Series(ideal),
            pd.Series(current),
            ctx=PolicyContext(bar_index=9),
            risky_symbols=risky,
            symbols=symbols,
            thresh_t=thresh,
            holding_age={"AAA": 3, "BBB": 0},
            last_buy_bar=last_buy_bar,
            cfg=cfg,
            cash_proxy="CASH",
            em_row=None,
        )

    # Partial trim: 0.40 -> 0.20 keeps the tenure timestamp intact.
    trimmed = {"AAA": 5, "BBB": -999}
    out = step({"AAA": 0.20, "BBB": 0.0, "CASH": 0.80},
               {"AAA": 0.40, "BBB": 0.0, "CASH": 0.60}, trimmed)
    assert out["AAA"] == pytest.approx(0.20)
    assert trimmed["AAA"] == 5

    # Full exit: 0.40 -> 0.00 resets it so a re-entry starts a fresh clock.
    exited = {"AAA": 5, "BBB": -999}
    out = step({"AAA": 0.0, "BBB": 0.0, "CASH": 1.0},
               {"AAA": 0.40, "BBB": 0.0, "CASH": 0.60}, exited)
    assert out["AAA"] == 0.0
    assert exited["AAA"] == -999


def test_buy_priority_and_capacity_ceiling():
    """B8: largest `diff` buys first, the runner-up is written back unchanged."""
    sequencer = PortfolioSequencer()
    cfg = PositionPolicyConfig()
    thresh = pd.Series(0.02, index=["AAA", "BBB"])
    last_buy_bar = {"AAA": -999, "BBB": -999}

    out = sequencer.sequence(
        pd.Series({"AAA": 0.90, "BBB": 0.60, "CASH": 0.0}),  # diag: 0.40 / 0.30
        pd.Series({"AAA": 0.50, "BBB": 0.30, "CASH": 0.20}),
        ctx=PolicyContext(bar_index=9),
        risky_symbols=["AAA", "BBB"],
        symbols=["AAA", "BBB", "CASH"],
        thresh_t=thresh,
        holding_age={"AAA": 3, "BBB": 1},
        last_buy_bar=last_buy_bar,
        cfg=cfg,
        cash_proxy="CASH",
        em_row=None,
    )

    # avail_inc after sells is 1 - 0.80 = 0.20, so only the bigger diff fits.
    assert out["AAA"] == pytest.approx(0.70)
    assert last_buy_bar["AAA"] == 9
    # Runner-up: actual_inc = 0 < buy_thresh -> the OLD weight is written back
    # explicitly and no tenure stamp is taken.
    assert out["BBB"] == pytest.approx(0.30)
    assert last_buy_bar["BBB"] == -999
    # Total risky weight never exceeds 100%.
    assert float(out[["AAA", "BBB"]].sum()) == pytest.approx(1.0)
    assert out["CASH"] == pytest.approx(0.0)

    # Same ceiling through the wrapper, on an over-allocated (pre-deleverage)
    # ideal row: capacity is capped, cash fills the remainder.
    index = pd.bdate_range("2024-07-01", periods=2)
    daily = pd.DataFrame(
        {
            "AAA": [0.50, 0.90],
            "BBB": [0.30, 0.60],
            "CASH": [0.20, 0.00],
        },
        index=index,
    )
    got = kernel_apply_asset_inertia(daily, min_weight_change=0.02)
    assert float(got[["AAA", "BBB"]].sum(axis=1).max()) <= 1.0 + 1e-12
    assert got["AAA"].iloc[1] == pytest.approx(0.70)
    assert got["BBB"].iloc[1] == pytest.approx(0.30)


def test_cash_proxy_residual_only_when_column_exists():
    """B9: `cash_proxy` never injects a column; the residual is a rewrite."""
    index = pd.bdate_range("2024-08-01", periods=2)
    no_cash = pd.DataFrame({"AAA": [0.50, 0.20], "BBB": [0.50, 0.60]}, index=index)

    for proxy in ("CASH", "MISSING", None):
        got = kernel_apply_asset_inertia(no_cash, min_weight_change=0.02, cash_proxy=proxy)
        assert list(got.columns) == ["AAA", "BBB"]  # no column injected
        assert not got.isna().all(axis=None)

    # With the column present, the residual IS written (an inconsistent CASH
    # cell on the rebalance row is replaced by 1 - risky sum).
    with_cash = pd.DataFrame(
        {"AAA": [0.50, 0.20], "BBB": [0.30, 0.30], "CASH": [0.20, 0.45]}, index=index
    )
    got = kernel_apply_asset_inertia(with_cash, min_weight_change=0.02, cash_proxy="CASH")
    assert got["CASH"].iloc[1] == pytest.approx(0.50)  # 1 - (0.20 + 0.30), NOT the 0.45 ideal

    # A "MISSING" proxy takes the CASH column as an ordinary risky asset (so the
    # 0.45 ideal is honoured) and writes no cash residual of its own.
    got_missing = kernel_apply_asset_inertia(
        with_cash, min_weight_change=0.02, cash_proxy="MISSING"
    )
    assert list(got_missing.columns) == ["AAA", "BBB", "CASH"]
    assert got_missing["CASH"].iloc[1] == pytest.approx(0.45)


def test_sparse_projector_project_matches_where_changed():
    """B10: unchanged rows stay NaN (not 0.0); a changed real 0.0 stays 0.0."""
    index = pd.bdate_range("2024-09-02", periods=5)
    daily = pd.DataFrame(
        {
            "AAA": [0.50, 0.50, 0.00, 0.00, 0.30],
            "BBB": [0.50, 0.50, 0.90, 0.90, 0.00],
        },
        index=index,
    )

    changed = (daily != daily.shift(1)).any(axis=1)
    changed.iloc[0] = True
    expected = daily.where(changed)

    projected = SparseProjector().project(daily)
    pd.testing.assert_frame_equal(projected, expected)

    # Row 1 is byte-identical to row 0 -> NaN (no instruction), NOT 0.0.
    assert projected.iloc[1].isna().all()
    assert not (projected.iloc[1] == 0.0).any()
    # Row 2 changes AAA to exactly 0.0 -> that real exit survives verbatim.
    assert projected["AAA"].iloc[2] == 0.0
    assert not pd.isna(projected["AAA"].iloc[2])
    # Row 4 changes BBB to exactly 0.0 and AAA to 0.30 -> both survive.
    assert projected["BBB"].iloc[4] == 0.0
    assert projected["AAA"].iloc[4] == pytest.approx(0.30)
    # Row 0 is always materialised, even if it equals a zero row.
    assert not projected.iloc[0].isna().any()


def test_sparse_projector_write_never_fabricates_a_zero():
    """B11: the unique write-back rule -- `None` means "omit the row"."""
    projector = SparseProjector()

    def decision(action, target):
        return Decision(action=action, target_weight=target)

    # No-op actions with an unchanged target -> omit the row.
    assert projector.write(decision(PolicyAction.HOLD, 0.30), 0.30) is None
    assert projector.write(decision(PolicyAction.HOLD_FILTERED, 0.30), 0.30) is None
    assert projector.write(decision(PolicyAction.BUY, 0.30), 0.30) is None
    assert projector.write(decision(PolicyAction.SELL, 0.30), 0.30) is None

    # A real SELL to exactly 0.0 is an explicit exit.
    assert projector.write(decision(PolicyAction.SELL, 0.0), 0.25) == 0.0
    assert projector.write(decision(PolicyAction.SELL, 0.0), None) == 0.0

    # Changed targets are written verbatim (BUY and SELL).
    assert projector.write(decision(PolicyAction.BUY, 0.30), 0.20) == pytest.approx(0.30)
    assert projector.write(decision(PolicyAction.BUY, 0.20), None) == pytest.approx(0.20)
    assert projector.write(decision(PolicyAction.SELL, 0.15), 0.25) == pytest.approx(0.15)

    # Never-traded actions must not become a phantom `0.0` anchor, even at 0.0.
    assert projector.write(decision(PolicyAction.CANCEL_PENDING, 0.0), 0.25) is None
    assert projector.write(decision(PolicyAction.CANCEL_PENDING, 0.0), None) is None
    assert projector.write(decision(PolicyAction.COOLDOWN_BLOCKED, 0.0), 0.25) is None
    assert projector.write(decision(PolicyAction.COOLDOWN_BLOCKED, 0.0), None) is None

    # A flat, never-held symbol stays omitted.
    assert projector.write(decision(PolicyAction.HOLD, 0.0), None) is None


# ---------------------------------------------------------------------------
# C. Invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", list(range(12)))
def test_invariants_long_only_and_capacity(seed):
    """Long-only, non-negative, risky weight <= 100% (design invariant 1)."""
    daily, kwargs, _ = _build_case(seed)
    got = kernel_apply_asset_inertia(daily, **kwargs)
    values = got.to_numpy(dtype=float)

    assert np.isfinite(values).all(), f"seed={seed}: non-finite weight in kernel output"
    assert (values >= 0.0).all(), f"seed={seed}: negative weight in kernel output"

    cash_proxy = kwargs["cash_proxy"]
    risky = [c for c in got.columns if c != cash_proxy]
    risky_sums = got[risky].sum(axis=1)
    assert (risky_sums <= 1.0 + 1e-12).all(), (
        f"seed={seed}: risky weight exceeds 100% on {risky_sums[risky_sums > 1.0 + 1e-12].index.tolist()}"
    )


def test_decision_rejects_nan_target_weight():
    """Invariant 2: a NaN target is a programming error, raised at construction."""
    with pytest.raises(ValueError, match="NaN"):
        Decision(action=PolicyAction.BUY, target_weight=float("nan"))
    with pytest.raises(ValueError, match="NaN"):
        Decision(action=PolicyAction.HOLD_FILTERED, target_weight=np.nan)
    # A finite target is accepted and normalised to a plain float.
    decision = Decision(action=PolicyAction.BUY, target_weight=np.float64(0.25))
    assert isinstance(decision.target_weight, float) and decision.target_weight == 0.25


def test_asset_policy_step_does_not_mutate_caller_state():
    """Invariant 3 plumbing: a stop exit returns a SELL, the input is untouched."""
    state = AssetState(
        symbol="AAA",
        state=PositionState.HELD_ACTIVE,
        held_bars=1,
        entry_price=10.0,
        peak_price=10.5,
        current_weight=0.30,
    )
    decision = AssetPolicy().step(
        state,
        0.30,
        PolicyContext(price=9.0, is_emergency_halt=True),  # forced circuit-breaker exit
        PositionPolicyConfig(),
    )
    assert decision.action == PolicyAction.SELL
    assert decision.target_weight == 0.0
    assert decision.next_state is PositionState.COOLDOWN
    # Caller-owned state was copied, never mutated.
    assert state.held_bars == 1 and state.current_weight == 0.30
    assert decision.detail["state_after"]["held_bars"] == 0


@pytest.mark.parametrize("seed", list(range(8)))
def test_reapplying_kernel_to_its_own_output(seed):
    """Invariant 5: re-application is stable, and matches the reference's own 2nd pass.

    Disclosed (see module docstring): re-application is NOT bit-exact, because a
    committed buy writes `current + (ideal - current)`, which differs from
    `ideal` by up to 1 ULP. That is a property of `apply_asset_inertia` itself
    (asserted here by comparing the two second passes bit-for-bit). What MUST
    hold, and is asserted: the same NaN/zero structure and a numerically
    identical frame (<= 1e-12), so no position is silently opened or closed and
    no phantom rebalance row appears on a re-run.
    """
    rng = np.random.default_rng(seed)
    daily = _make_daily_frame(
        rng,
        n_rows=int(rng.integers(30, 61)),
        n_risky=int(rng.integers(6, 9)),
        sparseish=(seed % 2 == 0),
        with_cash=True,
    )
    kwargs = {
        "min_weight_change": float(rng.choice([0.02, 0.05, 0.10])),
        "cash_proxy": "CASH",
        "asymmetric_sells": bool(rng.integers(0, 2)),
        "entry_threshold": [None, 0.05][int(rng.integers(0, 2))],
        "min_holding_bars": int(rng.choice([0, 2, 5])),
    }
    context = f"seed={seed} kwargs={_kw_summary(kwargs)}"

    first = kernel_apply_asset_inertia(daily, **kwargs)
    second = kernel_apply_asset_inertia(first, **kwargs)

    # Structure is exactly preserved: same NaN positions and same close cells.
    assert (first.isna() == second.isna()).all().all(), f"{context}: NaN structure changed"
    assert ((first.to_numpy(dtype=float) == 0.0) == (second.to_numpy(dtype=float) == 0.0)).all(), (
        f"{context}: a zero/close cell appeared or vanished on re-application"
    )
    np.testing.assert_allclose(
        second.to_numpy(dtype=float), first.to_numpy(dtype=float), rtol=0.0, atol=1e-12,
        err_msg=context,
    )

    # The real gate: the second pass is bit-identical to the reference's second pass.
    _assert_cell_identical(apply_asset_inertia(first, **kwargs), second, context=context)


# ---------------------------------------------------------------------------
# D. "No callers" guard -- this phase must stay dead code
# ---------------------------------------------------------------------------


def test_kernel_has_no_callers_in_templates_or_blend_base():
    """M4.1 ships the kernel as DEAD CODE: neither hot module may import it."""
    import re

    import_line = re.compile(r"^\s*(?:from\s+\S+\s+import\b|import\b)")
    offenders = []
    for relative in ("common/allocation_templates.py", "pipeline/research/blend/blend_base.py"):
        path = os.path.join(_PROJECT_ROOT, relative)
        assert os.path.isfile(path), f"expected guard target to exist: {relative}"
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if "position_policy" not in line:
                    continue
                if import_line.match(line):
                    offenders.append(f"{relative}:{number}: {line.strip()}")
    assert not offenders, (
        "M4.1 requires zero callers; these modules import the kernel: " + "; ".join(offenders)
    )
