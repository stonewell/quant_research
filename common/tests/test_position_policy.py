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

The M4.1 kernel shipped as DEAD CODE. M4.2 flipped both call sites, and commit 2
(`BASE_VERSION 1.9.0`) made the kernel the DEFAULT: `blend_base`'s inertia step
selects `PortfolioSequencer.sequence` unless `blend_use_policy_kernel=False` is
passed explicitly, and `common/allocation_templates.py::apply_asset_inertia`
delegates to `kernel_apply_asset_inertia` unconditionally (commit 1b).
`test_kernel_callers_are_guarded_by_opt_in_flag` pins the remaining invariants:
on the templates side a static import scan plus behavioural no-delegation checks
(the per-bar `_step_asset_inertia` and the inline `_sparse_from_daily` compression
must NOT route through the kernel).

The blend half is a BEHAVIOURAL call-path spy, deliberately not a source
substring check (this file previously did the latter, and it was vacuous: the
kernel is bit-identical to `_step_asset_inertia`, so forcing
`use_policy_kernel = True` or deleting the `if use_policy_kernel:` branch left
every test in the repo green -- output equivalence cannot see a silent default
flip). The spy patches `blend_base._step_asset_inertia` and
`PortfolioSequencer.sequence` and runs two real `generate_weights` calls: one on
the DEFAULT config (kernel path since 1.9.0) and one with the EXPLICIT opt-out
`blend_use_policy_kernel=False` (legacy path). Each arm must take its own route
and never the other's -- that is what proves the legacy path is still present and
selectable, i.e. that the differential harnesses' OFF arm did not become a
self-comparison of the kernel.

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

import importlib.util
import subprocess
import tempfile

import numpy as np
import pandas as pd
import pytest

# Ensure project root is in sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REPO_ROOT = _PROJECT_ROOT  # this file lives at <repo>/common/tests/, so the root IS the repo
for _root in (_PROJECT_ROOT,):
    if _root not in sys.path:
        sys.path.insert(0, _root)

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

# The commit immediately BEFORE M4.2 phase-2 / commit 1b, i.e. the last revision
# in which `common/allocation_templates.py::apply_asset_inertia` still ran its own
# per-bar loop. PINNED for the same reason as
# `pipeline/research_strategy/tests/test_blend_policy_kernel.py`: now that the
# delegation has landed, `HEAD:common/allocation_templates.py` IS the delegated
# file, so a `HEAD:`-based reference would compare the kernel against itself and
# keep passing no matter what the kernel did. The pin is also what makes the
# `position_policy`-free assertion below possible.
_LEGACY_TEMPLATES_COMMIT = "ab82318"
_LEGACY_TEMPLATES_PATH = "common/allocation_templates.py"
_LEGACY_MODULE = None


