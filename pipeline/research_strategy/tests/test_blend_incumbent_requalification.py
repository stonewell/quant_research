"""Incumbent re-qualification rule (`BASE_VERSION 2.2.0`, 2026-11-20).

What this file pins, in one place:

  1. EXIT WRITES AN EXPLICIT `0.0` (Rule 3) -- a blend-only incumbent that has
     spent `incumbent_exit_bars` consecutive bars below `incumbent_min_roc` is
     released by writing a concrete Python `0.0` into the dense target row, which
     `SparseProjector.project` preserves as `0.0` in the rebalance row (never
     `NaN`, which the backtester's column-wise `ffill()` would read as "keep the
     stale position"). The name is NOT re-held afterwards: the slot is released,
     not merely re-shaded for one bar.
  2. SUB-STRATEGY HELD NAMES ARE UNTOUCHED -- a name a sub-strategy holds is
     excluded from the rule (`_blend_only`), even on the identical price path that
     DOES exit in test 1. That sub-strategy's own exit logic stays authoritative.
  3. FLAG OFF IS BYTE-IDENTICAL -- the default (`False`, flag absent) reproduces
     the PRE-CHANGE `blend_base.py` cell for cell (same index, same column order,
     same `NaN` footprint, `max_abs_diff == 0.0`). The pre-change module is read
     from a PINNED git blob, not from `HEAD:`, because a `HEAD:` comparison would
     become code-versus-itself once this lands. Explicit `False` is also pinned
     equal to flag-absent, which is what makes "off by default" a contract rather
     than a comment.
  4. `incumbent_exit_bars` IS A COUNT OF CONSECUTIVE BARS -- a single-bar ROC dip
     does not exit; the same dip does exit once the window is 1. The counter is
     derived from the price frame (`roc_matrix` -> rolling sum), so it carries NO
     state across `generate_weights` calls: two identical runs agree exactly.
  5. THE FORCED-EXIT CHANNEL PIERCES THE ADAPTIVE SELL BAND (second iteration).
     The rule above is not enough on its own: a position SMALLER than the band
     (`diff <= -(thresh * asymmetric_ratio)`, ~6% at the 2026-11-20 reading) never
     even becomes a sell CANDIDATE, so `asset_emergency_df` (consulted only after
     candidacy) is never reached -- the 600362.SH `0.054982` swallow. The explicit
     `forced_exits` channel makes candidacy true for THAT symbol only, never by
     zeroing the band for every other symbol on the bar.
  6. `forced_exits=None` IS INERT at both layers (sequencer and engine), and the
     live side channel (`_last_forced_exits`) stays an empty dict with the flag off.

100% offline: synthetic OHLCV only (`common.testing.make_ohlcv_from_closes`),
no network, no real market data.
"""

import importlib.util
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

from common.position_policy import PolicyContext, PortfolioSequencer, PositionPolicyConfig
from common.testing import make_ohlcv_from_closes
from pipeline.research.blend.blend_base import BaseBlendAllocationStrategy
from pipeline.research_strategy.rs.config import StrategyConfig

# Last commit BEFORE this change (2026-11-20). `blend_base.py` is unmodified at
# that commit, so it IS the pre-change engine. `HEAD:` must not be used here --
# after this lands it would compare the new code against itself.
_PRE_CHANGE_COMMIT = "6adb67e"
_PRE_CHANGE_PATH = "pipeline/research/blend/blend_base.py"
_FLAG = "enable_incumbent_requalification"

_SUBJECT = "600362.SH"  # the 2026-11-20 audit's 600362 shape (metals sector)
_PRE_CHANGE_MODULE = None


# ---------------------------------------------------------------------------
# synthetic universes (offline)
# ---------------------------------------------------------------------------


def _dates(n: int) -> pd.DatetimeIndex:
    return pd.bdate_range("2020-01-01", periods=n)


def _make_breakdown_universe(n_days: int = 140) -> dict:
    """`_SUBJECT` leads for 60 bars, then declines 0.4%/bar for the rest.

    Every other name compounds +0.2%/bar throughout, so breadth and the 10d thrust
    stay high (5/6 > 0.60) and the pre-emptive thrust deployment keeps running --
    which is what puts `_SUBJECT` into the book in the first place. No stops and no
    vol targeting are configured (see `_make_cfg`), so ONLY the re-qualification
    rule can release it.
    """
    t = np.arange(n_days)
    subject = 100.0 * np.cumprod(np.concatenate([np.full(60, 1.004), np.full(n_days - 60, 0.996)]))
    steady = 100.0 * np.cumprod(np.full(n_days, 1.002))
    universe = {
        "600000.SH": make_ohlcv_from_closes(steady),
        "601857.SH": make_ohlcv_from_closes(steady),
        "601111.SH": make_ohlcv_from_closes(steady),
        _SUBJECT: make_ohlcv_from_closes(subject),
        "000157.SZ": make_ohlcv_from_closes(steady),
        "600584.SH": make_ohlcv_from_closes(steady),
        "BIL": make_ohlcv_from_closes(np.full(n_days, 100.0)),
    }
    dates = _dates(n_days)
    for df in universe.values():
        df.index = dates
    return universe


