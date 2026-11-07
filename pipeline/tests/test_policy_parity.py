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
  * section 5 -- the LIVE adapter (`common/position_lifecycle`). M4.4 stage D3
    DELETED the legacy 6-state body, so its reference arm is executed from a
    pinned git blob (`a415450`, the commit before the deletion) rather than from
    `live_use_policy_kernel=False`. The flag itself is now inert (it only logs).

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

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

import numpy as np
import pandas as pd
import pytest

_PIPELINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_PIPELINE_DIR)
for _root in (_PIPELINE_DIR, _REPO_ROOT):
    if _root not in sys.path:
        sys.path.insert(0, _root)

from common import position_lifecycle as _live_lifecycle
from common.allocation_templates import EqualWeightAllocation
from common.position_lifecycle import (
    AssetLifecycleState,
    PositionLifecycleConfig,
    PositionState,
    simulate_position_lifecycle,
    step_position_lifecycle,
)
from common.position_policy import PolicyContext, PositionPolicyConfig, PortfolioSequencer
from common.testing import make_ohlcv_from_closes
from pipeline.research.blend import GenericBlendStrategy
from pipeline.research.blend.chan_four_state_blend import ChanFourStateBlendStrategy
from pipeline.research_strategy.rs.config import StrategyConfig

_FLAG = "blend_use_policy_kernel"
_SEEDS = list(range(64))
_N_BARS = 320          # >= 300 bars, per the plan
_N_RISKY = 8           # >= 8 symbols, per the plan (+ the cash proxy)
_CASH_PROXY = "BIL"

# ---------------------------------------------------------------------------
# the pinned pre-deletion reference for the legacy per-bar inertia step
# ---------------------------------------------------------------------------
#
# `_step_asset_inertia` was DELETED from `blend_base` (and from
# `common/allocation_templates.py`) by M4.4 stage D1 / `BASE_VERSION 2.0.0`. The
# "OFF" arm of the NaN-panel differential in section 4 below must still execute
# the REAL legacy step -- if it imported a stub, or silently fell back to the
# kernel, that gate would compare the kernel with itself, stay green, and prove
# nothing (exactly the "still green, and worthless" failure mode the module
# docstring warns about).
#
# So the reference is executed from a PINNED git blob: `ab82318`, the last commit
# BEFORE `_step_asset_inertia` began delegating, i.e. the same pin
# `pipeline/research_strategy/tests/test_blend_policy_kernel.py` uses for the
# parent `blend_base` blob. A `HEAD:`-based reference is the wrong thing here --
# it would track the deletion and go vacuous the moment the deletion landed.
_LEGACY_TEMPLATES_COMMIT = "ab82318"
_LEGACY_TEMPLATES_PATH = "common/allocation_templates.py"
_LEGACY_TEMPLATES_MODULE = None


