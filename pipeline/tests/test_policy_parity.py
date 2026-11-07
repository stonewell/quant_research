"""M4.2 phase 2 -- parity harness for the position-policy kernel.

Gate under test (`docs/development_plan.md` §2.16 `M4.2` items 3 and 4): flipping
`blend_use_policy_kernel` from OFF to ON must NOT change a single cell of a blend
strategy's sparse weight frame.

POST-1.9.0 NOTE: the flag's DEFAULT is now ON, so every "OFF" arm below passes
`{"blend_use_policy_kernel": False}` EXPLICITLY. An arm that merely omits the flag
would run the kernel too and the harness would degrade into the kernel compared
with itself -- still green, and worthless. "Not a single cell" is meant literally, and in
three SEPARATE senses -- the sparse-weights contract distinguishes them, and a
harness that compared them as one number would miss the failure that matters:

  1. **identical `NaN` positions** -- a `NaN` cell means "no instruction, the
     backtester carries the previous weight forward". A `NaN` where the other
     path has a value means a rebalance silently appeared or vanished.
  2. **identical explicit `0.0` positions** -- inside a rebalance row a `0.0`
     means "exit this position". Treating it as `NaN` would let the backtester
     keep a stale holding; treating a `NaN` as `0.0` would invent an exit. The
     two are NEVER compared as equal here (`NaN == 0.0` is False in the mask, and
     the zero mask is only meaningful because the NaN masks are already proven
     equal).
  3. **identical numeric values**, `max_abs_diff == 0.0` -- both arms run the
     same float arithmetic (`sells` first, cash-capped buys in `(-diff, symbol)`
     order, cash residual), so exact equality is attainable and is not loosened.

100% offline: synthetic OHLCV only (`common.testing.make_ohlcv_from_closes`). No
network, no real cache, no `data/runs/**`.

SURFACES COVERED
================
  * `test_diff_harness_all_seeds_cell_identical` -- 64 seeds
    (`np.random.default_rng(seed)`), a fresh 8-risky-symbol + cash-proxy universe
    of 320 bars per seed, `GenericBlendStrategy` with the flag OFF vs ON.
  * `test_golden_equal_weight_static_template` -- the `equal_weight` STATIC
    template: the flag is not a static-template concept, so injecting it into the
    params must be inert.
  * `test_golden_chan_four_state_blend_no_inertia_and_baseline` -- the deployed
    `chan_four_state_blend`, on its real SSOT params ("baseline") and on the
    ablation "no_inertia" arm, plus the deployed `regime_ic_ir_hybrid` pin
    (documented exception -- it must NOT be normalized to `rolling_ic_ir`).

DISCLOSED SCOPE LIMIT (why the 64-seed sweep disables four structural flags)
===========================================================================
`BaseBlendAllocationStrategy.generate_weights` calls
`enrich_universe_with_structural_features` (~6.7 s for 8 symbols x 320 bars,
dominated by `fractal_dimension`/`hurst_exponent`) whenever any of
`blend_enable_cyq_quality_gate` / `blend_enable_vp_confluence_sizing` /
`blend_enable_spring_fast_entry` / `blend_enable_structural_chandelier_stop` is
on. Running that 128 times (64 seeds x 2 arms) would put this file at ~35
minutes, so the SWEEP disables those four (documented in `_sweep_params`) and
keeps the inertia step, the sequencer, the projection and the sparse contract --
which is what this file is about -- fully exercised. The enriched path is NOT
left uncovered: the golden `chan_four_state_blend` arms below run the DEPLOYED
params with all four flags on.
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

_PIPELINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_PIPELINE_DIR)
for _root in (_PIPELINE_DIR, _REPO_ROOT):
    if _root not in sys.path:
        sys.path.insert(0, _root)

from common.allocation_templates import EqualWeightAllocation
from common.position_policy import PolicyContext, PositionPolicyConfig, PortfolioSequencer
from common.testing import make_ohlcv_from_closes
from pipeline.research.blend import GenericBlendStrategy
from pipeline.research.blend.blend_base import _step_asset_inertia
from pipeline.research.blend.chan_four_state_blend import ChanFourStateBlendStrategy
from pipeline.research_strategy.rs.config import StrategyConfig

_FLAG = "blend_use_policy_kernel"
_SEEDS = list(range(64))
_N_BARS = 320          # >= 300 bars, per the plan
_N_RISKY = 8           # >= 8 symbols, per the plan (+ the cash proxy)
_CASH_PROXY = "BIL"


# ---------------------------------------------------------------------------
# synthetic inputs
# ---------------------------------------------------------------------------


def _random_universe(rng: np.random.Generator, n_bars: int = _N_BARS) -> dict:
    """A fresh 8-risky-symbol + `BIL` synthetic universe (offline, deterministic).

    Per-symbol drift / amplitude / period / phase / noise are all drawn from `rng`
    so different seeds see genuinely different price paths (not one path with a
    different starting point), while a fixed seed reproduces its own universe.
    """
    dates = pd.bdate_range("2021-01-04", periods=n_bars)
    t = np.arange(n_bars, dtype=float)
    universe = {}
    for i in range(_N_RISKY):
        drift = float(rng.uniform(-0.05, 0.15))
        amplitude = float(rng.uniform(0.5, 8.0))
        period = float(rng.uniform(15.0, 60.0))
        phase = float(rng.uniform(0.0, 2.0 * np.pi))
        noise = np.cumsum(rng.normal(0.0, 0.4, n_bars))
        close = 100.0 + drift * t + amplitude * np.sin(2.0 * np.pi * t / period + phase) + noise
        universe[f"6000{i:02d}.SH"] = make_ohlcv_from_closes(np.maximum(close, 5.0))
    universe[_CASH_PROXY] = make_ohlcv_from_closes(np.full(n_bars, 100.0))
    for frame in universe.values():
        frame.index = dates
    return universe


def _sweep_params(seed: int) -> dict:
    """Per-seed params for the 64-seed sweep.

    The four structural flags are off for the reason given in the module docstring
    (runtime, not semantics). `asymmetric_sells` / `min_holding_bars` are varied
    per seed so the two branches where `_step_asset_inertia` and
    `AssetPolicy.inertia_step` could drift are swept too, not just the defaults.
    """
    return {
        "blend_enable_cyq_quality_gate": False,
        "blend_enable_vp_confluence_sizing": False,
        "blend_enable_spring_fast_entry": False,
        "blend_enable_structural_chandelier_stop": False,
        "blend_asymmetric_sells": bool(seed % 3 == 0),
        "blend_min_holding_bars": [0, 2, 5][seed % 3],
        "blend_min_weight_change": [0.02, 0.05, 0.08][(seed // 3) % 3],
    }


# ---------------------------------------------------------------------------
# the harness itself
# ---------------------------------------------------------------------------


def _assert_bit_identical(off: pd.DataFrame, on: pd.DataFrame, label: str) -> dict:
    """Three SEPARATE equality gates (`NaN` is never `0.0`).

    Returns the counts each caller needs for its own non-vacuity assertions
    (`nan_cells`, `zero_cells`, `rebalance_rows`).
    """
    assert list(off.columns) == list(on.columns), (
        f"{label}: column order changed: {list(off.columns)} != {list(on.columns)}"
    )
    assert off.index.equals(on.index), f"{label}: index changed"

    # (1) NaN positions -- the sparse contract's "no instruction" cells.
    nan_off = off.isna().to_numpy()
    nan_on = on.isna().to_numpy()
    nan_mismatch = np.argwhere(nan_off != nan_on)
    if len(nan_mismatch):
        row, col = nan_mismatch[0]
        raise AssertionError(
            f"{label}: {len(nan_mismatch)} cell(s) disagree on NaN-ness (a NaN is NOT "
            f"a 0.0 and NOT a value). First: row={off.index[row]!r} "
            f"col={off.columns[col]!r} off={'NaN' if nan_off[row, col] else repr(off.iat[row, col])} "
            f"on={'NaN' if nan_on[row, col] else repr(on.iat[row, col])}"
        )

    # (2) explicit 0.0 positions. Meaningful only because (1) passed: the zero mask
    # is False on a NaN, so a NaN-vs-0.0 swap would have failed above -- it must
    # never be absorbed into "values are close enough".
    zero_off = off.to_numpy(dtype=float) == 0.0
    zero_on = on.to_numpy(dtype=float) == 0.0
    zero_mismatch = np.argwhere(zero_off != zero_on)
    if len(zero_mismatch):
        row, col = zero_mismatch[0]
        raise AssertionError(
            f"{label}: {len(zero_mismatch)} cell(s) disagree on being an explicit 0.0 "
            f"(an exit must stay a 0.0, never NaN). First: row={off.index[row]!r} "
            f"col={off.columns[col]!r} off={off.iat[row, col]!r} on={on.iat[row, col]!r}"
        )

    # (3) numeric values. `(off - on)` is NaN wherever either side is NaN, which is
    # exactly the set already proven identical above, so the max is taken over the
    # comparable cells only.
    diff = (off - on).abs()
    max_abs_diff = float(diff.max().max()) if diff.notna().any().any() else 0.0
    assert max_abs_diff == 0.0, (
        f"{label}: max_abs_diff={max_abs_diff!r} -- the two arms are not bit-identical"
    )

    return {
        "nan_cells": int(nan_off.sum()),
        "zero_cells": int(zero_off.sum()),
        "rebalance_rows": int(len(off.dropna(how="all"))),
    }


# ---------------------------------------------------------------------------
# 1. the differential harness -- 64 seeds, off vs on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", _SEEDS)
def test_diff_harness_all_seeds_cell_identical(seed):
    """`np.random.default_rng(seed)`, 64 seeds, 8+1 symbols, 320 bars."""
    rng = np.random.default_rng(seed)
    universe = _random_universe(rng)
    params = _sweep_params(seed)

    # Fresh instances per arm: no shared mutable state can leak between them.
    # `False` is passed EXPLICITLY: the default is ON since BASE_VERSION 1.9.0.
    off = GenericBlendStrategy().generate_weights(universe, {**params, _FLAG: False})
    on = GenericBlendStrategy().generate_weights(universe, {**params, _FLAG: True})

    label = f"seed={seed}"
    stats = _assert_bit_identical(off, on, label)

    # Non-vacuity of BOTH structural assertions: if a seed produced no rebalance
    # row, no NaN cell or no exit, the corresponding gate would be trivially true.
    assert stats["rebalance_rows"] > 1, (
        f"{label}: only {stats['rebalance_rows']} rebalance row(s) -- the comparison would be vacuous"
    )
    assert stats["nan_cells"] > 0, (
        f"{label}: no NaN cell at all -- the sparse contract (and gate 1) is untested here"
    )
    assert stats["zero_cells"] > 0, (
        f"{label}: no explicit 0.0 cell at all -- gate 2 (NaN vs 0.0) is untested here"
    )


# ---------------------------------------------------------------------------
# 2. golden: the equal-weight STATIC template
# ---------------------------------------------------------------------------


def test_golden_equal_weight_static_template():
    """`EqualWeightAllocation` -- the flag must be inert on a static template.

    A static template never consults `blend_use_policy_kernel` (the flag selects
    the BLEND engine's per-bar inertia step). Injecting it into the params is the
    only way to express "off vs on" here, and the correct answer is: nothing
    changes. This also pins that the phase-2 delegations did not reach into the
    static-template path through a shared helper.
    """
    universe = _random_universe(np.random.default_rng(20260601))
    params = {"rebalance_freq_days": 21}

    off = EqualWeightAllocation().generate_weights(universe, {**params, _FLAG: False})
    on = EqualWeightAllocation().generate_weights(universe, {**params, _FLAG: True})

    stats = _assert_bit_identical(off, on, "equal_weight static template")
    assert stats["rebalance_rows"] > 1, (
        f"only {stats['rebalance_rows']} rebalance row(s) -- the comparison would be vacuous"
    )
    assert stats["nan_cells"] > 0, "equal_weight produced no NaN cell -- not sparse"

    # Determinism: a second identical call reproduces the frame (no hidden state).
    pd.testing.assert_frame_equal(off, EqualWeightAllocation().generate_weights(universe, dict(params)))


# ---------------------------------------------------------------------------
# 3. golden: the deployed chan_four_state_blend, baseline + no_inertia
# ---------------------------------------------------------------------------


def _deployed_params(name: str) -> dict:
    path = os.path.join(_PIPELINE_DIR, "research_strategy", "strategies_config.json")
    with open(path, encoding="utf-8") as handle:
        return dict(json.load(handle)[name]["parameters"])


def test_golden_chan_four_state_blend_no_inertia_and_baseline():
    """Deployed `chan_four_state_blend`: `baseline` and `no_inertia` arms, off vs on.

    `no_inertia` mirrors the walkforward ablation arm (inertia gate removed:
    `adaptive_inertia=False`, `min_weight_change=0.0`, `min_holding_bars=0`,
    `entry_threshold=0.0`), so the harness covers the "inertia practically off"
    shape as well as the deployed one. Both arms run the DEPLOYED params
    otherwise -- i.e. with the structural/quality gates enabled, which is the
    path the 64-seed sweep deliberately skips for runtime reasons.
    """
    deployed = _deployed_params("chan_four_state_blend")

    # Documented exception (AGENTS.md / docs/version.md §8): the deployed mode is
    # `regime_ic_ir_hybrid` and must NOT be normalized back to `rolling_ic_ir`.
    assert deployed.get("blend_weighting_mode") == "regime_ic_ir_hybrid", (
        "chan_four_state_blend's deployed weighting mode drifted to "
        f"{deployed.get('blend_weighting_mode')!r} (expected 'regime_ic_ir_hybrid')"
    )
    assert _FLAG not in deployed, (
        f"{_FLAG} was written into the deployed SSOT config -- the flag's default "
        "lives in `StrategyConfig`; a per-strategy copy here would mask it"
    )
    # The strategy really consumes the pin (the key reaches `resolve_param`).
    assert (
        ChanFourStateBlendStrategy().resolve_param(
            dict(deployed), "blend_weighting_mode", None, str
        )
        == "regime_ic_ir_hybrid"
    )

    no_inertia = {
        "cfsb_adaptive_inertia": False,
        "cfsb_min_weight_change": 0.0,
        "cfsb_min_holding_bars": 0,
        "cfsb_entry_threshold": 0.0,
    }
    arms = {
        "baseline": dict(deployed),
        "no_inertia": {**deployed, **no_inertia},
    }

    universe = _random_universe(np.random.default_rng(7))
    frames = {}
    for label, params in arms.items():
        off = ChanFourStateBlendStrategy().generate_weights(
            universe, {**params, _FLAG: False}
        )
        on = ChanFourStateBlendStrategy().generate_weights(
            universe, {**params, _FLAG: True}
        )
        stats = _assert_bit_identical(off, on, f"chan_four_state_blend[{label}]")
        assert stats["rebalance_rows"] > 1, (
            f"[{label}] only {stats['rebalance_rows']} rebalance row(s) -- vacuous"
        )
        assert stats["nan_cells"] > 0, f"[{label}] no NaN cell -- not sparse"
        frames[label] = off

    # Non-vacuity of the `no_inertia` arm: removing the inertia gate must actually
    # change the output, otherwise both arms would be testing the same code path.
    assert not frames["baseline"].equals(frames["no_inertia"]), (
        "the `no_inertia` arm produced the same frame as `baseline` -- the override "
        "keys do not reach the strategy, so this arm proves nothing"
    )


# ---------------------------------------------------------------------------
# 4. differential on the NaN input class -- the sweep's blind spot
# ---------------------------------------------------------------------------
#
# An independent review named this the one uncovered input class. Every frame the
# 64-seed sweep and both goldens feed in is FINITE: the synthetic OHLCV is finite,
# and `generate_weights` itself runs its sub-strategy weights through
# `.ffill().fillna(0.0)` (blend_base.py Step 2), so a NaN cannot even survive into
# `raw_w`. The NaN class is nonetheless reachable on the SAME two code paths,
# because `apply_asset_inertia` / the per-bar step accept an ARBITRARY dense panel
# -- that is where a late-listing symbol (no history yet) or a missing
# sub-strategy history actually arrives. So this differential drives the exact
# step the flag selects over a NaN-carrying panel, instead of `generate_weights`.
#
# `NaN` is never `0.0`: both arms must keep a NaN input cell as a NaN in the same
# place (an exit is an explicit `0.0`, a missing instruction is `NaN`, and the
# backtester's column-wise `ffill()` makes the difference a real position).


def _nan_target_panel(
    *, seed: int = 20261106, n_bars: int = 40, n_risky: int = 6
) -> tuple:
    """Dense target-weight panel with NaN cells, plus the NaN-`ideal` row index.

    Risky weights live in [0, 0.4], each row sums to at most 0.98, the cash proxy
    is the exact residual and exact `0.0` cells are present. Two NaN cells are
    injected, the two shapes that actually reach this step:

      * `(0, "SYM01")` -- the LATE-LISTING shape: `current_w` is seeded from row 0
        and carried forward, so the NaN propagates through the whole panel and the
        symbol is never a trade candidate. This is the class where a defect would
        turn "no instruction" into a fabricated `0.0` exit.
      * `(r, "SYM03")` -- a missing `ideal_w` on a bar where the symbol is HELD
        (`> 0.10`). The reference holds that weight (NaN comparisons are all
        False, so no trade is emitted); it must NOT be read as an exit.
    """
    rng = np.random.default_rng(seed)
    symbols = [f"SYM{i:02d}" for i in range(n_risky)]
    index = pd.bdate_range("2022-05-02", periods=n_bars)
    current = rng.uniform(0.0, 0.30, n_risky)
    rows = np.empty((n_bars, n_risky), dtype=float)
    for t in range(n_bars):
        if t > 0 and rng.random() < 0.20:
            current = np.clip(rng.uniform(0.0, 0.40, n_risky), 0.0, 0.45)
        else:
            current = np.clip(current + rng.normal(0.0, 0.05, n_risky), 0.0, 0.40)
        current[rng.random(n_risky) < 0.30] = 0.0  # real exits, not missing cells
        rows[t] = current
    totals = rows.sum(axis=1, keepdims=True)
    rows = rows * np.minimum(1.0, 0.98 / np.maximum(totals, 1e-12))
    panel = pd.DataFrame(np.round(rows, 8), index=index, columns=symbols)
    panel[_CASH_PROXY] = np.round(1.0 - panel.sum(axis=1), 8)

    assert not panel.isna().to_numpy().any(), "the base panel is finite by construction"
    panel.iloc[0, panel.columns.get_loc("SYM01")] = np.nan

    # A NaN `ideal_w` bar on which SYM03 is still HELD, so "hold" and "exit" are
    # distinguishable in the output (a held 0.0 would make them look identical).
    held_rows = [t for t in range(6, n_bars) if float(panel["SYM03"].iloc[t - 1]) > 0.10]
    assert held_rows, "no bar holds SYM03 at > 0.10 -- the NaN-ideal case would be blind"
    nan_ideal_row = held_rows[0]
    panel.iloc[nan_ideal_row, panel.columns.get_loc("SYM03")] = np.nan
    return panel, nan_ideal_row


def _run_inertia_panel(panel: pd.DataFrame, *, use_policy_kernel: bool) -> pd.DataFrame:
    """Run the step the flag SELECTS, bar by bar, with the engine's exact kwargs.

    `False` -> `blend_base._step_asset_inertia` (the pre-M4.2 per-bar step).
    `True`  -> `PortfolioSequencer.sequence` with the same four-field
    `PositionPolicyConfig` `generate_weights` builds (`asymmetric_sell_ratio`
    stays at the literal 0.60 the legacy call site implies).

    Bar 0 is the seed row, exactly as `generate_weights` does it (`current_w =
    raw_w.copy()`, no inertia), so the panel's row-0 NaN enters the tenure clocks
    the way a late listing would.
    """
    risky = [c for c in panel.columns if c != _CASH_PROXY]
    symbols = list(panel.columns)
    min_weight_change = 0.02
    min_holding_bars = 2
    asymmetric_sells = True
    entry_threshold = 0.05
    thresh_t = pd.Series(float(min_weight_change), index=risky)
    em_row = pd.Series(False, index=risky)

    sequencer = PortfolioSequencer() if use_policy_kernel else None
    policy_cfg = (
        PositionPolicyConfig(
            min_holding_bars=min_holding_bars,
            asymmetric_sells=asymmetric_sells,
            asymmetric_sell_ratio=0.60,
            entry_threshold=entry_threshold,
        )
        if use_policy_kernel
        else None
    )

    holding_age = {s: 0 for s in risky}
    last_buy_bar = {s: -999 for s in risky}
    out = pd.DataFrame(np.nan, index=panel.index, columns=symbols)
    out.iloc[0] = panel.iloc[0].to_numpy(dtype=float)

    for t in range(1, len(panel)):
        ideal_w = panel.iloc[t]
        current_w = out.iloc[t - 1]
        if use_policy_kernel:
            row = sequencer.sequence(
                ideal_w,
                current_w,
                ctx=PolicyContext(bar_index=t),
                risky_symbols=risky,
                symbols=symbols,
                thresh_t=thresh_t,
                holding_age=holding_age,
                last_buy_bar=last_buy_bar,
                cfg=policy_cfg,
                cash_proxy=_CASH_PROXY,
                em_row=em_row,
            )
        else:
            row = _step_asset_inertia(
                t=t,
                ideal_w=ideal_w,
                current_w=current_w,
                risky_symbols=risky,
                symbols=symbols,
                thresh_t=thresh_t,
                holding_age=holding_age,
                last_buy_bar=last_buy_bar,
                cash_proxy=_CASH_PROXY,
                em_row=em_row,
                asymmetric_sells=asymmetric_sells,
                entry_threshold=entry_threshold,
                min_holding_bars=min_holding_bars,
            )
        out.iloc[t] = row.reindex(symbols).to_numpy(dtype=float)
    return out


def test_diff_harness_nan_cells_off_vs_on():
    """Flag OFF (= explicit opt-out) versus flag ON over a frame that CONTAINS NaN.

    Cell by cell: identical NaN positions, identical explicit `0.0` positions,
    identical values (`max_abs_diff == 0.0`). Then, separately, the two NaN
    contracts the sparse weights rely on -- so a silent liquidation cannot hide
    inside a generic value diff:

      * a NaN in `current_w` (late listing) stays NaN for the whole run and is
        NEVER `0.0` (an explicit `0.0` there would be a fabricated exit);
      * a NaN in `ideal_w` on a HELD bar resolves to the held weight (no trade
        emitted), never to a fabricated exit either.

    `NaN == 0.0` is never treated as equal anywhere in this test.
    """
    panel, nan_ideal_row = _nan_target_panel()
    nan_in = panel.isna().to_numpy()
    assert int(nan_in.sum()) == 2, "the panel must carry exactly the two injected NaN cells"
    previous_row = nan_ideal_row - 1
    assert float(panel["SYM03"].iloc[previous_row]) > 0.10, (
        "the NaN-ideal bar must be preceded by a held position, otherwise 'hold' and "
        "'exit' would be indistinguishable"
    )

    # Post-1.9.0: the default IS the kernel and `False` is the explicit opt-out.
    default_flag = StrategyConfig().blend_use_policy_kernel
    assert default_flag is True, (
        "StrategyConfig.blend_use_policy_kernel is no longer True -- the default "
        "flip (BASE_VERSION 1.9.0) was reverted, so this 'off vs on' run would "
        "compare the kernel with itself"
    )
    on = _run_inertia_panel(panel, use_policy_kernel=default_flag)
    off = _run_inertia_panel(panel, use_policy_kernel=False)

    stats = _assert_bit_identical(off, on, "NaN panel (opt-out vs default kernel)")

    # Non-vacuity: the run really produced NaN and explicit-0.0 cells, and it really
    # rewrote at least one finite cell (a pass-through would make every equality
    # above true for free).
    assert stats["nan_cells"] > 0, "no NaN cell survived the step -- nothing was exercised"
    assert stats["zero_cells"] > 0, "no explicit 0.0 cell -- the exit path is untested here"
    finite = (~nan_in) & on.notna().to_numpy()
    assert (on.to_numpy(dtype=float)[finite] != panel.to_numpy(dtype=float)[finite]).any(), (
        "the kernel arm rewrote no finite cell -- the inertia step was a no-op, so the "
        "NaN equality would be vacuous"
    )

    # (a) NaN in `current_w`: stay NaN, NEVER `0.0`, in BOTH arms.
    for label, arm in (("opt-out/legacy", off), ("default/kernel", on)):
        column = arm["SYM01"].to_numpy(dtype=float)
        assert np.isnan(column).all(), (
            f"{label}: the NaN `current_w` symbol came back with values -- a NaN current "
            "must be carried through untouched"
        )
        assert not bool((column == 0.0).any()), (
            f"{label}: a NaN `current_w` cell was rewritten as 0.0 -- a silent "
            "liquidation, NOT 'no instruction'"
        )

    # (b) NaN in `ideal_w` on a HELD bar: hold, do not exit, in BOTH arms.
    for label, arm in (("opt-out/legacy", off), ("default/kernel", on)):
        held_before = float(arm["SYM03"].iloc[previous_row])
        at_nan_bar = float(arm["SYM03"].iloc[nan_ideal_row])
        assert held_before > 0.0, (
            f"{label}: SYM03 was not held before the NaN-ideal bar ({held_before!r}) -- "
            "the hold-vs-exit distinction would be vacuous"
        )
        assert at_nan_bar == held_before, (
            f"{label}: a NaN `ideal_w` on a held bar became {at_nan_bar!r} (held "
            f"{held_before!r}) -- NaN must mean 'no instruction', never an exit"
        )