def _make_single_dip_universe(n_days: int = 110) -> dict:
    """`_SUBJECT` rises, goes flat, takes ONE -4% bar, then recovers to flat.

    Its 10-bar ROC is therefore below zero on exactly one bar (`consec_max == 1`),
    which is what separates "1-bar dip" from N consecutive bars.
    """
    subject = np.concatenate([
        100.0 * np.cumprod(np.full(60, 1.004)),   # bars 0..59: rising (gets it bought)
    ])
    # ONE level scalar for every flat segment: deriving the flat level twice (e.g.
    # `1.004 ** 59` vs. the accumulated `cumprod`) leaves ~1e-14 of rounding error,
    # which would register as a genuinely negative 10-bar ROC and silently turn the
    # single-bar dip into a two- or three-bar one.
    level = float(subject[-1])
    subject = np.concatenate([
        subject,
        np.full(16, level),                      # bars 60..75: flat, ROC10 == 0.0 exactly
        [level * 0.96],                          # bar 76: the single-bar dip (-4%)
        np.full(n_days - 77, level),             # bars 77..: recovered to flat
    ])
    steady = 100.0 * np.cumprod(np.full(n_days, 1.002))
    universe = {
        "600000.SH": make_ohlcv_from_closes(steady),
        "601857.SH": make_ohlcv_from_closes(steady),
        "601111.SH": make_ohlcv_from_closes(steady),
        _SUBJECT: make_ohlcv_from_closes(subject),
        "000157.SZ": make_ohlcv_from_closes(steady),
        "600584.SH": make_ohlcv_from_closes(steady),
        "BIL": make_ohlcv_from_closes(np.full(n_days, 100.0)),
    }
    dates = _dates(n_days)
    for df in universe.values():
        df.index = dates
    return universe


# ---------------------------------------------------------------------------
# engines under test
# ---------------------------------------------------------------------------


def _make_cfg() -> StrategyConfig:
    """Deterministic engine: no stops, no vol targeting, no structural gates.

    `min_holding_bars=200` is what makes the incumbent STICKY once bought (the
    inertia kernel suppresses every sell for a young/tenure-locked position, so
    `raw_w` going to zero is not enough on its own) -- i.e. exactly the "占用槽位"
    pathology the rule fixes. `min_entry_bars=1` skips the entry confirmation gate so
    the deployment buys on its first qualifying bar.
    """
    cfg = StrategyConfig(cash_proxy="BIL")
    cfg.blend_stop_mode = "none"
    cfg.blend_enable_vol_targeting = False
    cfg.blend_enable_cyq_quality_gate = False
    cfg.blend_enable_structural_regime = False
    cfg.blend_min_entry_bars = 1
    cfg.blend_min_holding_bars = 200
    cfg.blend_min_weight_change = 0.05
    cfg.blend_max_single_position = 0.20
    cfg.blend_bull_max_single_position = 0.20
    cfg.blend_dynamic_cash_deployment = True
    cfg.blend_preemptive_thrust_deployment = True
    cfg.blend_weighting_mode = "static"
    return cfg


class _ConstantHoldingSub:
    """A sub-strategy that holds `_SUBJECT` at a fixed weight from bar 0 on."""

    def __init__(self, symbol: str, weight: float):
        self._symbol = symbol
        self._weight = weight

    def generate_weights(self, universe, params=None):
        idx = next(iter(universe.values())).index
        out = pd.DataFrame(np.nan, index=idx, columns=list(universe.keys()))
        out.iloc[0] = 0.0
        out.loc[idx[0], self._symbol] = self._weight
        return out


class _StubBlend(BaseBlendAllocationStrategy):
    """No sub-strategies: every holding is BLEND-ONLY by construction."""

    def __init__(self, config=None, version=None):
        super().__init__(name="stub_blend", param_prefix="blend", config=config, version=version)

    def get_sub_strategies(self, universe, params=None):
        return {}

    def get_sub_strategy_weights(self, params=None):
        return {}


class _SubHeldBlend(BaseBlendAllocationStrategy):
    """Identical engine, but `_SUBJECT` is held by a sub-strategy (NOT blend-only)."""

    def __init__(self, config=None, version=None):
        super().__init__(name="sub_held_blend", param_prefix="blend", config=config, version=version)

    def get_sub_strategies(self, universe, params=None):
        return {"const": _ConstantHoldingSub(_SUBJECT, 0.30)}

    def get_sub_strategy_weights(self, params=None):
        return {"const": 1.0}


def _run(universe: dict, params: dict, cls=_StubBlend) -> pd.DataFrame:
    return cls(config=_make_cfg()).generate_weights(universe, params)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _dense_book(sparse: pd.DataFrame) -> pd.DataFrame:
    """Reconstruct the delivered book by `ffill`ing the sparse frame (Rule 3 view)."""
    return sparse.ffill().fillna(0.0)


