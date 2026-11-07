"""M4.2 -> M4.4 stage D1 -- `blend_use_policy_kernel`: the legacy path is GONE.

History: M4.2 wired the position-policy kernel behind `blend_use_policy_kernel`
and flipped its default ON in `BASE_VERSION 1.9.0`. M4.4 stage D1
(`BASE_VERSION 2.0.0`) DELETED the pre-M4.2 `_step_asset_inertia` branch and its
import, so the kernel is now the ONLY per-bar inertia step and the key is inert:
an explicit `False` logs a loud removal warning and the kernel runs anyway.

What this file pins, in one place:

  1. OUTPUT IDENTITY WITH THE PRE-M4.2 MODULE -- every arm (flag absent, explicit
     `False`, explicit `True`) equals the PRE-M4.2 module exactly: same cells, same
     `NaN` positions, same column order, `max_abs_diff == 0.0`. The parent module is
     loaded from a PINNED git blob, not from `HEAD:`. Why pinned: a
     `HEAD:`-based comparison would degrade into code-versus-itself once the change
     lands, and keep passing even if the kernel's committed arithmetic were
     rewritten. The pin (the last commit before the delegation) still contains the
     legacy step, which is what makes the comparison non-vacuous.
  2. INERT LEGACY KEY -- `blend_use_policy_kernel=False`, in `params` or on
     `StrategyConfig`, produces the SAME frame as flag-absent AND emits the removal
     warning naming `BASE_VERSION 2.0.0`. It must not be honoured silently (there
     is no legacy engine left to honour it with) and it must not be dropped
     silently either.
  3. SPARSE / CELL-LEVEL CONTRACT -- non-rebalance rows are all `NaN`, the output is
     never forward-filled by the template, a rebalance row carries NO `NaN` in a
     risky column, and a de-risked symbol is an explicit `0.0` (a `NaN` there would
     make the backtester's column-wise `ffill()` silently keep the stale position).
  4. KEY RESOLUTION -- the legacy key is read on the PREFIXED name only (that is
     why `False` still warns). The bare name `use_policy_kernel` must be ignored:
     the 2026-10-21 `wq_alpha_id` incident was a bare name falling through to a
     global `StrategyConfig` field and silently swapping a sub-strategy class.
  5. DEPLOYED CONFIG PIN -- `chan_four_state_blend` stays on
     `regime_ic_ir_hybrid` (documented exception, `docs/version.md` §8).
  6. VERSION PIN -- `BaseBlendAllocationStrategy.BASE_VERSION == "2.0.0"`
     (major: a flag branch was deleted, so the engine contract changed). A `1.9.0`
     stamp here would let a dump claim a two-path engine that no longer exists.

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
# `common/allocation_templates.py` as of the last commit BEFORE `apply_asset_inertia`
# delegated to the kernel -- i.e. the module that still CONTAINS the legacy
# `_step_asset_inertia` the pinned parent blend_base imports. Needed because M4.4
# stage D1 deleted that symbol from the live module: the pinned parent cannot be
# exec'd against it any more, and the parent arm must be paired with the module of
# its own era, not with a stub.
_TEMPLATES_PARENT_COMMIT = "ab82318"
_TEMPLATES_PARENT_PATH = "common/allocation_templates.py"
_FLAG = "blend_use_policy_kernel"

_PARENT_MODULE = None
_TEMPLATES_PARENT_MODULE = None


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


def _templates_parent_module():
    """Import the PRE-delegation `common.allocation_templates` from the pinned blob.

    Only used to satisfy the pinned parent `blend_base` blob's
    `from common.allocation_templates import ..., _step_asset_inertia`: that symbol
    no longer exists in the live module after M4.4 stage D1, and the reference arm
    must run the reference step, not a shim.
    """
    global _TEMPLATES_PARENT_MODULE
    if _TEMPLATES_PARENT_MODULE is not None:
        return _TEMPLATES_PARENT_MODULE

    try:
        proc = subprocess.run(
            ["git", "show", f"{_TEMPLATES_PARENT_COMMIT}:{_TEMPLATES_PARENT_PATH}"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:  # pragma: no cover
        pytest.skip(f"cannot read the pre-delegation allocation_templates blob: {exc}")

    src = proc.stdout
    assert "def _step_asset_inertia(" in src, (
        "the pinned allocation_templates blob no longer contains the legacy "
        "`_step_asset_inertia` step -- the reference arm would be vacuous"
    )
    assert "kernel_apply_asset_inertia" not in src, (
        "the pinned allocation_templates blob already imports the policy kernel; the "
        "reference must be the PRE-delegation implementation"
    )

    tmpdir = tempfile.mkdtemp(prefix="m4_2_templates_parent_")
    module_path = os.path.join(tmpdir, "allocation_templates_pre_m42.py")
    with open(module_path, "w", encoding="utf-8") as fh:
        fh.write(src)

    spec = importlib.util.spec_from_file_location("_templates_pre_m42", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _TEMPLATES_PARENT_MODULE = module
    return module


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
    # The pinned parent imports `_step_asset_inertia`, which the live module no
    # longer has (D1). Swap the pinned pre-delegation module in for the duration of
    # the exec so the reference arm is the reference code end to end, then restore.
    import common.allocation_templates as _live_templates

    _pinned_templates = _templates_parent_module()
    sys.modules["common.allocation_templates"] = _pinned_templates
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules["common.allocation_templates"] = _live_templates
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
    """Flag absent, legacy key `False` (inert since D1), explicit `True`, and the
    pinned pre-M4.2 parent module. All three live-engine arms are the kernel now."""
    universe = _make_universe(200)
    return {
        "universe": universe,
        "absent": _run(universe),
        "explicit_false": _run(universe, {_FLAG: False}),
        "kernel": _run(universe, {_FLAG: True}),
        "parent": _parent_strategy().generate_weights(universe),
    }


# ---------------------------------------------------------------------------
# 1. bit-identity vs. the pre-change module (the legacy step's last known output)
# ---------------------------------------------------------------------------


def test_disabled_path_is_bit_identical_to_pre_change_module(frames):
    """All three arms equal the pre-M4.2 output exactly -- the D1 non-regression gate.

    M4.4 stage D1 deleted the legacy step, so "which arm is running" is no longer a
    question: every arm is the kernel. What still has to hold is that DELETING the
    branch changed nothing a consumer can see -- that is what this pinned-blob
    comparison buys, to the last digit (`max_abs_diff == 0.0`, identical `NaN`
    footprint, identical column order), for flag absent, `False` and `True`.

    The parent blob is the last commit BEFORE the M4.2 delegation, i.e. the legacy
    `_step_asset_inertia` implementation itself. Keeping the arms while the engine
    they used to differentiate is gone is deliberate: the frame is the contract, and
    the arms are how a future refactor would show up.
    """
    absent = frames["absent"]
    parent = frames["parent"]

    # Non-vacuity guard: the comparison must actually contain rebalance rows.
    assert len(absent.dropna(how="all")) > 0, "no rebalance rows -- the comparison would be vacuous"
    _assert_frames_identical(absent, parent, "default path (flag absent) vs pre-M4.2 module")

    # The legacy key is still RECOGNISED (it warns) and still changes nothing.
    _assert_frames_identical(frames["explicit_false"], parent, "flag False vs pre-M4.2 module")
    _assert_frames_identical(frames["kernel"], parent, "flag True vs pre-M4.2 module")


# ---------------------------------------------------------------------------
# 2. the legacy key is inert -- and loud about it
# ---------------------------------------------------------------------------


def test_flag_false_is_inert_and_warns_after_legacy_removal(caplog):
    """`blend_use_policy_kernel=False` must not select a path (there is none) and
    must not be swallowed in silence.

    Two failure shapes this pins, and they are different:

      * a silent `False` -> the caller believes a legacy engine is running while the
        kernel runs (the 2026-11-02 "name and engine disagreeing" shape);
      * a raising `False` -> every persisted params file / live config still
        carrying the old opt-out fails closed for a key that no longer selects
        anything. D1 deliberately chose the warning (documented in
        `blend_base.generate_weights`), so the warning IS the contract now.
    """
    import logging

    universe = _make_universe(160)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        inert = _run(universe, {_FLAG: False})
    assert len(inert.dropna(how="all")) > 0, "no rebalance rows -- the warning arm would be vacuous"
    _assert_frames_identical(inert, _run(universe), "legacy key False vs flag absent")

    hits = [rec.getMessage() for rec in caplog.records if _FLAG in rec.getMessage()]
    assert len(hits) == 1, (
        f"expected exactly ONE removal warning for `{_FLAG}=False`, got {hits!r} -- "
        "an explicit opt-out must not be ignored silently after its path was deleted"
    )
    assert "2.0.0" in hits[0], (
        f"the removal warning does not name the version that removed the path: {hits[0]!r}"
    )

    # And the DEFAULT must stay quiet -- a warning on every run would be noise that
    # hides the real one.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _run(universe)
    assert not [rec for rec in caplog.records if _FLAG in rec.getMessage()], (
        "the flag-absent (default) run emitted the legacy-key warning"
    )


def test_asymmetric_sells_and_tenure_lock_remain_effective_on_the_kernel_path():
    """The two knobs the deleted branch used to read still reach the engine.

    `asymmetric_sells=True` (sell band = 60% of the threshold) and
    `min_holding_bars=5` (mother-position / recent-add tenure suppressions) were the
    two places the legacy step and `AssetPolicy.inertia_step` could drift, so D1
    keeps the non-vacuity check: with both on, the frame must differ from the
    default run (the knobs are not silently dropped), and the inert legacy key must
    still produce the identical frame.
    """
    universe = _make_universe(160)
    params_extra = {
        "blend_asymmetric_sells": True,
        "blend_min_holding_bars": 5,
        "blend_min_weight_change": 0.03,
    }
    flagged_false = _run(universe, {**params_extra, _FLAG: False})
    kernel = _run(universe, {**params_extra, _FLAG: True})
    assert len(kernel.dropna(how="all")) > 0, "no rebalance rows -- comparison would be vacuous"
    # Non-vacuity: the asymmetric/tenure knobs must actually change the frame,
    # otherwise the branches this test claims to cover are never reached.
    assert not kernel.equals(_run(universe)), (
        "the asymmetric-sells / tenure-lock parameters did not change the output -- "
        "those branches would not be exercised by this comparison"
    )
    _assert_frames_identical(kernel, flagged_false, "kernel vs inert legacy key (asymmetric + tenure)")
    _assert_frames_identical(kernel, _run(universe, params_extra), "flag True vs flag absent")


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


def test_flag_resolution_order_and_prefixed_key_only(caplog):
    """The legacy key is read on the PREFIXED name only, and it selects nothing.

    Post-D1 the key is inert, so "resolution" can only mean two observable things:
    the PREFIXED key is still what the engine looks at (`False` there is recognised
    and warned about, `params` first and then the `StrategyConfig` attribute), while
    the bare `use_policy_kernel` -- the 2026-10-21 fallthrough shape -- is never
    consulted at all (no warning, no branch).
    """
    import logging

    universe = _make_universe(120)
    kernel = _run(universe, {_FLAG: True})
    assert len(kernel.dropna(how="all")) > 0, "no rebalance rows -- resolution test would be vacuous"
    _assert_frames_identical(kernel, _run(universe, {_FLAG: True}), "params-driven kernel path")

    # (a) the StrategyConfig attribute is still read
    cfg = StrategyConfig()
    # Post-construction assignment: pins PRECEDENCE only; that the field actually
    # exists (constructor route) is covered by
    # `test_strategy_config_field_alone_selects_kernel_path`.
    cfg.blend_use_policy_kernel = True
    cfg_attr_kernel = _run(universe, None, cfg=cfg)
    _assert_frames_identical(cfg_attr_kernel, kernel, "config-attribute-driven kernel path")

    # (b) params are read BEFORE the config attribute -- observable as exactly ONE
    # warning when params say False and the config says True (two lookups, but the
    # params hit short-circuits the config read). The frame cannot differ: the key
    # selects nothing.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        params_off = _run(universe, {_FLAG: False}, cfg=cfg)
    _assert_frames_identical(params_off, kernel, "inert legacy key False must not change the frame")
    hits = [rec.getMessage() for rec in caplog.records if _FLAG in rec.getMessage()]
    assert len(hits) == 1, (
        f"expected exactly one warning for `{_FLAG}=False` in params (params read "
        f"first, config ignored), got {hits!r}"
    )

    # (c) the BARE name is ignored in both sources -- the 2026-10-21 fallthrough bug.
    # Concretely: it must not even trigger the prefixed-key warning.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        bare_only = _run(universe, {"use_policy_kernel": True})
    _assert_frames_identical(bare_only, kernel, "bare 'use_policy_kernel' must be ignored (params)")
    cfg_bare = StrategyConfig()
    cfg_bare.use_policy_kernel = True
    _assert_frames_identical(
        _run(universe, None, cfg=cfg_bare),
        kernel,
        "bare 'use_policy_kernel' must be ignored (config attribute)",
    )
    assert not [rec for rec in caplog.records if _FLAG in rec.getMessage()], (
        "a bare `use_policy_kernel` produced the PREFIXED-key warning -- the lookup is "
        "reading the bare name, which is exactly the 2026-10-21 fallthrough shape"
    )


# ---------------------------------------------------------------------------
# 5. documented deployed exception (read-only check of the SSOT config)
# ---------------------------------------------------------------------------


def test_strategy_config_field_alone_selects_kernel_path(monkeypatch, caplog):
    """`StrategyConfig(blend_use_policy_kernel=...)` -- the CONSTRUCTOR route.

    Distinct from `test_flag_resolution_order_and_prefixed_key_only` above, which
    reached the config source by *assigning an ad-hoc attribute after construction*:
    that works even when the dataclass has no such field, so it could not prove the
    field exists. Constructing with the keyword raises `TypeError` if the field is
    removed -- and that is exactly what this test now pins: the field SURVIVES D1 as
    a legacy key, is still read (both `True` and `False` reach the engine, the latter
    with the removal warning), and selects nothing, because there is no second path.

    Behavioural, not output-based, for the ROUTE: the spy proves
    `PortfolioSequencer.sequence` is called from `blend_base.py` in every case, and
    `blend_base` no longer exposing `_step_asset_inertia` proves the legacy engine is
    actually gone rather than merely unreachable.
    """
    import logging

    import pipeline.research.blend.blend_base as blend_base

    calls = {"kernel": 0, "delegated_helper": 0}
    original_sequence = PortfolioSequencer.sequence

    def spy_sequence(self, *args, **kwargs):
        # PROVENANCE split: `common.allocation_templates.apply_asset_inertia`
        # delegates to `kernel_apply_asset_inertia` unconditionally too, so the
        # kernel also runs for sub-strategies through that helper. What the D1 gate
        # owns is the BLEND ENGINE's own per-bar step, which lives in
        # `blend_base.py`; the split on the immediate caller's module keeps the two
        # separate.
        import inspect

        frame = inspect.currentframe().f_back
        caller = os.path.basename(frame.f_code.co_filename) if frame else ""
        if caller == "blend_base.py":
            calls["kernel"] += 1
        else:
            calls["delegated_helper"] += 1
        return original_sequence(self, *args, **kwargs)

    monkeypatch.setattr(PortfolioSequencer, "sequence", spy_sequence)

    assert not hasattr(blend_base, "_step_asset_inertia"), (
        "blend_base still exposes the legacy `_step_asset_inertia` step -- M4.4 "
        "stage D1 (BASE_VERSION 2.0.0) deleted that path and its import"
    )

    universe = _make_universe(160)

    # (a) the dataclass default is True and takes the (only) kernel path.
    default_cfg = StrategyConfig()
    assert default_cfg.blend_use_policy_kernel is True, (
        "StrategyConfig.blend_use_policy_kernel must default to True: the kernel is "
        "the only inertia engine since BASE_VERSION 2.0.0, so a False default would "
        "claim a legacy path that no longer exists"
    )
    on_default = _run(universe, None, cfg=default_cfg)
    assert len(on_default.dropna(how="all")) > 0, (
        "no rebalance rows -- the spy would be vacuous"
    )
    assert calls["kernel"] > 0, (
        "with the DEFAULT StrategyConfig, `PortfolioSequencer.sequence` was never "
        "called from `blend_base.py`: the per-bar inertia step is not wired"
    )
    assert calls["delegated_helper"] > 0, (
        "`apply_asset_inertia` never reached the kernel -- the commit-1b delegation is "
        "dead, so this run would not cover it at all"
    )

    # (b) `False` in params: still read, still loud, still the kernel.
    calls["kernel"] = 0
    calls["delegated_helper"] = 0
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        inert = _run(universe, {_FLAG: False}, cfg=StrategyConfig())
    assert len(inert.dropna(how="all")) > 0, "no rebalance rows -- the spy would be vacuous"
    _assert_frames_identical(inert, _run(universe, None), "legacy key False vs flag absent")
    assert calls["kernel"] > 0, (
        "with `blend_use_policy_kernel=False` in `params`, the kernel step was never "
        "called from `blend_base.py`: the legacy opt-out still bypasses the only engine"
    )
    assert any(
        _FLAG in rec.getMessage() and "2.0.0" in rec.getMessage()
        for rec in caplog.records
    ), (
        "`blend_use_policy_kernel=False` did not emit the BASE_VERSION 2.0.0 removal "
        f"warning: {[rec.getMessage() for rec in caplog.records]!r}"
    )

    # (c) constructor keyword alone (params carry no key): the config route is READ.
    calls["kernel"] = 0
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        off_cfg = _run(universe, None, cfg=StrategyConfig(blend_use_policy_kernel=False))
    assert len(off_cfg.dropna(how="all")) > 0, "no rebalance rows -- the spy would be vacuous"
    _assert_frames_identical(off_cfg, on_default, "config False vs default config")
    assert calls["kernel"] > 0, (
        "`StrategyConfig(blend_use_policy_kernel=False)` bypassed the kernel -- there is "
        "no legacy branch left, so this can only mean the step was skipped entirely"
    )
    assert any(
        _FLAG in rec.getMessage() and "2.0.0" in rec.getMessage()
        for rec in caplog.records
    ), (
        "`StrategyConfig(blend_use_policy_kernel=False)` produced no removal warning -- "
        "the StrategyConfig leg of the legacy-key lookup is no longer read"
    )


def test_chan_four_state_blend_still_pinned_to_regime_ic_ir_hybrid():
    path = os.path.join(_PROJECT_ROOT, "research_strategy", "strategies_config.json")
    with open(path, encoding="utf-8") as fh:
        cfg_json = json.load(fh)
    params = cfg_json["chan_four_state_blend"]["parameters"]
    assert params.get("blend_weighting_mode") == "regime_ic_ir_hybrid", (
        "chan_four_state_blend's deployed weighting mode drifted to "
        f"{params.get('blend_weighting_mode')!r} (expected 'regime_ic_ir_hybrid')"
    )
    # The default lives in `StrategyConfig`, not in the deployed SSOT config: a
    # per-strategy override here would mask the global default and make this file's
    # pin untestable.
    assert _FLAG not in params, (
        f"{_FLAG} was written into the deployed SSOT config; the flip belongs to "
        "`StrategyConfig` only, so a strategy-level copy would drift from it"
    )


# ---------------------------------------------------------------------------
# 6. version pin
# ---------------------------------------------------------------------------


def test_base_version_bumped_with_the_default_flip():
    """`1.9.0 -> 2.0.0` is owed BY the branch deletion, not by an optional feature.

    M4.2 flipped the default and paid with `1.8.0 -> 1.9.0`. M4.4 stage D1 then
    DELETED the alternative branch: the engine no longer has two paths, and a
    `blend_use_policy_kernel` key that used to select one now selects nothing (it
    only warns). That is a breaking base-engine contract change, hence a MAJOR
    bump -- a `1.9.0` stamp after D1 would let a dump advertise a two-path engine
    that cannot be reproduced, which is the 2026-11-02 "two version pairs" shape.
    """
    assert BaseBlendAllocationStrategy.BASE_VERSION == "2.0.0"
    inst = GenericBlendStrategy()
    assert inst.base_version == "2.0.0"
    assert "base: v2.0.0" in inst.version_string


# ---------------------------------------------------------------------------
# kernel contract used by the per-bar step (guards against a silent
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