def _legacy_apply_asset_inertia():
    """The PRE-delegation `apply_asset_inertia`, loaded from a pinned git blob."""
    global _LEGACY_MODULE
    if _LEGACY_MODULE is not None:
        return _LEGACY_MODULE.apply_asset_inertia

    try:
        proc = subprocess.run(
            ["git", "show", f"{_LEGACY_TEMPLATES_COMMIT}:{_LEGACY_TEMPLATES_PATH}"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        # NOT a skip: this is the primary bit-identity gate. A skip would let the
        # whole 240-seed differential harness disappear silently.
        pytest.fail(
            f"cannot read the pre-delegation allocation_templates blob "
            f"({_LEGACY_TEMPLATES_COMMIT}:{_LEGACY_TEMPLATES_PATH}) from git: {exc}"
        )

    src = proc.stdout
    assert src.strip(), "git show returned an empty allocation_templates source"
    assert "position_policy" not in src, (
        "the pinned blob already imports the policy kernel; the differential gate "
        "would be comparing the delegation against itself (vacuous)"
    )

    tmpdir = tempfile.mkdtemp(prefix="m4_2_templates_legacy_")
    module_path = os.path.join(tmpdir, "allocation_templates_legacy.py")
    with open(module_path, "w", encoding="utf-8") as fh:
        fh.write(src)

    spec = importlib.util.spec_from_file_location("_allocation_templates_legacy", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _LEGACY_MODULE = module
    return module.apply_asset_inertia



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
    """M4.1 gate: 240 seeds, whole matrix of threshold/cash/emergency/tenure shapes.

    Since M4.2 phase-2 / commit 1b, `apply_asset_inertia` IS a delegation to
    `kernel_apply_asset_inertia`, so comparing the two directly would be
    tautological (and would stay green even if the delegation changed the
    numbers). The reference is therefore the PRE-delegation implementation, read
    from the pinned commit `_LEGACY_TEMPLATES_COMMIT`, and the live delegated
    function is compared to it as well -- three-way, same cells.
    """
    daily, kwargs, ref_kwargs = _build_case(seed)
    context = f"seed={seed} kwargs={_kw_summary(kwargs)}"

    ref = _legacy_apply_asset_inertia()(daily, **ref_kwargs)
    got = kernel_apply_asset_inertia(daily, **kwargs)
    delegated = apply_asset_inertia(daily, **ref_kwargs)

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

    # The production entry point (now a delegation) must be indistinguishable from
    # the pinned legacy function -- this is the bit-identity the flip relies on.
    assert (ref.isna() == delegated.isna()).all().all(), (
        f"{context}: the delegated `apply_asset_inertia` changed the NaN footprint "
        f"-- {_first_mismatch(ref, delegated)}"
    )
    try:
        np.testing.assert_array_equal(ref.to_numpy(), delegated.to_numpy())
    except AssertionError as exc:  # pragma: no cover - only on a real defect
        raise AssertionError(
            f"{context}: the delegated `apply_asset_inertia` changed values -- "
            f"{_first_mismatch(ref, delegated)}\n{exc}"
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
# B12-B15. NaN cells -- the `Decision` invariant vs. the sparse NaN contract
# ---------------------------------------------------------------------------
#
# The 240-seed harness above is VACUOUS on this input class: `_make_daily_frame`
# only emits clipped/rounded finite values, so "NaN is never 0.0" is never
# actually exercised. These tests are the missing differential.
#
# The defect they pin: `Decision.__post_init__` raises on a NaN `target_weight`
# (design invariant 2, kept deliberately -- the live ticket path relies on it),
# while the reference `_step_asset_inertia` propagates a NaN cell silently and
# correctly (every comparison against NaN is False, so the symbol is never a
# sell/buy candidate and its cell is never rewritten). Two reachable inputs made
# the kernel raise where the reference propagates:
#   * a NaN `current_w[s]` -> the non-trading fall-through `target_weight=current`;
#     reachable from row 0 (a late-listing symbol with no history yet), because
#     `current_w` is seeded from `daily.iloc[0]` and then carried forward;
#   * a NaN `ideal_w[s]` on a global-emergency bar -> the pass-through
#     `target_weight=ideal`.
# `apply_asset_inertia` delegates to the kernel UNCONDITIONALLY (commit 176a36d),
# so this was live on the production path even with
# `blend_use_policy_kernel=False`.


def _nan_daily_frame(nan_cells, *, seed=20211105, n_rows=45, n_risky=7):
    """A realistic dense frame with exactly the NaN cells named in `nan_cells`.

    `nan_cells` is a sequence of `(row_position, symbol)` pairs. Every other cell
    is finite (asserted), so a "one NaN cell in a risky symbol" shape is
    unambiguous and a defect cannot hide behind a second missing value. `CASH`
    is never a target: it is the residual, not a position.
    """
    rng = np.random.default_rng(seed)
    daily = _make_daily_frame(
        rng, n_rows=n_rows, n_risky=n_risky, sparseish=True, with_cash=True
    )
    assert not daily.isna().to_numpy().any(), "the base frame is finite by construction"
    for row, symbol in nan_cells:
        assert symbol in daily.columns and symbol != "CASH"
        daily.iloc[row, daily.columns.get_loc(symbol)] = np.nan
    return daily


def _assert_nan_cell_forced_to_nan(daily, out, *, context, impl):
    """No input NaN cell may be rewritten to `0.0` (a silent liquidation).

    Kept separate from `_assert_cell_identical` so a guard that emits `0.0`
    instead of `NaN` fails on THIS assertion (NaN vs 0.0), not merely as a
    generic value diff.
    """
    nan_in = daily.isna().to_numpy()
    assert nan_in.any(), f"{context}: the frame carries no NaN cell (vacuous)"
    zero_out = out.to_numpy(dtype=float) == 0.0
    violations = np.argwhere(nan_in & zero_out)
    assert not len(violations), (
        f"{context}: {impl} rewrote a NaN input cell as 0.0 (a silent liquidation, "
        f"NOT 'no instruction') at {[tuple(x) for x in violations[:5]]}"
    )
    assert out.isna().to_numpy().any(), (
        f"{context}: {impl} dropped every NaN cell -- NaN must survive as NaN"
    )


def test_kernel_propagates_nan_cells_like_reference():
    """Three-way cell identity on a dense frame with ONE NaN cell in a risky symbol.

    Pinned pre-delegation reference vs. `kernel_apply_asset_inertia` vs. the live
    delegated `apply_asset_inertia`: NaN positions equal, real `0.0` positions
    equal, values exactly equal, and no NaN cell turned into `0.0`.
    """
    daily = _nan_daily_frame([(0, "SYM02")])
    kwargs = {
        "min_weight_change": 0.02,
        "cash_proxy": "CASH",
        "min_holding_bars": 2,
        "asymmetric_sells": True,
        "entry_threshold": 0.05,
    }
    context = "nan-current (single NaN cell, row 0)"

    assert int(daily.isna().to_numpy().sum()) == 1, "exactly one NaN cell, by construction"

    ref = _legacy_apply_asset_inertia()(daily, **kwargs)
    got = kernel_apply_asset_inertia(daily, **kwargs)
    delegated = apply_asset_inertia(daily, **kwargs)

    # The run is not a pass-through: the reference rewrote at least one finite cell.
    finite = (daily.notna() & ref.notna()).to_numpy()
    assert (ref.to_numpy(dtype=float)[finite] != daily.to_numpy(dtype=float)[finite]).any(), (
        f"{context}: the reference changed nothing -- the case would be vacuous"
    )

    _assert_cell_identical(ref, got, context=f"{context}: kernel vs pinned reference")
    _assert_cell_identical(
        ref, delegated, context=f"{context}: delegated vs pinned reference"
    )
    _assert_nan_cell_forced_to_nan(daily, got, context=context, impl="kernel")
    _assert_nan_cell_forced_to_nan(daily, delegated, context=context, impl="delegated")

    # `0.0` positions are identical too (a real exit is not a missing instruction).
    assert ((ref.to_numpy(dtype=float) == 0.0) == (got.to_numpy(dtype=float) == 0.0)).all()
    assert ((ref.to_numpy(dtype=float) == 0.0) == (delegated.to_numpy(dtype=float) == 0.0)).all()

    # Structural expectation: the NaN-looking column stays NaN for the whole
    # frame, because a NaN current can never clear a sell/buy threshold.
    assert got["SYM02"].isna().all(), context
    assert delegated["SYM02"].isna().all(), context


def test_kernel_nan_in_ideal_only():
    """Branch 1 of 2: the global-emergency pass-through (`target_weight=ideal`).

    No NaN anywhere in row 0, so `current_w` is finite at the bar in question;
    only `ideal_w` is NaN, on the one bar the mask marks as an all-halted
    (global) emergency. The reference returns `ideal_w.copy()` (NaN included);
    the kernel used to raise from `Decision.__post_init__`.
    """
    daily = _nan_daily_frame([(6, "SYM02")])
    emergency = pd.Series(False, index=daily.index)
    emergency.iloc[6] = True  # a per-date bool row == "global flag" for that bar
    kwargs = {
        "min_weight_change": 0.02,
        "cash_proxy": "CASH",
        "emergency_mask": emergency,
    }
    context = "nan-ideal (global-emergency pass-through)"

    assert int(daily.isna().to_numpy().sum()) == 1, "exactly one NaN cell, by construction"
    assert daily.iloc[0].notna().all(), "row 0 must stay finite: copy the ideal branch"

    ref = _legacy_apply_asset_inertia()(daily, **kwargs)
    got = kernel_apply_asset_inertia(daily, **kwargs)
    delegated = apply_asset_inertia(daily, **kwargs)

    _assert_cell_identical(ref, got, context=f"{context}: kernel vs pinned reference")
    _assert_cell_identical(
        ref, delegated, context=f"{context}: delegated vs pinned reference"
    )
    _assert_nan_cell_forced_to_nan(daily, got, context=context, impl="kernel")
    _assert_nan_cell_forced_to_nan(daily, delegated, context=context, impl="delegated")

    # The NaN appears exactly at the pass-through bar and is carried forward as a
    # NaN current afterwards (never as a 0.0 exit).
    assert got["SYM02"].iloc[1:6].notna().all(), context
    assert got["SYM02"].iloc[6:].isna().all(), context
    assert delegated["SYM02"].iloc[6:].isna().all(), context


def test_kernel_nan_in_current_only():
    """Branch 2 of 2: the non-trading fall-through (`target_weight=current`).

    NaN only in `current` (seeded from row 0, carried by the column-wise chain);
    no emergency mask at all, so `inertia_step` reaches the final
    `HOLD_FILTERED` return -- the exact branch the review named.
    """
    daily = _nan_daily_frame([(0, "SYM04")], seed=7, n_rows=40)
    kwargs = {
        "min_weight_change": 0.05,
        "cash_proxy": "CASH",
        "min_holding_bars": 7,
    }
    context = "nan-current only (fall-through)"

    assert int(daily.isna().to_numpy().sum()) == 1, "exactly one NaN cell, by construction"
    assert daily["SYM04"].isna().sum() == 1 and daily["SYM04"].isna().iloc[0], (
        "the NaN must be in the row-0 seed of `current_w`"
    )

    ref = _legacy_apply_asset_inertia()(daily, **kwargs)
    got = kernel_apply_asset_inertia(daily, **kwargs)
    delegated = apply_asset_inertia(daily, **kwargs)

    _assert_cell_identical(ref, got, context=f"{context}: kernel vs pinned reference")
    _assert_cell_identical(
        ref, delegated, context=f"{context}: delegated vs pinned reference"
    )
    _assert_nan_cell_forced_to_nan(daily, got, context=context, impl="kernel")
    _assert_nan_cell_forced_to_nan(daily, delegated, context=context, impl="delegated")

    # Direct, symbol-level proof of the fall-through: `inertia_step` must still
    # refuse to build the NaN `Decision` itself (that is why the guard is at the
    # sequencer/panel layer, not inside the per-symbol step).
    with pytest.raises(ValueError, match="NaN"):
        AssetPolicy().inertia_step(
            "SYM04",
            pd.Series({"SYM04": np.nan, "CASH": 0.5}),
            pd.Series({"SYM04": np.nan, "CASH": 0.5}),
            0.05,
            PositionPolicyConfig(min_holding_bars=7),
            ctx=PolicyContext(bar_index=1),
            holding_age={"SYM04": 0},
            last_buy_bar={"SYM04": -999},
        )


def test_kernel_nan_in_current_and_ideal_both():
    """Both NaN sources in one frame: NaN `current` from row 0 and NaN `ideal`
    on the global-emergency bar. Still cell-identical, still no `0.0`."""
    daily = _nan_daily_frame([(0, "SYM02"), (6, "SYM02")], seed=13, n_rows=36)
    emergency = pd.Series(False, index=daily.index)
    emergency.iloc[6] = True
    kwargs = {
        "min_weight_change": 0.02,
        "cash_proxy": "CASH",
        "emergency_mask": emergency,
        "asymmetric_sells": True,
    }
    context = "nan-current + nan-ideal"

    assert int(daily.isna().to_numpy().sum()) == 2, "exactly two NaN cells, by construction"

    ref = _legacy_apply_asset_inertia()(daily, **kwargs)
    got = kernel_apply_asset_inertia(daily, **kwargs)
    delegated = apply_asset_inertia(daily, **kwargs)

    _assert_cell_identical(ref, got, context=f"{context}: kernel vs pinned reference")
    _assert_cell_identical(
        ref, delegated, context=f"{context}: delegated vs pinned reference"
    )
    _assert_nan_cell_forced_to_nan(daily, got, context=context, impl="kernel")
    _assert_nan_cell_forced_to_nan(daily, delegated, context=context, impl="delegated")


@pytest.mark.parametrize("case", ["nan_current", "nan_ideal", "nan_both"])
def test_kernel_nan_does_not_raise(case):
    """The precise regression: these inputs used to raise `ValueError`.

    Before the fix both `kernel_apply_asset_inertia` (flag-independent, so also
    through the delegated `apply_asset_inertia`) failed with
    `ValueError: Decision.target_weight must never be NaN ...`.
    """
    seed = {"nan_current": 20211105, "nan_ideal": 20211105, "nan_both": 13}[case]
    cells = {
        "nan_current": [(0, "SYM02")],
        "nan_ideal": [(6, "SYM02")],
        "nan_both": [(0, "SYM02"), (6, "SYM02")],
    }[case]
    daily = _nan_daily_frame(cells, seed=seed, n_rows=40)

    emergency = None
    if case in ("nan_ideal", "nan_both"):
        emergency = pd.Series(False, index=daily.index)
        emergency.iloc[6] = True
    kwargs = {"min_weight_change": 0.02, "cash_proxy": "CASH", "emergency_mask": emergency}

    for impl, fn in (
        ("pinned reference", _legacy_apply_asset_inertia()),
        ("kernel", kernel_apply_asset_inertia),
        ("delegated", apply_asset_inertia),
    ):
        out = fn(daily, **kwargs)  # must NOT raise
        assert out.index.equals(daily.index) and out.columns.equals(daily.columns), (
            f"{case}: {impl} did not complete the run"
        )
        assert out.shape == daily.shape, f"{case}: {impl} changed the frame shape"

    # The invariant that forces the guard to live at the panel layer is still
    # armed -- the NaN is FILTERED OUT before a `Decision` exists, never allowed in.
    with pytest.raises(ValueError, match="NaN"):
        Decision(action=PolicyAction.HOLD_FILTERED, target_weight=float("nan"))


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

    # The real gate: the second pass is bit-identical to the LEGACY reference's
    # second pass (pinned pre-delegation source; `apply_asset_inertia` itself is
    # now the delegation, so using it here would be tautological).
    _assert_cell_identical(_legacy_apply_asset_inertia()(first, **kwargs), second, context=context)


# ---------------------------------------------------------------------------
# D. Caller guard -- exactly one opt-in call site
# ---------------------------------------------------------------------------


def _blend_universe(n_days: int = 200) -> dict:
    """Deterministic synthetic blend universe (offline; `BIL` is the cash proxy)."""
    from common.testing import make_ohlcv_from_closes

    dates = pd.bdate_range("2020-01-01", periods=n_days)
    t = np.arange(n_days)
    universe = {
        "601225.SH": make_ohlcv_from_closes(100.0 + 0.15 * t + 5.0 * np.sin(2 * np.pi * t / 25)),
        "601857.SH": make_ohlcv_from_closes(100.0 + 0.08 * t + 4.0 * np.cos(2 * np.pi * t / 30)),
        "601872.SH": make_ohlcv_from_closes(100.0 - 0.05 * t + 3.0 * np.sin(2 * np.pi * t / 20)),
        "BIL": make_ohlcv_from_closes(np.full(n_days, 100.0)),
    }
    for frame in universe.values():
        frame.index = dates
    return universe


def test_kernel_callers_are_guarded_by_opt_in_flag(monkeypatch):
    """M4.2 phase 2 (commit 2, `BASE_VERSION 1.9.0`): the kernel is the DEFAULT,
    the legacy path is still PRESENT and still explicitly selectable.

    History: M4.1 shipped the kernel as dead code and this guard asserted that
    BOTH `common/allocation_templates.py` and `pipeline/research/blend/blend_base.py`
    were import-free. Each phase then narrowed (never dropped) the guard:

      * `common/allocation_templates.py` -- phase 2 / commit 1b makes this real:
        the module DOES now import the kernel, but ONLY `kernel_apply_asset_inertia`,
        ONLY for `apply_asset_inertia`, and `apply_asset_inertia` must be exactly
        one delegation call. `_step_asset_inertia` (still the blend engine's
        flag-off branch) and `_sparse_from_daily` (the inline dense->sparse
        compression) must stay NON-delegating -- asserted behaviourally with spies,
        because a source substring would not notice `_step_asset_inertia` being
        re-pointed at the kernel;
      * `pipeline/research/blend/blend_base.py` -- MAY import it. Since commit 2
        (1.9.0) the DEFAULT inertia path is the kernel (`PortfolioSequencer.sequence`)
        and the explicit opt-out `blend_use_policy_kernel=False` must still run the
        legacy `_step_asset_inertia` step. Asserted BEHAVIOURALLY (a call-path spy),
        because the kernel is bit-identical to the legacy step
        (`max_abs_diff == 0.0`): output comparison is structurally incapable of
        noticing a default flip, and a source-substring check cannot notice the
        branch being neutralised (`use_policy_kernel = True`, or
        `if use_policy_kernel:` -> `if True:`). Dropping the opt-out arm instead of
        re-scoping it would let the legacy branch rot away silently -- and would
        turn every "flag OFF vs ON" differential into the kernel compared with
        itself.

    M4.2 phase 2 / commit 1a adds ONE projection delegation, and it is NOT part of
    the flag: `blend_base` now compresses via `SparseProjector.project` (a literal
    transcription of `_sparse_from_daily`'s compression branch) unconditionally,
    because the step has no behavioural knob to gate -- it is a pure dense->sparse
    projection, not the `sequence` call path. It is pinned below behaviourally in
    BOTH flag states, so a future refactor cannot quietly re-route it back through
    `_sparse_from_daily` (or wire `SparseProjector.write`, which belongs to M4.3).
    """
    import re

    import pipeline.research.blend.blend_base as blend_base
    from pipeline.research.blend import GenericBlendStrategy

    import_line = re.compile(r"^\s*(?:from\s+\S+\s+import\b|import\b)")

    # (a) `common/allocation_templates.py`: the kernel is imported ONLY for
    # `apply_asset_inertia`, and that function is exactly one delegation call.
    #
    # Static half: the single `position_policy` import must name
    # `kernel_apply_asset_inertia` and nothing else -- an import of `AssetPolicy`,
    # `SparseProjector` or `_sparse_from_daily`'s replacement here would mean a
    # second, unguarded call site.
    templates_path = os.path.join(_PROJECT_ROOT, "common/allocation_templates.py")
    assert os.path.isfile(templates_path), "expected guard target to exist"
    offenders = []
    with open(templates_path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if "position_policy" not in line:
                continue
            if import_line.match(line):
                offenders.append(f"common/allocation_templates.py:{number}: {line.strip()}")
    assert len(offenders) == 1, (
        "expected exactly ONE `common.position_policy` import in "
        f"common/allocation_templates.py (for `apply_asset_inertia`), got {offenders}"
    )
    assert "kernel_apply_asset_inertia" in offenders[0] and "{" not in offenders[0], (
        "common/allocation_templates.py must import ONLY `kernel_apply_asset_inertia` "
        f"from the kernel module; got: {offenders[0]}"
    )

    import common.allocation_templates as templates

    kernel_calls = {"n": 0}
    original_kernel = templates.kernel_apply_asset_inertia

    def spy_kernel(*args, **kwargs):
        kernel_calls["n"] += 1
        return original_kernel(*args, **kwargs)

    monkeypatch.setattr(templates, "kernel_apply_asset_inertia", spy_kernel)

    rng = np.random.default_rng(4242)
    daily = pd.DataFrame(
        rng.random((12, 4)) * 0.25,
        index=pd.bdate_range("2021-01-04", periods=12),
        columns=["A", "B", "C", "CASH"],
    )
    # `_step_asset_inertia` is SINGLE-BAR and must not route through the kernel --
    # it is still the blend engine's flag-off branch and its per-bar call shape is
    # what makes that branch bit-identical to the pre-M4.2 code.
    kernel_calls["n"] = 0
    templates._step_asset_inertia(
        t=1,
        ideal_w=daily.iloc[1],
        current_w=daily.iloc[0],
        risky_symbols=["A", "B", "C"],
        symbols=["A", "B", "C", "CASH"],
        thresh_t=pd.Series({"A": 0.02, "B": 0.02, "C": 0.02}),
        holding_age={"A": 1, "B": 1, "C": 1},
        last_buy_bar={"A": 0, "B": 0, "C": 0},
        cash_proxy="CASH",
        em_row=None,
    )
    assert kernel_calls["n"] == 0, (
        f"`_step_asset_inertia` called the kernel {kernel_calls['n']} times: it must "
        "stay the standalone per-bar step (blend_base's flag-off branch uses it directly)"
    )

    # `_sparse_from_daily` keeps its INLINE compression -- it must not become a
    # `SparseProjector.project` alias (that projection is wired in blend_base only).
    project_calls = {"n": 0}
    original_project_only = SparseProjector.project

    def spy_project_only(self, frame):
        project_calls["n"] += 1
        return original_project_only(self, frame)

    monkeypatch.setattr(SparseProjector, "project", spy_project_only)
    sparse_input = daily.copy()
    sparse_input.iloc[3] = sparse_input.iloc[2]  # an unchanged day MUST project to NaN
    sparse = templates._sparse_from_daily(sparse_input)
    assert project_calls["n"] == 0, (
        f"`_sparse_from_daily` called `SparseProjector.project` {project_calls['n']} times: "
        "the helper must keep its own inline compression"
    )
    assert sparse.iloc[3].isna().all(), (
        "the unchanged day was not projected to NaN -- the non-vacuity guard for the "
        "inline compression is broken"
    )
    monkeypatch.setattr(SparseProjector, "project", original_project_only)

    # `apply_asset_inertia` delegates EXACTLY ONCE per call (not once per bar).
    kernel_calls["n"] = 0
    templates.apply_asset_inertia(daily, min_weight_change=0.02, cash_proxy="CASH")
    assert kernel_calls["n"] == 1, (
        f"`apply_asset_inertia` called the kernel {kernel_calls['n']} times for one "
        "panel: the panel loop must be delegated ONCE, not re-entered per bar"
    )
    monkeypatch.setattr(templates, "kernel_apply_asset_inertia", original_kernel)

    # (a2) phase-2 commit 1a: sparse projection is delegated, flag-independently.
    assert not hasattr(blend_base, "_sparse_from_daily"), (
        "blend_base still imports `_sparse_from_daily` -- the projection step was "
        "supposed to move to `SparseProjector.project` (and the import dropped, it "
        "had no other use in the file)"
    )
    projections = {"off": 0, "on": 0}
    projections_written = {"off": 0, "on": 0}
    original_project = SparseProjector.project
    original_write = SparseProjector.write

    def spy_project(self, daily):
        projections[spy_project.state] += 1
        return original_project(self, daily)

    def spy_write(self, decision, previous_target):
        projections_written[spy_write.state] += 1
        return original_write(self, decision, previous_target)

    spy_project.state = "off"
    spy_write.state = "off"
    monkeypatch.setattr(SparseProjector, "project", spy_project)
    monkeypatch.setattr(SparseProjector, "write", spy_write)

    # (b) the blend call site delegates by CALL PATH, not by source text.
    assert hasattr(blend_base, "_step_asset_inertia"), (
        "blend_base no longer imports the legacy `_step_asset_inertia` step -- the "
        "explicit opt-out path would no longer be the pre-M4.2 code"
    )
    calls = {"legacy": 0, "kernel": 0, "delegated_helper": 0}
    original_step = blend_base._step_asset_inertia
    original_sequence = PortfolioSequencer.sequence

    def spy_step(**kwargs):
        calls["legacy"] += 1
        return original_step(**kwargs)

    def spy_sequence(self, *args, **kwargs):
        # PROVENANCE, because since commit 1b the kernel is reachable WITHOUT the
        # flag: `common.allocation_templates.apply_asset_inertia` now delegates to
        # `kernel_apply_asset_inertia`, and several sub-strategies call it
        # (`worldquant_alpha_strategy`, `gtja191_factors`, `chan_advanced_strategies`,
        # `timing_aspects`, ...). Those calls ARE the kernel running, unconditionally
        # and by design. What this guard still owns is the BLEND ENGINE's own per-bar
        # inertia step, so the counter splits on the immediate caller's module:
        # `blend_base.py` = the `generate_weights` loop (flag-gated),
        # `position_policy.py` = the delegated panel helper (flag-independent).
        import inspect

        frame = inspect.currentframe().f_back
        caller = os.path.basename(frame.f_code.co_filename) if frame else ""
        if caller == "blend_base.py":
            calls["kernel"] += 1
        else:
            calls["delegated_helper"] += 1
        return original_sequence(self, *args, **kwargs)

    monkeypatch.setattr(blend_base, "_step_asset_inertia", spy_step)
    monkeypatch.setattr(PortfolioSequencer, "sequence", spy_sequence)

    universe = _blend_universe(200)

    # (b1) DEFAULT (flag absent) -> the KERNEL step runs; the blend engine's own
    # legacy step is NEVER taken (the delegated panel helper also runs, and must --
    # see the provenance note in `spy_sequence`).
    default_run = GenericBlendStrategy().generate_weights(universe, {})
    assert len(default_run.dropna(how="all")) > 0, (
        "the synthetic run produced no rebalance rows -- the call-path spy would be vacuous"
    )
    assert calls["kernel"] > 0, (
        "with `blend_use_policy_kernel` absent, `PortfolioSequencer.sequence` was "
        f"never called from `blend_base.py` (legacy calls={calls['legacy']}): the "
        "DEFAULT inertia path is not the kernel, so the 1.9.0 default flip is gone"
    )
    assert calls["legacy"] == 0, (
        f"with `blend_use_policy_kernel` absent, `_step_asset_inertia` still ran "
        f"{calls['legacy']} times: the legacy step is still the default path"
    )
    assert calls["delegated_helper"] > 0, (
        "`apply_asset_inertia` did not reach the kernel at all -- commit 1b's "
        "delegation has gone dead, and this run would no longer cover it"
    )
    assert projections["off"] > 0, (
        "on the DEFAULT run `SparseProjector.project` was never called -- the sparse "
        "compression step was not delegated"
    )

    # (b2) explicit `blend_use_policy_kernel=False` -> the LEGACY step runs and the
    # blend engine's kernel step is BYPASSED. This is the opt-out that keeps the
    # differential's OFF arm real (and `apply_asset_inertia` still delegates, by
    # design, flag-independently).
    calls["legacy"] = 0
    calls["kernel"] = 0
    calls["delegated_helper"] = 0
    spy_project.state = "on"
    spy_write.state = "on"
    off = GenericBlendStrategy().generate_weights(
        universe, {"blend_use_policy_kernel": False}
    )
    assert len(off.dropna(how="all")) > 0, (
        "the opt-out run produced no rebalance rows -- the call-path spy would be vacuous"
    )
    assert calls["legacy"] > 0, (
        "with `blend_use_policy_kernel=False` `_step_asset_inertia` was never called "
        f"(kernel calls={calls['kernel']}): the legacy step is no longer reachable, so "
        "the opt-out is dead and every OFF-vs-ON differential is now the kernel "
        "compared with itself"
    )
    assert calls["kernel"] == 0, (
        f"with `blend_use_policy_kernel=False` the kernel path ran {calls['kernel']} "
        "times from `blend_base.py`: the explicit opt-out did not bypass the kernel"
    )
    assert calls["delegated_helper"] > 0, (
        "`apply_asset_inertia` did not reach the kernel on the opt-out run -- the "
        "flag-independent delegation has gone dead"
    )

    # (b3) an explicit `True` is the same route as the default (the flag is a
    # selection, not a mode), so both kernel arms must be cell-identical.
    calls["legacy"] = 0
    calls["kernel"] = 0
    on = GenericBlendStrategy().generate_weights(
        universe, {"blend_use_policy_kernel": True}
    )
    assert calls["kernel"] > 0 and calls["legacy"] == 0, (
        f"explicit True took the wrong route (kernel={calls['kernel']}, "
        f"legacy={calls['legacy']})"
    )

    # Disclosed, and the reason this guard is behavioural: the two paths are
    # cell-identical, so output equality is necessary but proves nothing about
    # WHICH path was taken. Kept here so a future divergence surfaces next to the
    # call-path assertions that actually guard the switch.
    pd.testing.assert_frame_equal(on, off)
    pd.testing.assert_frame_equal(default_run, on)

    # Projection is flag-INDEPENDENT (commit 1a): taken in both states, and the
    # write-back rule (`SparseProjector.write`) is still NOT wired (that is M4.3).
    assert projections["on"] > 0, (
        "with `blend_use_policy_kernel=True`, `SparseProjector.project` was never "
        "called -- the projection step is missing from the kernel path too"
    )
    assert projections_written["off"] == 0 and projections_written["on"] == 0, (
        "`SparseProjector.write` is now wired into the backtest path "
        f"({projections_written} calls); that write-back rule belongs to M4.3 and "
        "must not be introduced by the projection commit"
    )