def test_blend_only_incumbent_negative_roc_exits_with_explicit_zero():
    """Test 1 -- the exit is a real, explicit `0.0` and the slot is never re-taken."""
    universe = _make_breakdown_universe()
    sparse = _run(universe, {_FLAG: True})
    book = _dense_book(sparse)

    # Non-vacuity: the name IS bought by the thrust deployment first.
    assert float(book[_SUBJECT].max()) > 1e-4, "the incumbent was never held -- comparison would be vacuous"

    # Precondition recomputed from prices: 3 consecutive bars with ROC10 < 0.
    closes = universe[_SUBJECT]["Close"]
    roc10 = closes / closes.shift(10) - 1.0
    consec = (roc10 < 0.0).rolling(3, min_periods=3).sum()
    assert float(consec.max()) >= 3, "no 3-bar negative-ROC run in the fixture"

    touched = sparse[_SUBJECT].dropna()
    exits = [d for d, v in touched.items() if float(v) == 0.0 and d != touched.index[0]]
    assert len(exits) == 1, f"expected exactly one requalification exit, got {[(d, v) for d, v in touched.items()]}"
    exit_date = exits[0]

    # RULE 3: a concrete 0.0 in the rebalance row -- not NaN, not absent.
    assert not pd.isna(sparse.loc[exit_date, _SUBJECT])
    assert float(sparse.loc[exit_date, _SUBJECT]) == 0.0

    # It was held the bar before, and is NOT re-held afterwards (slot released).
    assert float(book[book.index < exit_date][_SUBJECT].iloc[-1]) > 1e-4
    assert float(book.loc[exit_date:, _SUBJECT].abs().max()) == 0.0
    assert float(book.iloc[-1][_SUBJECT]) == 0.0

    # The exit is not earlier than the rule allows (first 3-bar run while held).
    assert exit_date >= consec[consec >= 3].index[0]

    # Contrast (non-vacuity): with the rule OFF the same name is still held at the end.
    book_off = _dense_book(_run(universe, {}))
    assert float(book_off.iloc[-1][_SUBJECT]) > 1e-4


def test_substrategy_held_name_unaffected():
    """Test 2 -- `_blend_only` guards the rule: a sub-strategy's holding is untouched."""
    universe = _make_breakdown_universe()
    sparse = _run(universe, {_FLAG: True}, cls=_SubHeldBlend)
    book = _dense_book(sparse)

    # Precondition: the identical price path DOES carry a >= 3-bar negative-ROC run,
    # so only the `_blend_only` guard can be what stops the exit here (test 1 exits
    # on exactly this path with no sub-strategy present).
    closes = universe[_SUBJECT]["Close"]
    roc10 = closes / closes.shift(10) - 1.0
    assert float((roc10 < 0.0).rolling(3, min_periods=3).sum().max()) >= 3

    touched = sparse[_SUBJECT].dropna()
    assert all(float(v) > 0.0 for v in touched.values), (
        f"a sub-strategy-held name was zeroed: {[(str(d.date()), float(v)) for d, v in touched.items()]}"
    )
    assert float(book.iloc[-1][_SUBJECT]) > 1e-4


def test_incumbent_requalification_off_by_default():
    """Test 3 -- flag off: identical to the PRE-CHANGE engine, cell for cell."""
    universe = _make_breakdown_universe()
    absent = _run(universe, {})
    explicit_false = _run(universe, {_FLAG: False})

    assert len(absent.dropna(how="all")) > 0, "no rebalance rows -- the comparison would be vacuous"

    # "Off by default" is a contract: absent == explicit False.
    pd.testing.assert_frame_equal(absent, explicit_false, check_exact=True)

    # ...and both equal the pre-change module read from the pinned blob.
    parent_frame = _pre_change_strategy(_make_cfg()).generate_weights(universe, {})
    assert list(absent.columns) == list(parent_frame.columns)
    assert absent.index.equals(parent_frame.index)
    assert absent.isna().equals(parent_frame.isna()), (
        "NaN footprint changed with the flag off -- a NaN and an explicit 0.0 are not "
        "interchangeable under the sparse contract"
    )
    diff = (absent - parent_frame).abs()
    max_abs_diff = float(diff.max().max()) if diff.notna().any().any() else 0.0
    assert max_abs_diff == 0.0, f"flag-off output drifted from the pre-change engine: {max_abs_diff!r}"

    # The rule is what differs, not some unrelated setting: flag ON releases the name.
    book_on = _dense_book(_run(universe, {_FLAG: True}))
    assert float(_dense_book(absent).iloc[-1][_SUBJECT]) > 1e-4
    assert float(book_on.iloc[-1][_SUBJECT]) == 0.0

    # Site-2 arm: the retention guard added next to the sector-incumbent benchmark
    # is UNCONDITIONAL, so it must be provably inert with the flag off. ATR stops are
    # what actually write `asset_emergency_df` cells (the guard's only input), so this
    # arm runs with stops ON -- still bit-identical to the pre-change engine.
    cfg_stops = _make_cfg()
    cfg_stops.blend_stop_mode = "atr_trailing"
    cfg_stops.blend_min_holding_bars = 2
    pd.testing.assert_frame_equal(
        _StubBlend(config=cfg_stops).generate_weights(universe, {}),
        _pre_change_strategy(cfg_stops).generate_weights(universe, {}),
        check_exact=True,
    )


