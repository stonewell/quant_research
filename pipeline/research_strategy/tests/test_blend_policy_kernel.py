"""M4.2 phase 1 -- `blend_use_policy_kernel` (default OFF) wiring tests.

What this file pins, in one place:

  1. DISABLED BIT-IDENTITY -- with the flag absent (or explicitly False) the blend
     engine's output equals the PRE-M4.2 module exactly: same cells, same `NaN`
     positions, same column order, `max_abs_diff == 0.0`. The parent module is
     loaded from a PINNED git blob, not from `HEAD:`. Why pinned: once this change
     is committed, `HEAD:pipeline/research/blend/blend_base.py` IS the new file, so
     a `HEAD:`-based comparison would silently degrade into code-versus-itself and
     keep passing even if the legacy branch were rewritten. The pin (the last
     commit before the delegation) also lets the loaded source be asserted flag-free,
     which is what makes the test non-vacuous.
  2. ENABLED EQUIVALENCE -- with the flag True, `PortfolioSequencer.sequence` returns
     the SAME committed weights as `_step_asset_inertia` (tolerance 0.0: both paths
     run the same float arithmetic, so any difference would be structural, not
     numerical).
  3. SPARSE / CELL-LEVEL CONTRACT -- non-rebalance rows are all `NaN`, the output is
     never forward-filled by the template, a rebalance row carries NO `NaN` in a
     risky column, and a de-risked symbol is an explicit `0.0` (a `NaN` there would
     make the backtester's column-wise `ffill()` silently keep the stale position).
  4. FLAG RESOLUTION -- params first, then the `StrategyConfig` attribute, on the
     PREFIXED key only. The bare name `use_policy_kernel` must be ignored: the
     2026-10-21 `wq_alpha_id` incident was a bare name falling through to a global
     `StrategyConfig` field and silently swapping a sub-strategy class.
  5. DEPLOYED CONFIG PIN -- `chan_four_state_blend` stays on
     `regime_ic_ir_hybrid` (documented exception, `docs/version.md` §8).
  6. VERSION PIN -- `BaseBlendAllocationStrategy.BASE_VERSION == "1.8.0"`.

100% offline: synthetic OHLCV only (`common.testing.make_ohlcv_from_closes`).
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.position_policy import PolicyContext, PositionPolicyConfig, PortfolioSequencer
from common.testing import make_ohlcv_from_closes
from pipeline.research.blend import GenericBlendStrategy
from pipeline.research.blend.blend_base import BaseBlendAllocationStrategy
from pipeline.research_strategy.rs.config import StrategyConfig

# Last commit BEFORE the M4.2 delegation (see module docstring for why `HEAD:` is
# the wrong reference here).
_PARENT_COMMIT = "c51dd2a"
_PARENT_PATH = "pipeline/research/blend/blend_base.py"
_FLAG = "blend_use_policy_kernel"

_PARENT_MODULE = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_universe(n_days: int = 200) -> dict:
    """Deterministic synthetic universe (offline, no network)."""
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    t = np.arange(n_days)
    universe = {
        "601225.SH": make_ohlcv_from_closes(100.0 + 0.15 * t + 5.0 * np.sin(2 * np.pi * t / 25)),
        "601857.SH": make_ohlcv_from_closes(100.0 + 0.08 * t + 4.0 * np.cos(2 * np.pi * t / 30)),
        "601872.SH": make_ohlcv_from_closes(100.0 - 0.05 * t + 3.0 * np.sin(2 * np.pi * t / 20)),
        "BIL": make_ohlcv_from_closes(np.full(n_days, 100.0)),
    }
    for df in universe.values():
        df.index = dates
    return universe


def _parent_module():
    """Import the PRE-M4.2 `blend_base` from the pinned git blob."""
    global _PARENT_MODULE
    if _PARENT_MODULE is not None:
        return _PARENT_MODULE

    try:
        proc = subprocess.run(
            ["git", "show", f"{_PARENT_COMMIT}:{_PARENT_PATH}"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:  # pragma: no cover
        pytest.skip(f"cannot read the pre-M4.2 blend_base blob from git: {exc}")

    src = proc.stdout
    assert src.strip(), "git show returned an empty blend_base source"
    assert _FLAG not in src, (
        f"the pinned parent blob already mentions {_FLAG!r}; the bit-identity test "
        "would be comparing the new code against itself (vacuous)"
    )

    tmpdir = tempfile.mkdtemp(prefix="m4_2_blend_parent_")
    module_path = os.path.join(tmpdir, "blend_base_pre_m42.py")
    with open(module_path, "w", encoding="utf-8") as fh:
        fh.write(src)

    spec = importlib.util.spec_from_file_location("_blend_base_pre_m42", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _PARENT_MODULE = module
    return module


def _parent_strategy() -> "BaseBlendAllocationStrategy":
    """A concrete strategy whose `generate_weights` is the PRE-M4.2 implementation."""
    parent = _parent_module()

    class _ParentGenericBlend(parent.BaseBlendAllocationStrategy):
        version = "1.0.1"
        _resolve_sub_strategies_spec = GenericBlendStrategy._resolve_sub_strategies_spec
        get_sub_strategies = GenericBlendStrategy.get_sub_strategies
        get_sub_strategy_weights = GenericBlendStrategy.get_sub_strategy_weights

    return _ParentGenericBlend()


def _run(universe, params=None, cfg=None):
    return GenericBlendStrategy(config=cfg).generate_weights(universe, params)


def _assert_frames_identical(actual: pd.DataFrame, expected: pd.DataFrame, label: str) -> None:
    """Cell-exact equality, `NaN` footprint included (a `NaN` is NOT a `0.0`)."""
    assert list(actual.columns) == list(expected.columns), (
        f"{label}: column order changed: {list(actual.columns)} != {list(expected.columns)}"
    )
    assert actual.index.equals(expected.index), f"{label}: index changed"
    assert actual.isna().equals(expected.isna()), (
        f"{label}: NaN footprint changed "
        f"({int(actual.isna().sum().sum())} vs {int(expected.isna().sum().sum())} NaN cells) -- "
        "a NaN and an explicit 0.0 are NOT interchangeable under the sparse contract"
    )
    diff = (actual - expected).abs()
    max_abs_diff = float(diff.max().max()) if diff.notna().any().any() else 0.0
    assert max_abs_diff == 0.0, f"{label}: max_abs_diff={max_abs_diff!r} (expected bit-identical)"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def frames():
    """Legacy (flag absent), legacy (explicit False), kernel (True), parent module."""
    universe = _make_universe(200)
    return {
        "universe": universe,
        "absent": _run(universe),
        "explicit_false": _run(universe, {_FLAG: False}),
        "kernel": _run(universe, {_FLAG: True}),
        "parent": _parent_strategy().generate_weights(universe),
    }


# ---------------------------------------------------------------------------
# 1. disabled bit-identity vs. the pre-change module
# ---------------------------------------------------------------------------


def test_disabled_path_is_bit_identical_to_pre_change_module(frames):
    """Flag absent => output equals the pre-M4.2 implementation exactly."""
    absent = frames["absent"]
    parent = frames["parent"]

    # Non-vacuity guard: the comparison must actually contain rebalance rows.
    assert len(absent.dropna(how="all")) > 0, "no rebalance rows -- the comparison would be vacuous"
    _assert_frames_identical(absent, parent, "flag absent vs pre-M4.2 module")

    # Explicit False must take the same branch (the guard tests truthiness, not
    # presence), so it is also bit-identical to the parent.
    _assert_frames_identical(frames["explicit_false"], parent, "flag False vs pre-M4.2 module")


# ---------------------------------------------------------------------------
# 2. enabled equivalence with the legacy path
# ---------------------------------------------------------------------------


def test_enabled_kernel_matches_legacy_committed_weights_exactly(frames):
    """Flag True => same committed weights as `_step_asset_inertia`, tolerance 0.0.

    Both routes execute the same float arithmetic (`sells-first`, cash-capped buys
    in `(-diff, symbol)` order, cash residual), so an exact match is attainable --
    the tolerance is not loosened to hide a structural difference.
    """
    kernel = frames["kernel"]
    absent = frames["absent"]
    assert len(kernel.dropna(how="all")) > 0
    _assert_frames_identical(kernel, absent, "kernel path vs legacy path")


def test_enabled_kernel_matches_legacy_with_asymmetric_sells_and_tenure_lock():
    """Same equivalence with the two branches the default fixture never takes.

    `asymmetric_sells=True` (sell band = 60% of the threshold) and
    `min_holding_bars=5` (mother-position / recent-add tenure suppressions) are the
    two places `_step_asset_inertia` and `AssetPolicy.inertia_step` could drift, so
    the equivalence is re-proved with both enabled rather than assumed.
    """
    universe = _make_universe(160)
    params_extra = {
        "blend_asymmetric_sells": True,
        "blend_min_holding_bars": 5,
        "blend_min_weight_change": 0.03,
    }
    legacy = _run(universe, dict(params_extra))
    kernel = _run(universe, {**params_extra, _FLAG: True})
    assert len(kernel.dropna(how="all")) > 0, "no rebalance rows -- comparison would be vacuous"
    # Non-vacuity: the asymmetric/tenure knobs must actually change the frame,
    # otherwise the branches this test claims to cover are never reached.
    assert not legacy.equals(_run(universe)), (
        "the asymmetric-sells / tenure-lock parameters did not change the output -- "
        "those branches would not be exercised by this comparison"
    )
    _assert_frames_identical(kernel, legacy, "kernel vs legacy (asymmetric sells + tenure lock)")


# ---------------------------------------------------------------------------
# 3. sparse / cell-level contract
# ---------------------------------------------------------------------------


def test_kernel_path_preserves_sparse_nan_and_explicit_zero_contract(frames):
    kernel = frames["kernel"]
    risky = [c for c in kernel.columns if c != "BIL"]

    # Sparse: un-instructed days exist and are empty rows, not forward fills.
    assert kernel.isna().all(axis=1).any(), "expected all-NaN (non-rebalance) rows"
    assert not kernel.ffill().equals(kernel), "output was forward-filled inside the template"

    rebal = kernel.dropna(how="all")
    assert not rebal.empty
    # A rebalance row is DENSE for risky symbols: no NaN hole that the backtester's
    # column-wise ffill would silently turn into a stale held position.
    assert not rebal[risky].isna().any().any(), (
        "rebalance rows contain NaN for a risky symbol -- the backtester would carry "
        f"the previous weight instead: {rebal[risky].isna().sum()[rebal[risky].isna().sum() > 0].to_dict()}"
    )

    # An exit must be an EXPLICIT 0.0 on the rebalance row that instructs it.
    dense = kernel.ffill().fillna(0.0)
    exits = 0
    for prev_date, date in zip(rebal.index[:-1], rebal.index[1:]):
        prev_w = dense.loc[prev_date, risky]
        cur_w = dense.loc[date, risky]
        for symbol in risky:
            if float(prev_w[symbol]) > 1e-4 and float(cur_w[symbol]) <= 1e-4:
                exits += 1
                assert kernel.loc[date, symbol] == 0.0, (
                    f"{symbol} was exited on {date.date()} but the cell is "
                    f"{kernel.loc[date, symbol]!r}; it must be an explicit 0.0, never NaN"
                )
    assert exits >= 1, "no exit event in the fixture -- the explicit-0.0 assertion would be vacuous"


# ---------------------------------------------------------------------------
# 4. flag resolution: params first, then config; prefixed key only
# ---------------------------------------------------------------------------


def test_flag_resolution_order_and_prefixed_key_only():
    universe = _make_universe(120)
    baseline = _run(universe)
    kernel = _run(universe, {_FLAG: True})
    assert len(kernel.dropna(how="all")) > 0, "no rebalance rows -- resolution test would be vacuous"
    _assert_frames_identical(kernel, _run(universe, {_FLAG: True}), "params-driven kernel path")

    # (a) the StrategyConfig attribute is the second source
    cfg = StrategyConfig()
    cfg.blend_use_policy_kernel = True  # ad-hoc: `StrategyConfig` has no such field YET
    cfg_attr_kernel = _run(universe, None, cfg=cfg)
    _assert_frames_identical(cfg_attr_kernel, kernel, "config-attribute-driven kernel path")

    # (b) params win over the config attribute
    params_off = _run(universe, {_FLAG: False}, cfg=cfg)
    _assert_frames_identical(params_off, baseline, "params False must override config True")

    # (c) the BARE name is ignored in both sources -- the 2026-10-21 fallthrough bug
    bare_only = _run(universe, {"use_policy_kernel": True})
    _assert_frames_identical(bare_only, baseline, "bare 'use_policy_kernel' must be ignored (params)")
    cfg_bare = StrategyConfig()
    cfg_bare.use_policy_kernel = True
    _assert_frames_identical(
        _run(universe, None, cfg=cfg_bare),
        baseline,
        "bare 'use_policy_kernel' must be ignored (config attribute)",
    )


# ---------------------------------------------------------------------------
# 5. documented deployed exception (read-only check of the SSOT config)
# ---------------------------------------------------------------------------


def test_chan_four_state_blend_still_pinned_to_regime_ic_ir_hybrid():
    path = os.path.join(_PROJECT_ROOT, "research_strategy", "strategies_config.json")
    with open(path, encoding="utf-8") as fh:
        cfg_json = json.load(fh)
    params = cfg_json["chan_four_state_blend"]["parameters"]
    assert params.get("blend_weighting_mode") == "regime_ic_ir_hybrid", (
        "chan_four_state_blend's deployed weighting mode drifted to "
        f"{params.get('blend_weighting_mode')!r} (expected 'regime_ic_ir_hybrid')"
    )
    # The new flag must not have been baked into the deployed config by this phase.
    assert _FLAG not in params, (
        f"{_FLAG} was written into the deployed SSOT config; this phase must not flip "
        "the default anywhere"
    )


# ---------------------------------------------------------------------------
# 6. version pin
# ---------------------------------------------------------------------------


def test_base_version_bumped_for_the_opt_in_flag():
    assert BaseBlendAllocationStrategy.BASE_VERSION == "1.8.0"
    inst = GenericBlendStrategy()
    assert inst.base_version == "1.8.0"
    assert "base: v1.8.0" in inst.version_string


# ---------------------------------------------------------------------------
# kernel contract used by the flag-True branch (guards against a silent
# request-as-committed mistake when the wiring is refactored)
# ---------------------------------------------------------------------------


def test_sequencer_returns_committed_row_with_explicit_zero_on_exit():
    """`sequence` returns the COMMITTED row (dense, explicit 0.0 on an exit).

    A `Decision` is a per-symbol REQUEST whose `target_weight` is not
    cash-capped; only the sequencer's returned Series is authoritative. The blend
    wiring writes that Series (never a `Decision`) into `desired_weights`.
    """
    risky = ["A", "B"]
    ideal = pd.Series({"A": 0.0, "B": 0.30, "CASH": 0.70})
    current = pd.Series({"A": 0.50, "B": 0.20, "CASH": 0.30})
    committed = PortfolioSequencer().sequence(
        ideal,
        current,
        ctx=PolicyContext(bar_index=5),
        risky_symbols=risky,
        symbols=["A", "B", "CASH"],
        thresh_t=pd.Series({"A": 0.05, "B": 0.05}),
        holding_age={"A": 0, "B": 0},
        last_buy_bar={"A": 5, "B": 5},
        cfg=PositionPolicyConfig(),
        cash_proxy="CASH",
        em_row=None,
    )
    assert isinstance(committed, pd.Series)
    assert committed["A"] == 0.0, "an exit must be an explicit 0.0 (not NaN, not the stale weight)"
    assert not committed.isna().any(), "the committed row is dense"
    assert committed["A"] + committed["B"] + committed["CASH"] == pytest.approx(1.0, abs=1e-12)