def _legacy_step_asset_inertia():
    """Return the PRE-deletion `_step_asset_inertia` from the pinned git blob.

    Loaded once per session (`importlib`), with two non-vacuity guards on the blob
    itself: it MUST define the legacy step, and it MUST NOT already be the kernel
    delegation (a blob that imports the kernel would make the "OFF" arm a second
    kernel run).
    """
    global _LEGACY_TEMPLATES_MODULE
    if _LEGACY_TEMPLATES_MODULE is not None:
        return _LEGACY_TEMPLATES_MODULE._step_asset_inertia

    try:
        proc = subprocess.run(
            ["git", "show", f"{_LEGACY_TEMPLATES_COMMIT}:{_LEGACY_TEMPLATES_PATH}"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:  # pragma: no cover
        pytest.skip(f"cannot read the pinned allocation_templates blob from git: {exc}")

    src = proc.stdout
    assert src.strip(), "git show returned an empty allocation_templates source"
    assert "def _step_asset_inertia(" in src, (
        "the pinned allocation_templates blob no longer contains the legacy "
        "`_step_asset_inertia` step -- the OFF arm would be vacuous"
    )
    assert "kernel_apply_asset_inertia" not in src, (
        "the pinned allocation_templates blob already delegates to the policy "
        "kernel -- the reference must be the PRE-delegation implementation"
    )

    tmpdir = tempfile.mkdtemp(prefix="m4_4_parity_legacy_templates_")
    module_path = os.path.join(tmpdir, "allocation_templates_pre_deletion.py")
    with open(module_path, "w", encoding="utf-8") as fh:
        fh.write(src)

    spec = importlib.util.spec_from_file_location(
        "_parity_allocation_templates_pre_deletion", module_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert hasattr(module, "_step_asset_inertia"), (
        "the pinned module executed but exposes no `_step_asset_inertia`"
    )
    _LEGACY_TEMPLATES_MODULE = module
    return module._step_asset_inertia


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

    `False` -> `_step_asset_inertia`, executed from the PINNED pre-deletion
    `common/allocation_templates.py` blob (`ab82318`): the live module lost that
    helper in M4.4 stage D1, and substituting anything else here would turn this
    gate into code-versus-itself.
    `True`  -> `PortfolioSequencer.sequence` with the same four-field
    `PositionPolicyConfig` `generate_weights` builds (`asymmetric_sell_ratio`
    stays at the literal 0.60 the legacy call site implies).

    Bar 0 is the seed row, exactly as `generate_weights` does it (`current_w =
    raw_w.copy()`, no inertia), so the panel's row-0 NaN enters the tenure clocks
    the way a late listing would.
    """
    legacy_step = None if use_policy_kernel else _legacy_step_asset_inertia()
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
            row = legacy_step(
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

    # Non-vacuity of the OFF arm's PROVENANCE: it must be the pinned pre-deletion
    # module's own function -- never a live alias, and never the kernel. (The
    # dynamic half of the non-vacuity proof is external: perturbing the kernel by
    # 1e-12 in one cell turns this gate red.)
    assert _legacy_step_asset_inertia().__module__ == "_parity_allocation_templates_pre_deletion", (
        "the OFF arm is not executing the pinned pre-deletion `_step_asset_inertia`"
    )

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


# ---------------------------------------------------------------------------
# 5. (M4.3/D3) the LIVE adapter -- pinned legacy body vs the kernel
# ---------------------------------------------------------------------------
#
# The gate for M4.3 is narrower than the blend kernel's and lives on a different
# seam: `common/position_lifecycle.step_position_lifecycle` keeps its signature
# and return type and delegates ONE step to `AssetPolicy.step`. M4.4 stage D3
# DELETED the legacy 6-state body (`BASE_VERSION 2.0.0`), so the "legacy" arm is
# now executed from a PINNED git blob -- `a415450`, the commit before the deletion
# -- instead of from `live_use_policy_kernel=False`. That keeps the differential
# that licensed the deletion executable: an arm that silently ran the kernel would
# compare the kernel with itself and prove nothing.
#
# The differential drives `simulate_position_lifecycle` -- the same call the
# backtester and the parity harness make -- over a real multi-symbol, multi-bar
# path, and compares:
#
#   1. the sparse weight FRAME, with the same three separate gates as section 1
#      (NaN positions, explicit `0.0` positions, numeric values) -- the live path
#      writes 0.0 for an exited name, and treating that as NaN would hand the
#      backtester a stale holding;
#   2. the lifecycle RECORDS on their shared decision vocabulary
#      (action, target_weight, reason, state_before, state_after,
#      fast_path_triggered, cancelled_entry_streak, blocking_reason);
#   3. the PERSISTED STATE trajectory per (date, symbol) -- `LifecycleDecision`
#      does not carry the counters, so a `held_bars` / `cooldown_remaining` drift
#      would otherwise stay invisible until it changed an action days later.
#
# The two fields the engines deliberately do NOT share are asserted separately and
# never folded into "identical": `detail_reason` (the kernel travels its counters
# there) and the kernel-only state fields (`last_exit_price`, `breakdown_streak`,
# `expansion_streak`, `cold_start`, `entry_price_approx`). See
# `common/position_lifecycle.py`'s module docstring.
#
# The parity arm leaves `price_reentry_gate` OFF. That is not a convenience: with
# the gate ON and a real exit anchor present, the kernel genuinely refuses a
# re-entry the legacy machine allows -- gate 3b is an OPT-IN behaviour that the
# legacy body does not implement at all, so no differential can be run with it
# enabled and still claim "same decisions". The gate's own conditional behaviour
# is pinned by the last test below.

_ADAPTER_SEEDS = list(range(6))
_DECISION_KEYS = (
    "action",
    "target_weight",
    "reason",
    "state_before",
    "state_after",
    "fast_path_triggered",
    "cancelled_entry_streak",
    "blocking_reason",
)

#: The state fields BOTH engines carry (the kernel-only additions are asserted
#: separately -- folding them in would turn a documented representation
#: difference into a permanent failure).
_SHARED_STATE_KEYS = (
    "symbol",
    "state",
    "held_bars",
    "entry_streak",
    "exit_streak",
    "cooldown_remaining",
    "entry_price",
    "peak_price",
    "current_weight",
    "broker_weight",
    "last_exit_type",
)

#: The pinned PRE-deletion lifecycle module (the "legacy" arm of section 5).
_LEGACY_LIFECYCLE_COMMIT = "a415450"
_LEGACY_LIFECYCLE_PATH = "common/position_lifecycle.py"
_LEGACY_LIFECYCLE_MODULE = None


def _legacy_lifecycle_module():
    """Load the pre-D3 `common/position_lifecycle.py` from the pinned git blob.

    Fails (never skips): this is the reference arm of the bit-identity gate, and a
    skip would let it disappear silently.
    """
    global _LEGACY_LIFECYCLE_MODULE
    if _LEGACY_LIFECYCLE_MODULE is not None:
        return _LEGACY_LIFECYCLE_MODULE

    try:
        proc = subprocess.run(
            ["git", "show", f"{_LEGACY_LIFECYCLE_COMMIT}:{_LEGACY_LIFECYCLE_PATH}"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:  # pragma: no cover
        pytest.fail(
            f"cannot read the pre-D3 lifecycle blob "
            f"({_LEGACY_LIFECYCLE_COMMIT}:{_LEGACY_LIFECYCLE_PATH}) from git: {exc}"
        )

    src = proc.stdout
    assert src.strip(), "git show returned an empty position_lifecycle source"
    assert "def step_position_lifecycle(" in src, (
        "the pinned lifecycle blob no longer defines `step_position_lifecycle` -- "
        "the legacy arm would be vacuous"
    )
    assert "AssetPolicy" not in src and "live_use_policy_kernel" not in src, (
        "the pinned lifecycle blob already contains the M4.3 kernel adapter -- the "
        "reference must be the PRE-deletion legacy body"
    )

    tmpdir = tempfile.mkdtemp(prefix="m4_4_d3_parity_legacy_lifecycle_")
    module_path = os.path.join(tmpdir, "position_lifecycle_pre_d3.py")
    with open(module_path, "w", encoding="utf-8") as fh:
        fh.write(src)

    spec = importlib.util.spec_from_file_location(
        "_parity_position_lifecycle_pre_d3", module_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _LEGACY_LIFECYCLE_MODULE = module
    return module


def _adapter_inputs(seed: int, n_bars: int = 120) -> tuple:
    """A synthetic OHLCV universe plus a sparse target-weight panel for one seed.

    Targets switch regime every ~10 bars (0.0 / small / large weights) so the run
    traverses the entry ladder, the tenure lock, inertia trims, full exits and the
    cooldown; `NaN` rows are the "no instruction" bars the sparse contract requires.
    """
    rng = np.random.default_rng(10_000 + seed)
    universe = _random_universe(rng, n_bars=n_bars)
    risky = [c for c in universe if c != _CASH_PROXY]
    index = universe[risky[0]].index
    weights = pd.DataFrame(np.nan, index=index, columns=list(universe))
    for t in range(n_bars):
        if t % 10 == 0:
            draw = rng.uniform(0.0, 0.34, len(risky))
            row = np.where(rng.random(len(risky)) < 0.35, 0.0, draw)
        elif t % 10 < 4:
            row = None  # no rebalance instruction -> the whole row stays NaN
        else:
            row = None
        if row is None:
            continue
        weights.iloc[t, [weights.columns.get_loc(s) for s in risky]] = row
    # A zero row means "exit everything"; `simulate_position_lifecycle` only
    # rebalances when some asset trades, so at least one non-zero weight keeps the
    # frame honest.
    if float(np.nansum(weights.to_numpy(dtype=float))) == 0.0:
        weights.iloc[0, 0] = 0.25
    return universe, weights


def _run_adapter_arm(seed: int, *, kernel: bool = True, **cfg_overrides):
    """Run `simulate_position_lifecycle` once, kernel or pinned-legacy arm.

    `kernel=False` executes the PINNED pre-D3 module end to end (its own config
    class, its own simulator): the legacy body no longer exists in the working
    tree, and substituting the kernel here would make the differential vacuous.
    """
    universe, weights = _adapter_inputs(seed)
    if kernel:
        cfg = PositionLifecycleConfig(**cfg_overrides)
        frame, records = simulate_position_lifecycle(
            universe, weights, cfg=cfg, market="china", cash_proxy=_CASH_PROXY
        )
    else:
        legacy = _legacy_lifecycle_module()
        cfg = legacy.PositionLifecycleConfig(**cfg_overrides)
        frame, records = legacy.simulate_position_lifecycle(
            universe, weights, cfg=cfg, market="china", cash_proxy=_CASH_PROXY
        )
    return universe, weights, frame, records, cfg


def _decision_view(rec: dict) -> tuple:
    """The decision fields BOTH engines are contractually required to agree on.

    `blocking_reason` is included: it is the telemetry column the ticket reads, so
    a row that produced the same action for a DIFFERENT reason is a divergence.
    """
    return tuple(rec.get(k) for k in _DECISION_KEYS)


@pytest.mark.parametrize("seed", _ADAPTER_SEEDS)
def test_lifecycle_adapter_legacy_vs_kernel_cell_and_record_identical(seed):
    """Pinned legacy vs kernel over a 120-bar, 9-symbol path: same frame, same records.

    The legacy arm is the `a415450` blob's own `simulate_position_lifecycle`, i.e.
    the body D3 deleted -- so this remains the evidence that licensed the deletion.
    """
    off_u, off_w, off_frame, off_recs, _ = _run_adapter_arm(seed, kernel=False)
    on_u, on_w, on_frame, on_recs, _ = _run_adapter_arm(seed, kernel=True)
    assert list(off_u) == list(on_u) and off_w.equals(on_w)

    stats = _assert_bit_identical(off_frame, on_frame, f"adapter seed {seed}")
    assert stats["rebalance_rows"] > 0, f"seed {seed}: no rebalance row -- vacuous"
    assert stats["zero_cells"] > 0, f"seed {seed}: no explicit 0.0 cell -- exits untested"

    assert len(off_recs) == len(on_recs) > 0
    for i, (a, b) in enumerate(zip(off_recs, on_recs)):
        assert (a["date"], a["symbol"]) == (b["date"], b["symbol"])
        assert _decision_view(a) == _decision_view(b), (
            f"seed {seed}: record {i} ({a['date']}, {a['symbol']}) diverged:\n"
            f" legacy={_decision_view(a)}\n kernel={_decision_view(b)}"
        )

    # Non-vacuity: the path actually traded through both directions and used the
    # stop-loss fast path, so "identical" is not "both sides sat still".
    actions = {r["action"] for r in off_recs}
    assert {"BUY", "SELL"} <= actions, actions
    assert any(r.get("fast_path_triggered") == "STOP_LOSS" for r in off_recs), (
        f"seed {seed}: the hard-stop fast path never fired over this path"
    )


@pytest.mark.parametrize("seed", _ADAPTER_SEEDS[:3])
def test_lifecycle_adapter_state_trajectory_identical(seed):
    """The persisted state must agree at EVERY step, not just where it trades.

    `simulate_position_lifecycle` returns no state snapshots, so this replays the
    same inputs through `step_position_lifecycle` directly, carrying one
    `AssetLifecycleState` per symbol across bars -- the shape
    `run_live_strategy` actually uses. The kernel-only state fields
    (`last_exit_price`, `breakdown_streak`, `expansion_streak`, `cold_start`,
    `entry_price_approx`) are excluded from the equality and asserted separately
    (documented divergence: the legacy body never maintained them).
    """
    universe, weights = _adapter_inputs(seed)
    risky = [c for c in universe if c != _CASH_PROXY]
    closes = {s: universe[s]["Close"] for s in risky}
    # Same target semantics as `simulate_position_lifecycle`: a NaN is "carry the
    # previous instruction", NOT 0.0 (which would mean "exit now"). Using 0.0 here
    # would make the replay a monotonically-flat path -- still equal, but vacuous.
    raw_targets = weights.ffill().fillna(0.0)

    def run(kernel: bool):
        module = _live_lifecycle if kernel else _legacy_lifecycle_module()
        cfg = module.PositionLifecycleConfig()
        states = {s: module.AssetLifecycleState(symbol=s) for s in risky}
        trajectory, exit_anchors = [], {s: [] for s in risky}
        actions = set()
        for t in universe[risky[0]].index:
            snapshot = {}
            for s in risky:
                decision = module.step_position_lifecycle(
                    states[s],
                    float(raw_targets.at[t, s]),
                    float(closes[s].loc[t]),
                    cfg,
                    market="china",
                )
                actions.add(decision.action)
                d = states[s].to_dict()
                exit_anchors[s].append(d.pop("last_exit_price", 0.0))
                snapshot[s] = {k: d.get(k) for k in _SHARED_STATE_KEYS}
            trajectory.append((t, snapshot))
        return trajectory, exit_anchors, actions

    (off_traj, off_anchors, off_actions), (on_traj, on_anchors, _) = run(False), run(True)
    assert len(off_traj) == len(on_traj)
    for (t_off, a), (t_on, b) in zip(off_traj, on_traj):
        assert t_off == t_on
        assert a == b, f"seed {seed} @ {t_off}: state diverged\n legacy={a}\n kernel={b}"

    # Non-vacuity: the path entered AND exited, with a stop, in the shared state.
    assert {"BUY", "SELL"} <= off_actions, off_actions

    # The documented state divergence, pinned: the legacy body writes 0.0 forever,
    # the kernel records the close of each real exit. Any other anchor difference
    # would mean the kernel wrote one where no exit happened.
    assert all(v == 0.0 for s in off_anchors for v in off_anchors[s])
    assert any(v > 0.0 for s in on_anchors for v in on_anchors[s]), (
        "the kernel never recorded an exit anchor -- gate 3b would be untestable"
    )


def test_lifecycle_adapter_two_day_breakdown_rule_needs_an_anchor():
    """Gate 2b is inert WITHOUT an anchor and live WITH one (Stage C split).

    Two separate facts, and conflating them is what this rewrite fixes:

      1. **A caller that supplies no `ma20_close` still gets a strict no-op.**
         The kernel reads a missing anchor as "this bar cannot be evaluated",
         never as a fabricated 0.0 floor (which would make every close a
         breakdown). That is the contract `common/tests/test_position_lifecycle.py`
         pins through the same signature, re-pinned here so the parity arm can
         keep the knob alone.
      2. **The simulate seam DOES supply the anchor** (M1.1 / Stage C), derived
         causally by `derive_lifecycle_context`, so arming the rule through
         `simulate_position_lifecycle` is a real, deliberate behaviour change --
         the armed run produces `BREAKDOWN` exits the unarmed run never emits.
         Before Stage C this seam was inert too, which is exactly why the old
         version of this test could assert bit-identity: nothing derived the
         anchor anywhere.
    """
    # (1) No anchor -> strict no-op, through the signature the live caller uses.
    cfg_off = PositionLifecycleConfig()
    cfg_armed = PositionLifecycleConfig(two_day_breakdown_rule=True)

    def _held_step(cfg):
        state = AssetLifecycleState(
            symbol="A",
            state=PositionState.HELD_ACTIVE,
            held_bars=5,
            entry_price=100.0,
            peak_price=100.0,
            current_weight=0.30,
        )
        # 1% under the cost basis: no hard stop, no ATR (None), no VWAP. Only the
        # missing MA20 anchor can decide whether this bar is a structural break.
        return step_position_lifecycle(state, 0.30, 99.0, cfg), state

    off_decision, off_state = _held_step(cfg_off)
    armed_decision, armed_state = _held_step(cfg_armed)
    assert armed_decision.action == off_decision.action == "HOLD"
    assert armed_state.to_dict() == off_state.to_dict()
    assert armed_state.breakdown_streak == 0

    # (2) Through the simulator -- the anchor exists, so the armed rule fires.
    base = _run_adapter_arm(0)
    armed = _run_adapter_arm(0, two_day_breakdown_rule=True)
    breakdown_exits = [
        r
        for r in armed[3]
        if r["action"] == "SELL"
        and (r.get("detail_reason") or {}).get("last_exit_type") == "BREAKDOWN"
    ]
    assert breakdown_exits, "Stage C plumbed no MA20 anchor into the simulator"
    assert not any(
        (r.get("detail_reason") or {}).get("last_exit_type") == "BREAKDOWN" for r in base[3]
    ), "the UNARMED run emitted a structural-breakdown exit"
    # ... and the frame genuinely diverged (a breakdown exit is an explicit 0.0).
    assert not base[2].equals(armed[2])


def test_lifecycle_adapter_price_reentry_gate_is_the_one_real_divergence():
    """Why the differential above keeps gate 3b OFF -- and proof it is inert alone.

    With `last_exit_price` unknown (`0.0`, the cold-start value) the gate ALLOWS,
    so legacy and kernel agree. Once a real exit anchor exists and the price has
    not cleared `last_exit_price * (1 + price_reentry_pct)`, the kernel refuses the
    re-entry (`REENTRY_GATED`) while the PINNED legacy body (`a415450`, the same
    pre-D3 reference section 5 uses) has no such concept. That is a genuine,
    opt-in behaviour difference, not a porting defect, which is exactly why no
    parity claim is made with the gate enabled.
    """
    legacy_module = _legacy_lifecycle_module()

    def step(kernel: bool, gate: bool, last_exit_price: float, price: float):
        module = _live_lifecycle if kernel else legacy_module
        cfg_kwargs = dict(guard_latency_bars=1)
        if kernel:
            # The M1.1 knobs (incl. gate 3b) were added WITH the kernel bridge:
            # the pinned legacy config has no such field, and the legacy body has
            # no such concept.
            cfg_kwargs["price_reentry_gate"] = gate
        cfg = module.PositionLifecycleConfig(**cfg_kwargs)
        # `cooldown_remaining=1`: the LAST cooldown bar, which is the only bar a
        # re-entry can happen on. (A COOLDOWN state with 0 days left is not a state
        # the legacy body can consume at all -- it raises -- and the live driver
        # never persists one, so it is not the case to test gate 3b against.)
        state_kwargs = dict(
            symbol="A",
            state=PositionState.COOLDOWN,
            cooldown_remaining=1,
        )
        # The pinned legacy state has no `last_exit_price` field (the anchor is a
        # kernel-only addition), so it can only be primed on the kernel arm.
        if kernel:
            state_kwargs["last_exit_price"] = last_exit_price
        state = module.AssetLifecycleState(**state_kwargs)
        return module.step_position_lifecycle(state, 0.30, price, cfg), state

    # Unknown anchor -> legacy and kernel both allow the entry (gate is a no-op,
    # not a lockout).
    legacy, _ = step(False, True, 0.0, 100.0)
    kernel, _ = step(True, True, 0.0, 100.0)
    assert kernel.action == legacy.action == "BUY"

    # Known anchor + price still below the re-entry threshold -> the kernel gates.
    legacy_at, _ = step(False, True, 100.0, 100.5)
    kernel_gated, gated_state = step(True, True, 100.0, 100.5)
    assert legacy_at.action == "BUY", "the legacy machine has no gate 3b by design"
    assert kernel_gated.action == "HOLD_FILTERED"
    assert kernel_gated.blocking_reason == "REENTRY_GATED"
    assert gated_state.state == PositionState.FLAT

    # ... and the same call with the gate OFF reproduces the legacy decision.
    kernel_open, _ = step(True, False, 100.0, 100.5)
    assert kernel_open.action == legacy_at.action