def test_incumbent_requalification_respects_exit_bars():
    """Test 4 -- one dip is not enough; N consecutive bars are."""
    universe = _make_single_dip_universe()

    closes = universe[_SUBJECT]["Close"]
    roc10 = closes / closes.shift(10) - 1.0
    assert int((roc10 < 0.0).sum()) == 1, "fixture must carry exactly one negative-ROC bar"

    # 1-bar dip vs. the default 3-bar window: no exit.
    sparse3 = _run(universe, {_FLAG: True, "incumbent_exit_bars": 3})
    assert float(_dense_book(sparse3).iloc[-1][_SUBJECT]) > 1e-4, "a 1-bar dip must not release the incumbent"

    # Same dip with a 1-bar window: the exit fires on that bar, as an explicit 0.0.
    sparse1 = _run(universe, {_FLAG: True, "incumbent_exit_bars": 1})
    dip_date = roc10[roc10 < 0.0].index[0]
    assert not pd.isna(sparse1.loc[dip_date, _SUBJECT])
    assert float(sparse1.loc[dip_date, _SUBJECT]) == 0.0
    assert float(_dense_book(sparse1).loc[dip_date, _SUBJECT]) == 0.0

    # Stateless counter: the same inputs produce the same frame twice.
    pd.testing.assert_frame_equal(sparse1, _run(universe, {_FLAG: True, "incumbent_exit_bars": 1}), check_exact=True)


# ---------------------------------------------------------------------------
# the forced-exit channel (BASE_VERSION 2.2.0, second iteration)
# ---------------------------------------------------------------------------

#: The 2026-11-20 audit's delivered weight for 600362.SH, verbatim.
_SUB_BAND_WEIGHT = 0.054982
#: The adaptive band that swallowed it (`expanding_vol_ratio * 0.05` clipped).
_ADAPTIVE_BAND = 0.0596
_SUB_BAND_CONTROL = "600000.SH"


def test_forced_exit_pierces_adaptive_sell_band():
    """A sub-band position is released by `forced_exits`, and only by it.

    THE MEASURED DEFECT. The site-1 sweep wrote `raw_w = 0.0` and
    `asset_emergency_df = True` 66 times for `600362.SH`, yet the delivered
    weight stayed 0.054982: the position lives in the persistence layer, and the
    policy's sell candidacy is `diff <= -(thresh * asymmetric_ratio)` == -0.0596.
    A 5.4982% drop never clears that band, so `symbol_emergency` (consulted only
    AFTER candidacy) was never reached. The forced-exit channel makes candidacy
    itself true, without zeroing the band for every other symbol on the bar.

    Both directions are asserted here, on the same bar and the same band.
    """
    seq = PortfolioSequencer()
    cfg = PositionPolicyConfig(min_holding_bars=200)
    symbols = [_SUBJECT, _SUB_BAND_CONTROL]
    ideal = pd.Series({_SUBJECT: 0.0, _SUB_BAND_CONTROL: 0.0})
    current = pd.Series({_SUBJECT: _SUB_BAND_WEIGHT, _SUB_BAND_CONTROL: 0.05})
    thresh = pd.Series({_SUBJECT: _ADAPTIVE_BAND, _SUB_BAND_CONTROL: _ADAPTIVE_BAND})

    def _run(forced):
        return seq.sequence(
            ideal,
            current,
            ctx=PolicyContext(bar_index=10),
            risky_symbols=symbols,
            symbols=symbols,  # no cash proxy -> no residual write on this bar
            thresh_t=thresh,
            holding_age={s: 30 for s in symbols},
            last_buy_bar={s: 0 for s in symbols},
            cfg=cfg,
            em_row=None,  # NO emergency mask: only the new channel can act
            forced_exits=forced,
        )

    # (1) WITHOUT the channel the 5.5% drop is frozen (the swallow, reproduced).
    frozen = _run(None)
    assert float(frozen[_SUBJECT]) == pytest.approx(_SUB_BAND_WEIGHT)
    assert float(frozen[_SUB_BAND_CONTROL]) == pytest.approx(0.05)

    # (2) WITH it, the position lands on a CONCRETE 0.0 -- Rule 3: not NaN,
    # not "carried", and no future row can ffill the stale weight back in.
    released = _run({_SUBJECT})
    assert released.notna().all(), "a forced exit must never produce a NaN cell"
    assert float(released[_SUBJECT]) == 0.0
    assert released[_SUBJECT] == 0.0  # Python float, not np.nan-ish

    # (3) CONTROL: the band was NOT zeroed globally. The sibling sub-band symbol
    # stays frozen on the same bar.
    assert float(released[_SUB_BAND_CONTROL]) == pytest.approx(0.05)

    # (4) END-TO-END WIRING: the blend engine's site-1 sweep publishes exactly
    # this channel, keyed by the bar the exit was written on.
    stub = _StubBlend(config=_make_cfg())
    sparse = stub.generate_weights(_make_breakdown_universe(), {_FLAG: True})
    assert stub._last_forced_exits, "the flag-on engine published no forced-exit dates"
    published = {
        date: syms for date, syms in stub._last_forced_exits.items() if _SUBJECT in syms
    }
    assert published, f"{_SUBJECT} never appears in _last_forced_exits"
    for date in published:
        assert not pd.isna(sparse.loc[date, _SUBJECT])
        assert float(sparse.loc[date, _SUBJECT]) == 0.0
        assert date in sparse.dropna(how="all").index


def test_forced_exit_noop_when_none():
    """`forced_exits=None` is inert at BOTH layers.

    (a) the sequencer: omitting the kwarg, passing `None` and passing an empty
        set are the SAME call, cell for cell;
    (b) the engine: flag-off output is identical to the PRE-CHANGE module read
        from the pinned blob, and the live side channel is an EMPTY dict (so the
        runner's `getattr` finds nothing to pass on).
    """
    seq = PortfolioSequencer()
    cfg = PositionPolicyConfig(min_holding_bars=200)
    symbols = [_SUBJECT, _SUB_BAND_CONTROL]
    ideal = pd.Series({_SUBJECT: 0.0, _SUB_BAND_CONTROL: 0.05})
    current = pd.Series({_SUBJECT: _SUB_BAND_WEIGHT, _SUB_BAND_CONTROL: 0.05})
    thresh = pd.Series({_SUBJECT: _ADAPTIVE_BAND, _SUB_BAND_CONTROL: _ADAPTIVE_BAND})

    def _run(**kwargs):
        return seq.sequence(
            ideal,
            current,
            ctx=PolicyContext(bar_index=10),
            risky_symbols=symbols,
            symbols=symbols,
            thresh_t=thresh,
            holding_age={s: 30 for s in symbols},
            last_buy_bar={s: 0 for s in symbols},
            cfg=cfg,
            em_row=None,
            **kwargs,
        )

    omitted = _run()
    explicit_none = _run(forced_exits=None)
    empty = _run(forced_exits=set())
    pd.testing.assert_series_equal(omitted, explicit_none, check_exact=True)
    pd.testing.assert_series_equal(omitted, empty, check_exact=True)
    assert float(omitted[_SUBJECT]) == pytest.approx(_SUB_BAND_WEIGHT)

    # (b) Flag off == the pre-change engine, cell for cell, and no side channel.
    universe = _make_breakdown_universe()
    stub = _StubBlend(config=_make_cfg())
    flag_off = stub.generate_weights(universe, {})
    assert stub._last_forced_exits == {}, (
        "the flag-off engine populated the live forced-exit side channel"
    )
    parent = _pre_change_strategy(_make_cfg()).generate_weights(universe, {})
    pd.testing.assert_frame_equal(flag_off, parent, check_exact=True)

    # And the flag being explicitly False is the same contract.
    stub_false = _StubBlend(config=_make_cfg())
    flag_false = stub_false.generate_weights(universe, {_FLAG: False})
    assert stub_false._last_forced_exits == {}
    pd.testing.assert_frame_equal(flag_off, flag_false, check_exact=True)


# ---------------------------------------------------------------------------
# the pre-change engine (pinned git blob)
# ---------------------------------------------------------------------------


def _pre_change_strategy(cfg: StrategyConfig):
    """A no-sub-strategy stub running the PINNED pre-change `generate_weights`."""
    module = _pre_change_module()

    class _ParentStub(module.BaseBlendAllocationStrategy):
        def __init__(self, config=None, version=None):
            super().__init__(name="stub_blend", param_prefix="blend", config=config, version=version)

        def get_sub_strategies(self, universe, params=None):
            return {}

        def get_sub_strategy_weights(self, params=None):
            return {}

    return _ParentStub(config=cfg)


def _pre_change_module():
    """Load `blend_base.py` as of `_PRE_CHANGE_COMMIT` (the pre-change engine)."""
    global _PRE_CHANGE_MODULE
    if _PRE_CHANGE_MODULE is not None:
        return _PRE_CHANGE_MODULE

    try:
        proc = subprocess.run(
            ["git", "show", f"{_PRE_CHANGE_COMMIT}:{_PRE_CHANGE_PATH}"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:  # pragma: no cover
        pytest.skip(f"cannot read the pre-change blend_base blob from git: {exc}")

    src = proc.stdout
    assert src.strip(), "git show returned an empty blend_base source"
    assert _FLAG not in src, (
        f"the pinned blob already mentions {_FLAG!r}; the flag-off identity test would be "
        "comparing the new code against itself (vacuous)"
    )

    tmpdir = tempfile.mkdtemp(prefix="requal_parent_")
    module_path = os.path.join(tmpdir, "blend_base_pre_requal.py")
    with open(module_path, "w", encoding="utf-8") as fh:
        fh.write(src)

    spec = importlib.util.spec_from_file_location("_blend_base_pre_requal", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _PRE_CHANGE_MODULE = module
    return module


# ---------------------------------------------------------------------------
# sector-incumbent momentum qualification (`BASE_VERSION 2.3.0`, docs/version.md §54)
# ---------------------------------------------------------------------------
#
# THE DEFECT THESE PIN. The sector-incumbent protection (the `sec_incumbents`
# shortlist) exists to suppress CHURN: a held name whose live blend weight has just
# collapsed (`raw_w <= 0.01`) is kept for one more rebalance when the outside
# challenger does not beat it by `sector_incumbent_hurdle`. But the shortlist admitted
# ANY held name of the sector, and the retention branch then manufactured a positive
# score for it via `max(0.001, inc_roc)` -- so a name with NEGATIVE momentum that
# fails `r_val >= thrust_min_roc` (0.01) kept receiving a deployment slot.
# 600362.SH: ROC10 -11.99% / -10.42% / -13.02% on 2026-11-04 / 11-18 / 11-19
# (rank 18-19 of 20) and still retained.
#
# The fix is ONE predicate in the shortlist: the incumbent must clear the same
# momentum hurdle as every challenger (`roc >= max(thrust_min_roc, 0.0)`, NaN = not
# qualified). If the shortlist empties, the whole retention branch is skipped and the
# challenger is evaluated on its normal path.
#
# Fixture geometry (100% synthetic, offline): a metals INCUMBENT (`600362.SH`) held
# only by the blend (its sub-strategy drops it at `_SECTOR_DROP_BAR`), a metals
# CHALLENGER (`601899.SH`) whose ROC10 sits just above the 0.01 hurdle and inside the
# 0.03 incumbent hurdle, four rising fillers (so breadth/thrust stay high enough for
# the pre-emptive thrust deployment to run) and a flat cash proxy.

_INC = "600362.SH"        # metals, the incumbent under audit
_CHAL = "601899.SH"       # metals, the qualifying outside challenger
_ANCHOR = "600000.SH"     # banking; keeps the incumbent's delivered weight far below the cap
_THRUST_MIN_ROC = 0.01
_INCUMBENT_HURDLE = 0.03
#: Bar at which the incumbent's sub-strategy drops it: `raw_w` collapses to 0 while
#: the delivered book still holds it (inertia / `min_holding_bars`) -- exactly the
#: "blend-only incumbent whose live weight collapsed" the shortlist inspects.
_SECTOR_DROP_BAR = 70
#: Same shape, but the incumbent's ROC10 is +1.0% (QUALIFYING) at the drop bar.
_QUALIFY_DROP_BAR = 110


class _DropSub:
    """Holds `weights` until `drop_bar`, then 0.0 (a blend leg dropping its holdings)."""

    def __init__(self, weights: dict, drop_bar: int):
        self._weights = dict(weights)
        self._drop_bar = drop_bar

    def generate_weights(self, universe, params=None):
        idx = next(iter(universe.values())).index
        out = pd.DataFrame(np.nan, index=idx, columns=list(universe.keys()))
        out.iloc[0] = 0.0
        for sym, weight in self._weights.items():
            col = out.columns.get_loc(sym)
            out.iloc[: self._drop_bar, col] = weight
            out.iloc[self._drop_bar :, col] = 0.0
        return out


class _SectorBlend(BaseBlendAllocationStrategy):
    """Blend engine whose only sub-strategy drops the incumbent at `drop_bar`."""

    def __init__(self, config=None, drop_bar: int = _SECTOR_DROP_BAR):
        super().__init__(name="sector_stub", param_prefix="blend", config=config)
        self._drop_bar = drop_bar

    def get_sub_strategies(self, universe, params=None):
        return {"const": _DropSub({_INC: 0.05, _ANCHOR: 0.60}, self._drop_bar)}

    def get_sub_strategy_weights(self, params=None):
        return {"const": 1.0}


def _make_metals_universe(inc_closes, n_days: int = 140, nan_close_at=None):
    """Incumbent + challenger + four rising fillers + flat cash proxy (all synthetic)."""
    inc = np.asarray(inc_closes, dtype=float).copy()
    if nan_close_at is not None:
        inc[nan_close_at] = np.nan
    challenger = np.concatenate(
        [np.full(45, 100.0), 100.0 * np.cumprod(np.full(n_days - 45, 1.0012))]
    )
    steady = 100.0 * np.cumprod(np.full(n_days, 1.002))
    universe = {
        _ANCHOR: make_ohlcv_from_closes(steady),
        "601857.SH": make_ohlcv_from_closes(steady),
        "601111.SH": make_ohlcv_from_closes(steady),
        _INC: make_ohlcv_from_closes(inc),
        _CHAL: make_ohlcv_from_closes(challenger),
        "000157.SZ": make_ohlcv_from_closes(steady),
        "BIL": make_ohlcv_from_closes(np.full(n_days, 100.0)),
    }
    dates = _dates(n_days)
    for df in universe.values():
        df.index = dates
    return universe, dates


def _falling_incumbent(n_days: int = 140):
    """Incumbent declines 0.1%/bar => ROC10 ~= -0.9955% (fails the 0.01 hurdle)."""
    return 100.0 * np.cumprod(np.full(n_days, 0.999))


def _qualifying_incumbent(n_days: int = 140):
    """Incumbent's ROC10 ~= +1.005% at bar 110 while still BELOW its SMA50.

    Qualifying on momentum but trend-gated out of the candidate list (see
    `_TREND_GATE`), so the sector shortlist is the only path that can hand it a slot
    -- precisely the churn-suppression path that must keep working.
    """
    return 100.0 * np.cumprod(
        np.concatenate([np.full(60, 1.004), np.full(40, 0.9905), np.full(n_days - 100, 1.001)])
    )


#: `require_asset_trend` via the PREFIXED key (max_assets_per_sector-style globals are
#: never read: `resolve_param` step 1 hits `blend_require_asset_trend` first). Used to
#: keep an incumbent out of `cand_scores` so the sector shortlist is the only way in.
_TREND_GATE = {"blend_require_asset_trend": True}


def _roc10(universe: dict, symbol: str, dates) -> pd.Series:
    closes = pd.Series(np.asarray(universe[symbol]["Close"], dtype=float), index=dates)
    return closes.pct_change(10)


def _prechange_sector_blend(drop_bar: int = _SECTOR_DROP_BAR):
    """The SAME stub, but inheriting the PINNED pre-change `blend_base.py`."""
    module = _pre_change_module()

    class _PreSectorBlend(module.BaseBlendAllocationStrategy):
        def __init__(self, config=None):
            super().__init__(name="sector_stub", param_prefix="blend", config=config)

        def get_sub_strategies(self, universe, params=None):
            return {"const": _DropSub({_INC: 0.05, _ANCHOR: 0.60}, drop_bar)}

        def get_sub_strategy_weights(self, params=None):
            return {"const": 1.0}

    return _PreSectorBlend


def test_negative_momentum_incumbent_not_retained():
    """Test 5 (§54) -- a negative-ROC incumbent must not take the sector slot.

    Both directions are asserted on the same bar and the same shortlist: the PINNED
    pre-change engine hands the slot to the disqualified incumbent (starving the
    qualifying challenger at `0.0`), while the fixed engine hands it to the challenger
    and never re-fills the incumbent.
    """
    universe, dates = _make_metals_universe(_falling_incumbent())
    drop_date = dates[_SECTOR_DROP_BAR]

    inc_roc = _roc10(universe, _INC, dates)
    chal_roc = _roc10(universe, _CHAL, dates)
    # Preconditions, recomputed from prices (not read back from the engine): the
    # incumbent FAILS the hurdle, the challenger PASSES it, and the challenger is
    # inside the incumbent hurdle -- i.e. pre-change this is a retention decision.
    assert float(inc_roc.loc[drop_date]) < _THRUST_MIN_ROC
    assert float(chal_roc.loc[drop_date]) >= _THRUST_MIN_ROC
    assert float(chal_roc.loc[drop_date]) < float(inc_roc.loc[drop_date]) + _INCUMBENT_HURDLE

    fixed = _SectorBlend(config=_make_cfg()).generate_weights(universe, {})
    pre = _prechange_sector_blend()(config=_make_cfg()).generate_weights(universe, {})
    fixed_book, pre_book = _dense_book(fixed), _dense_book(pre)

    # Non-vacuity: the incumbent IS held on the bar before its weight collapses.
    assert float(fixed_book.loc[:drop_date, _INC].iloc[-2]) > 1e-4
    assert float(pre_book.loc[:drop_date, _INC].iloc[-2]) > 1e-4

    # FIXED: the sector slot goes to the qualifying challenger ...
    assert float(fixed.loc[drop_date, _CHAL]) > 1e-4, "the qualifying challenger received no sector slot"
    assert float(fixed_book.iloc[-1][_CHAL]) > 1e-4
    # ... and the disqualified incumbent is never re-filled above its blend weight.
    inc_after = fixed[_INC].dropna()
    inc_after = inc_after[inc_after.index >= drop_date]
    assert float(inc_after.max()) <= 0.05 + 1e-9, (
        f"a negative-momentum incumbent was re-filled: {float(inc_after.max())!r}"
    )

    # PRE-CHANGE (discriminating power): the SAME incumbent consumed the slot and the
    # challenger got nothing at all.
    assert float(pre.loc[drop_date, _CHAL]) == 0.0
    assert float(pre_book[_CHAL].abs().max()) == 0.0


def test_qualifying_incumbent_still_retained():
    """Test 6 (§54) -- a QUALIFYING incumbent keeps its slot; no churn regression."""
    universe, dates = _make_metals_universe(_qualifying_incumbent())
    drop_date = dates[_QUALIFY_DROP_BAR]

    inc_roc = _roc10(universe, _INC, dates)
    chal_roc = _roc10(universe, _CHAL, dates)
    # Precondition: the incumbent QUALIFIES (>= 0.01) and the challenger is inside the
    # incumbent hurdle -- the shortlist must therefore keep the incumbent.
    assert float(inc_roc.loc[drop_date]) >= _THRUST_MIN_ROC
    assert float(chal_roc.loc[drop_date]) < float(inc_roc.loc[drop_date]) + _INCUMBENT_HURDLE

    fixed = _SectorBlend(config=_make_cfg(), drop_bar=_QUALIFY_DROP_BAR).generate_weights(
        universe, _TREND_GATE)
    pre = _prechange_sector_blend(_QUALIFY_DROP_BAR)(config=_make_cfg()).generate_weights(
        universe, _TREND_GATE)
    fixed_book = _dense_book(fixed)

    # The incumbent keeps the metals slot (it is retained, not evicted) ...
    assert float(fixed_book.loc[drop_date, _INC]) > 0.15
    assert float(fixed_book.iloc[-1][_INC]) > 0.15
    # ... and the outside challenger is NOT churned in.
    assert float(fixed_book[_CHAL].abs().max()) == 0.0

    # No regression: for a qualifying incumbent the fix is a no-op, cell for cell.
    assert list(fixed.columns) == list(pre.columns)
    assert fixed.index.equals(pre.index)
    assert fixed.isna().equals(pre.isna())
    diff = (fixed - pre).abs()
    assert (float(diff.max().max()) if diff.notna().any().any() else 0.0) == 0.0


def test_nan_roc_incumbent_not_retained():
    """Test 7 (§54) -- a NaN ROC incumbent is NOT qualified.

    A NaN must not be read as "0.0% momentum" (which would clear a negative bound) and
    must not slip through comparison semantics: it is simply not qualified. The
    pre-change engine fell back to `inc_roc = 0.0` and retained it anyway.
    """
    universe, dates = _make_metals_universe(
        _falling_incumbent(), nan_close_at=_SECTOR_DROP_BAR - 10)
    drop_date = dates[_SECTOR_DROP_BAR]

    inc_roc = _roc10(universe, _INC, dates)
    chal_roc = _roc10(universe, _CHAL, dates)
    assert pd.isna(inc_roc.loc[drop_date]), "fixture must make ROC10 NaN exactly on the drop bar"
    assert float(chal_roc.loc[drop_date]) >= _THRUST_MIN_ROC

    fixed = _SectorBlend(config=_make_cfg()).generate_weights(universe, {})
    pre = _prechange_sector_blend()(config=_make_cfg()).generate_weights(universe, {})

    assert float(fixed.loc[drop_date, _CHAL]) > 1e-4, "a NaN-ROC incumbent blocked the challenger"
    inc_after = fixed[_INC].dropna()
    inc_after = inc_after[inc_after.index >= drop_date]
    assert float(inc_after.max()) <= 0.05 + 1e-9

    # PRE-CHANGE: NaN -> `inc_roc = 0.0` -> retention fired, challenger starved.
    assert float(pre.loc[drop_date, _CHAL]) == 0.0


def test_negative_thrust_min_roc_config_cannot_reopen_hole():
    """Test 8 (§54) -- a negative `thrust_min_roc` cannot re-open the hole.

    With `blend_thrust_min_roc = -0.5` every name clears the CANDIDACY hurdle, so the
    incumbent is kept out of `cand_scores` only by the trend gate; the sector shortlist
    then sees a -1% incumbent. Without the `max(..., 0.0)` floor that name would be
    retained (and the challenger skipped), so the -0.5 arm would diverge from the 0.0
    arm. The two arms are asserted identical, cell for cell.
    """
    universe, dates = _make_metals_universe(_falling_incumbent())
    drop_date = dates[_SECTOR_DROP_BAR]

    inc_roc = _roc10(universe, _INC, dates)
    assert float(inc_roc.loc[drop_date]) < 0.0, (
        "the floor only matters for a NEGATIVE-ROC incumbent"
    )

    floored = _SectorBlend(config=_make_cfg()).generate_weights(
        universe, dict(_TREND_GATE, blend_thrust_min_roc=0.0))
    negative = _SectorBlend(config=_make_cfg()).generate_weights(
        universe, dict(_TREND_GATE, blend_thrust_min_roc=-0.5))

    # The sign of the configured hurdle cannot change who is retained.
    pd.testing.assert_frame_equal(floored, negative, check_exact=True)

    # ... and in BOTH arms the challenger -- not the incumbent -- takes the sector slot.
    for label, frame in (("0.0", floored), ("-0.5", negative)):
        assert float(frame.loc[drop_date, _CHAL]) > 1e-4, f"hurdle {label}: challenger starved"
        assert float(frame.loc[drop_date, _INC]) <= 0.05 + 1e-9, f"hurdle {label}: incumbent retained"
