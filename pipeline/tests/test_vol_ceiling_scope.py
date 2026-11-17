"""2026-11-16 -- the Stage-1 vol CEILING's *scope*: an opt-in switch, never a default flip.

THE MEASURED PROBLEM
--------------------
`realized_vol_21d` is a CROSS-SECTIONAL MEDIAN of single-name volatilities
(2026-11-16: 0.3033) while `target_vol` is a PORTFOLIO target (0.12), so the ceiling compares
two different quantities. The offline ablation
(`scripts/run_vol_ceiling_ablation.py`, archived in
`backtests_research/20261116_131625_volscope_ablation/`, 2022-04..2026-11) measured what that
does: the status-quo scope binds on **100% of trading days**, CAGR 14.19% -> 6.04%, while the
Sharpe change (1.173 -> 1.128) has a paired block-bootstrap 95% CI of
**[-0.175, +0.162]** -- i.e. NOT significant. A portfolio-scoped cap binds on 17% of days with
CAGR 13.16% / Sharpe 1.129. With the documented decision rule requiring that CI to exclude 0,
the verdict is INCONCLUSIVE => the DEFAULT MUST NOT CHANGE.

WHAT THIS FILE PINS
-------------------
1. **No behaviour change by default.** An absent / `stage1` scope reproduces the Stage-1 factor
   and the capped weight vector BIT-FOR-BIT (exact series equality, and every pre-existing
   audit number equal to the direct `apply_portfolio_vol_ceiling` call), including when a
   hostile `book_scale_factor` is passed on the `stage1` path -- that argument is
   portfolio-scope-only.
2. **`portfolio` scope arithmetic.** On a synthetic 2-asset universe the covariance vol
   (`sqrt(252 * w' Σ w)`, ddof=1) and the resulting factor are independently recomputed, and
   the capped vector is the model book scaled by that factor.
3. **Unknown scope spelling** degrades to `stage1` and the rejection is recorded (in the
   resolution AND in the overlay audit block) instead of being silently swallowed.
4. **Precedence.** The strategy-PREFIXED key `cfsb_vol_ceiling_scope` wins over a colliding
   BARE spelling; `--vol-ceiling-scope` wins over the dump value and is labelled `override`.
   A non-allowlisted strategy records `stage1` with `source=default` (the gate's own default).
5. **Non-allowlisted strategies** still get `vol_overlay=None` and an untouched series, even
   when they declare a scope and pass portfolio-scope inputs.
6. **Honest banner.** The `stage1` binding alert gains a scope caption carrying the ablation
   numbers (and Stage-1's equal-weight comparison when the health report has it), and omits
   that clause -- without crashing -- when the diagnostic keys are absent.
7. **Two volatility numbers, never one (2026-11-16 NIT 7).** `realized_vol_21d` is ALWAYS
   Stage-1's own measurement in BOTH scopes, `consumed_vol_21d` is the number that produced
   the factor and `consumed_vol_source` (`stage1` | `portfolio`) says which. On the default
   `stage1` path the block is the pre-change block plus exactly those two ADDITIVE keys
   (canonical-JSON identical once they are pruned, and `consumed_vol_21d` equals the old
   `realized_vol_21d`); on `portfolio` only the reported `realized_vol_21d` changes --
   the capped series and every arithmetic key stay byte-for-byte what the old code
   produced (the archived 11.82% figure moves into `consumed_vol_21d`).

100% offline: synthetic frames and pandas Series only. The one `main()` test uses the
synthetic provider on a pinned past session and writes to `tmp_path` (same pattern as
`test_portfolio_vol_overlay.py`).
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.run_live_strategy import (  # noqa: E402
    VOL_CEILING_SCOPE_FALLBACK_REASON,
    VOL_CEILING_SCOPE_PARAM,
    VOL_CEILING_SCOPE_PORTFOLIO,
    VOL_CEILING_SCOPE_STAGE1,
    VOL_CEILING_SCOPE_UNKNOWN_REASON,
    VOL_CEILING_TARGET_BASE_VOL,
    VOL_CEILING_TARGET_BULL_VOL,
    apply_portfolio_vol_ceiling,
    apply_portfolio_vol_overlay_for_strategy,
    compute_portfolio_volatility,
    format_vol_scope_alert_line,
    record_vol_ceiling_scope_effective_param,
    resolve_target_vol_for_scope,
    resolve_vol_ceiling_factor,
    resolve_vol_ceiling_scope,
)
from common.effective_params import (  # noqa: E402
    SOURCE_DEFAULT,
    SOURCE_DUMP,
    SOURCE_OVERRIDE,
    build_effective_params,
)

CASH = "BIL"
ALLOWED = "chan_four_state_blend"

#: Archived Stage-1 numbers for the 2026-11-16 round (the round the ablation cross-checks).
ARCHIVED_VOL = 0.3033
ARCHIVED_FACTOR = 0.3957
ARCHIVED_EW_PORTFOLIO_VOL = 0.0994
ARCHIVED_SCOPE_RATIO = 3.05

#: The overlay's own audit keys that must exist on EVERY overlay (2026-11-16 additions).
SCOPE_AUDIT_KEYS = (
    "scope",
    "portfolio_vol_21d",
    "portfolio_vol_63d",
    "target_vol_used",
    "target_vol_source",
    "stage1_factor",
    "effective_factor",
    "scope_note",
    # (2026-11-16 NIT 7) the two ADDITIVE disambiguators: which number the factor consumed,
    # and which scope produced it.
    "consumed_vol_21d",
    "consumed_vol_source",
)

#: The two keys NIT 7 added -- the ONLY difference on the default `stage1` path.
CONSUMED_VOL_KEYS = ("consumed_vol_21d", "consumed_vol_source")

#: The full key set of a `stage1` overlay BEFORE NIT 7 (frozen so an accidental key
#: addition/removal cannot hide behind "the tests still pass"). It is
#: `apply_portfolio_vol_ceiling`'s own 12 keys + the 10 scope keys of 2026-11-16.
PRE_NIT7_STAGE1_KEYS = frozenset(
    {
        # -- apply_portfolio_vol_ceiling --
        "applied",
        "mode",
        "scope",
        "realized_vol_21d",
        "vol_scaling_factor",
        "model_equity_sum",
        "pre_cap_equity_sum",
        "capped_equity_sum",
        "equity_scale_factor",
        "binding",
        "reason",
        "superseded_by_circuit_breaker",
        # -- the 2026-11-16 scope stamping --
        "scope_applied",
        "stage1_factor",
        "portfolio_vol_21d",
        "portfolio_vol_63d",
        "target_vol_used",
        "target_vol_source",
        "scope_fallback_reason",
        "effective_factor",
        "scope_note",
    }
)


def _targets(weights):
    return pd.Series(dict(weights), dtype="float64")


def _eq_sum(series, cash_proxy=CASH):
    return float(sum(v for s, v in series.items() if str(s) != cash_proxy))


def _health_report(
    factor=ARCHIVED_FACTOR,
    realized=ARCHIVED_VOL,
    breadth=0.65,
    ew_vol=ARCHIVED_EW_PORTFOLIO_VOL,
    scope_ratio=ARCHIVED_SCOPE_RATIO,
):
    block = {
        "realized_vol_21d": realized,
        "vol_scaling_factor": factor,
        "vol_degraded": False,
        # (2026-11-16) Stage-1 scope diagnostic -- OPTIONAL upstream, read when present.
        "vol_portfolio_ew_21d": ew_vol,
        "vol_scope_ratio": scope_ratio,
    }
    return {"volatility_targeting": block, "market_macro": {"breadth": breadth}}


def _universe(symbols=("AAA.SH", "BBB.SH"), daily_vol=(0.03, 0.03), periods=90, seed=7):
    """Two independent random walks with a KNOWN daily sigma (Close-only frames)."""
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2026-06-01", periods=periods)
    universe = {}
    for sym, sigma in zip(symbols, daily_vol):
        rets = rng.normal(0.0, sigma, periods)
        close = 100.0 * np.cumprod(1.0 + rets)
        universe[sym] = pd.DataFrame(
            {
                "Open": close,
                "High": close,
                "Low": close,
                "Close": close,
                "Volume": np.full(periods, 1_000_000.0),
            },
            index=index,
        )
    return universe


def _expected_portfolio_vol(universe, weights, window, trading_days=252):
    """Independent recomputation: pct_change of ffilled closes -> sample cov (ddof=1)."""
    closes = pd.DataFrame(
        {sym: universe[sym]["Close"] for sym in weights}
    ).sort_index().ffill()
    rets = closes.pct_change().tail(window)
    cov = np.cov(rets.to_numpy(dtype=float), rowvar=False, ddof=1)
    w = np.array([weights[sym] for sym in weights], dtype=float)
    return float(np.sqrt(float(trading_days) * float(w @ cov @ w)))


# ---------------------------------------------------------------------------
# 1. default / absent scope == today's behaviour, BIT-FOR-BIT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("params", [{}, {VOL_CEILING_SCOPE_PARAM: "stage1"}])
def test_default_scope_reproduces_stage1_cap_bit_for_bit(params):
    targets = _targets({f"60000{i}.SH": 0.11 for i in range(4)})
    targets[CASH] = 0.0

    # The pre-existing (pre-2026-11-16) API with unchanged semantics is the reference.
    reference_capped, reference_summary = apply_portfolio_vol_ceiling(
        targets,
        cash_proxy=CASH,
        vol_scaling_factor=ARCHIVED_FACTOR,
        realized_vol_21d=ARCHIVED_VOL,
    )
    assert reference_summary["binding"] is True

    capped, summary = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=ALLOWED,
        params=params,
        health_report=_health_report(),
        cash_proxy=CASH,
    )

    # exact series equality -- no re-rounding, no extra epsilon
    pd.testing.assert_series_equal(capped, reference_capped, check_exact=True)
    for name in ("model_equity_sum", "pre_cap_equity_sum", "capped_equity_sum",
                 "equity_scale_factor", "binding", "applied", "reason",
                 "vol_scaling_factor", "realized_vol_21d"):
        assert summary[name] == reference_summary[name]
    assert _eq_sum(capped) == pytest.approx(ARCHIVED_FACTOR)
    # ... and the new keys say "stage1, nothing portfolio-scoped was computed"
    for key in SCOPE_AUDIT_KEYS:
        assert key in summary
    assert summary["scope"] == VOL_CEILING_SCOPE_STAGE1
    assert summary["scope_applied"] == VOL_CEILING_SCOPE_STAGE1
    assert summary["stage1_factor"] == ARCHIVED_FACTOR
    assert summary["effective_factor"] == summary["equity_scale_factor"]
    assert summary["portfolio_vol_21d"] is None
    assert summary["portfolio_vol_63d"] is None
    assert summary["target_vol_used"] is None
    assert summary["scope_fallback_reason"] is None


def test_default_scope_block_is_the_old_block_plus_two_additive_keys():
    """NIT 7: under `stage1` the block is BIT-IDENTICAL apart from the two new keys.

    Proven two ways, because "the old assertions still pass" would not catch a silent
    second edit: (1) the key set is exactly the FROZEN pre-NIT-7 set plus the two
    additions, and (2) the canonical JSON of the block PRUNED of those two keys equals the
    canonical JSON of the pre-change block, assembled here from the pre-existing API's own
    summary plus the 2026-11-16 scope keys.

    `scope_note` is taken from the overlay itself: its wording is pinned by the banner tests
    in section 6, and it is the one key the reference cannot rebuild without duplicating the
    Chinese literal.
    """
    targets = _targets({f"60000{i}.SH": 0.11 for i in range(4)})
    targets[CASH] = 0.0

    reference_capped, reference = apply_portfolio_vol_ceiling(
        targets,
        cash_proxy=CASH,
        vol_scaling_factor=ARCHIVED_FACTOR,
        realized_vol_21d=ARCHIVED_VOL,
    )
    capped, overlay = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=ALLOWED,
        params={},
        health_report=_health_report(),
        cash_proxy=CASH,
    )
    pd.testing.assert_series_equal(capped, reference_capped, check_exact=True)

    # (1) exactly two keys were added -- nothing else appeared, nothing disappeared
    assert set(overlay) == PRE_NIT7_STAGE1_KEYS | set(CONSUMED_VOL_KEYS)
    assert set(overlay) - set(CONSUMED_VOL_KEYS) == PRE_NIT7_STAGE1_KEYS

    # (2) the pruned block is the pre-change block, bit for bit after canonical JSON
    pruned = {k: v for k, v in overlay.items() if k not in CONSUMED_VOL_KEYS}
    expected = dict(reference)
    expected.update(
        mode=overlay["mode"],
        scope=VOL_CEILING_SCOPE_STAGE1,
        scope_applied=VOL_CEILING_SCOPE_STAGE1,
        stage1_factor=ARCHIVED_FACTOR,
        portfolio_vol_21d=None,
        portfolio_vol_63d=None,
        target_vol_used=None,
        target_vol_source=None,
        scope_fallback_reason=None,
        effective_factor=reference["equity_scale_factor"],
        scope_note=overlay["scope_note"],
    )
    assert json.dumps(pruned, sort_keys=True) == json.dumps(expected, sort_keys=True)

    # ... and the two additions carry the OLD meaning of `realized_vol_21d` verbatim
    assert overlay["realized_vol_21d"] == ARCHIVED_VOL
    assert overlay["consumed_vol_21d"] == ARCHIVED_VOL == reference["realized_vol_21d"]
    assert overlay["consumed_vol_source"] == VOL_CEILING_SCOPE_STAGE1


def test_stage1_scope_ignores_a_book_scale_factor():
    """`book_scale_factor` is portfolio-scope-only; passing it on stage1 must not move a weight."""
    targets = _targets({f"60000{i}.SH": 0.11 for i in range(4)})
    targets[CASH] = 0.0

    plain, plain_summary = apply_portfolio_vol_ceiling(
        targets, cash_proxy=CASH, vol_scaling_factor=ARCHIVED_FACTOR
    )
    hostile, hostile_summary = apply_portfolio_vol_ceiling(
        targets,
        cash_proxy=CASH,
        vol_scaling_factor=ARCHIVED_FACTOR,
        book_scale_factor=0.10,          # would cut the book to 4.4% if it were honoured
        scope=VOL_CEILING_SCOPE_STAGE1,
    )
    pd.testing.assert_series_equal(hostile, plain, check_exact=True)
    assert hostile_summary["capped_equity_sum"] == plain_summary["capped_equity_sum"]
    assert hostile_summary["capped_equity_sum"] == pytest.approx(ARCHIVED_FACTOR)


def test_stage1_with_foreign_portfolio_inputs_is_still_unchanged():
    """A `stage1` run must ignore portfolio vol / target inputs entirely."""
    targets = _targets({"600000.SH": 0.30, "600001.SH": 0.20, CASH: 0.0})
    capped_stage1, summary_stage1 = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=ALLOWED,
        params={},
        health_report=_health_report(),
        cash_proxy=CASH,
        scope=VOL_CEILING_SCOPE_STAGE1,
        portfolio_vol={"portfolio_vol_21d": 0.50, "portfolio_vol_63d": 0.50},
        target_vol_used=0.12,
        target_vol_source="breadth",
    )
    assert summary_stage1["scope"] == VOL_CEILING_SCOPE_STAGE1
    assert summary_stage1["portfolio_vol_21d"] is None
    assert _eq_sum(capped_stage1) == pytest.approx(ARCHIVED_FACTOR)


def test_scope_keys_are_present_even_when_nothing_was_capped():
    """The audit additions exist on EVERY overlay, including the 'no measurement' paths."""
    targets = _targets({"600000.SH": 0.80, CASH: 0.20})
    healthy = {"volatility_targeting": {"realized_vol_21d": 0.30,
                                        "vol_scaling_factor": 0.40,
                                        "vol_degraded": False}}
    cases = [
        # (label, health report, superseded_by, expected reason)
        ("unavailable", {}, None, "vol_unavailable"),
        ("superseded", healthy, "circuit_breaker_tier3", "circuit_breaker_tier3"),
        ("non_binding", healthy, None, None),
    ]
    for label, report, superseded, reason in cases:
        _, summary = apply_portfolio_vol_overlay_for_strategy(
            targets,
            strategy_key=ALLOWED,
            params={},
            health_report=report,
            cash_proxy=CASH,
            superseded_by=superseded,
        )
        assert summary is not None, label
        for key in SCOPE_AUDIT_KEYS + ("scope_applied", "scope_fallback_reason"):
            assert key in summary, f"{label}: missing {key}"
        if reason:
            assert summary["reason"] == reason, label
        assert summary["scope"] == VOL_CEILING_SCOPE_STAGE1
        assert summary["stage1_factor"] == summary["vol_scaling_factor"]


# ---------------------------------------------------------------------------
# 2. portfolio scope: covariance vol + factor
# ---------------------------------------------------------------------------

def test_portfolio_scope_vol_matches_independent_covariance():
    universe = _universe()
    weights = {"AAA.SH": 0.30, "BBB.SH": 0.20}
    targets = _targets({**weights, CASH: 0.70})

    info = compute_portfolio_volatility(targets, universe, cash_proxy=CASH)

    expected_21 = _expected_portfolio_vol(universe, weights, 21)
    expected_63 = _expected_portfolio_vol(universe, weights, 63)
    assert info["unavailable_reason"] is None
    assert info["n_symbols"] == 2
    assert info["missing_symbols"] == []
    assert info["portfolio_vol_21d"] == pytest.approx(expected_21, rel=1e-12)
    assert info["portfolio_vol_63d"] == pytest.approx(expected_63, rel=1e-12)
    # the synthetic daily sigma is 3% -> ~17% annualised for this 2-name book, i.e. NOT the
    # 30%+ single-name scale the stage1 scope reports
    assert 0.10 < info["portfolio_vol_21d"] < 0.25


def test_portfolio_scope_factor_scales_the_book_and_is_reported():
    universe = _universe()
    weights = {"AAA.SH": 0.30, "BBB.SH": 0.20}
    targets = _targets({**weights, CASH: 0.50})
    expected_21 = _expected_portfolio_vol(universe, weights, 21)
    expected_63 = _expected_portfolio_vol(universe, weights, 63)
    expected_factor = min(1.0, VOL_CEILING_TARGET_BASE_VOL / expected_21)
    assert expected_factor < 1.0        # the cap must actually bind for this assertion to mean something

    capped, summary = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=ALLOWED,
        params={VOL_CEILING_SCOPE_PARAM: VOL_CEILING_SCOPE_PORTFOLIO},
        health_report=_health_report(breadth=0.55),        # -> bear branch: target 0.12
        cash_proxy=CASH,
        portfolio_vol=compute_portfolio_volatility(targets, universe, cash_proxy=CASH),
        target_vol_used=VOL_CEILING_TARGET_BASE_VOL,
        target_vol_source="breadth",
    )

    assert summary["scope"] == VOL_CEILING_SCOPE_PORTFOLIO
    assert summary["scope_applied"] == VOL_CEILING_SCOPE_PORTFOLIO
    assert summary["portfolio_vol_21d"] == pytest.approx(expected_21, rel=1e-12)
    assert summary["portfolio_vol_63d"] == pytest.approx(expected_63, rel=1e-12)
    assert summary["target_vol_used"] == pytest.approx(VOL_CEILING_TARGET_BASE_VOL)
    assert summary["target_vol_source"] == "breadth"
    assert summary["stage1_factor"] == pytest.approx(ARCHIVED_FACTOR)
    assert summary["effective_factor"] == pytest.approx(expected_factor, rel=1e-9)
    # the ceiling is the BOOK scaled by the factor -- NOT `min(book, factor)` (which would
    # leave 0.50 < factor untouched only if factor were huge; here it would leave 0.50
    # untouched because 0.50 < 1.0, i.e. silently inert)
    assert summary["binding"] is True
    # `capped_equity_sum` is rounded to the ticket's effective-weight decimals (8dp)
    assert summary["capped_equity_sum"] == pytest.approx(0.50 * expected_factor, abs=1e-8)
    assert capped["AAA.SH"] == pytest.approx(0.30 * expected_factor, rel=1e-8)
    assert capped["BBB.SH"] == pytest.approx(0.20 * expected_factor, rel=1e-8)
    assert capped[CASH] == pytest.approx(0.50)     # cash leg is the caller's to recompute


def test_portfolio_scope_uses_the_bull_target_when_breadth_is_high():
    universe = _universe(daily_vol=(0.006, 0.006))     # calm book: no cap should bind
    weights = {"AAA.SH": 0.30, "BBB.SH": 0.20}
    targets = _targets({**weights, CASH: 0.50})

    capped, summary = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=ALLOWED,
        params={VOL_CEILING_SCOPE_PARAM: VOL_CEILING_SCOPE_PORTFOLIO},
        health_report=_health_report(breadth=0.65),
        cash_proxy=CASH,
        portfolio_vol=compute_portfolio_volatility(targets, universe, cash_proxy=CASH),
        target_vol_used=VOL_CEILING_TARGET_BULL_VOL,
        target_vol_source="breadth",
    )
    assert summary["target_vol_used"] == pytest.approx(VOL_CEILING_TARGET_BULL_VOL)
    assert summary["effective_factor"] == pytest.approx(1.0)
    assert summary["binding"] is False
    assert summary["effective_factor"] == 1.0
    pd.testing.assert_series_equal(capped, targets, check_exact=True)


def test_portfolio_scope_without_usable_vols_falls_back_to_stage1():
    universe = _universe()
    weights = {"AAA.SH": 0.30, "BBB.SH": 0.20}
    targets = _targets({**weights, CASH: 0.50})

    # BBB.SH is held but has no frame at all -> the aggregate is incomplete, never guessed.
    partial = compute_portfolio_volatility(
        targets, {"AAA.SH": universe["AAA.SH"]}, cash_proxy=CASH
    )
    assert partial["portfolio_vol_21d"] is None
    assert partial["missing_symbols"] == ["BBB.SH"]
    assert partial["unavailable_reason"] == "missing_price_history"

    capped, summary = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=ALLOWED,
        params={VOL_CEILING_SCOPE_PARAM: VOL_CEILING_SCOPE_PORTFOLIO},
        health_report=_health_report(),
        cash_proxy=CASH,
        portfolio_vol=partial,
        target_vol_used=VOL_CEILING_TARGET_BASE_VOL,
        target_vol_source="breadth",
    )
    assert summary["scope"] == VOL_CEILING_SCOPE_PORTFOLIO     # what was ASKED for
    assert summary["scope_applied"] == VOL_CEILING_SCOPE_STAGE1  # what actually happened
    assert summary["scope_fallback_reason"] == VOL_CEILING_SCOPE_FALLBACK_REASON
    assert summary["portfolio_vol_21d"] is None
    # the stage1 factor is used -- the same numbers a stage1 round would produce
    stage1_capped, stage1_summary = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=ALLOWED,
        params={},
        health_report=_health_report(),
        cash_proxy=CASH,
    )
    pd.testing.assert_series_equal(capped, stage1_capped, check_exact=True)
    assert summary["capped_equity_sum"] == stage1_summary["capped_equity_sum"]


def test_portfolio_scope_reports_the_stage1_median_and_names_the_consumed_vol():
    """NIT 7: under `portfolio`, `realized_vol_21d` is Stage-1's 30.33%, not the 11.82%.

    THE DEFECT THIS PINS: the ticket used to report the CONSUMED portfolio vol under
    `realized_vol_21d`, so Stage-1's own 30.33% survived only as a factor
    (`stage1_factor`). An auditor comparing the Stage-1 report with the ticket saw two
    different quantities under one name and no way to tell them apart.
    """
    universe = _universe()
    weights = {"AAA.SH": 0.30, "BBB.SH": 0.20}
    targets = _targets({**weights, CASH: 0.50})

    vol_info = compute_portfolio_volatility(targets, universe, cash_proxy=CASH)
    expected_21 = vol_info["portfolio_vol_21d"]
    expected_factor = min(1.0, VOL_CEILING_TARGET_BASE_VOL / expected_21)
    assert expected_factor < 1.0

    capped, overlay = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=ALLOWED,
        params={VOL_CEILING_SCOPE_PARAM: VOL_CEILING_SCOPE_PORTFOLIO},
        health_report=_health_report(),
        cash_proxy=CASH,
        portfolio_vol=vol_info,
        target_vol_used=VOL_CEILING_TARGET_BASE_VOL,
        target_vol_source="breadth",
    )

    # Stage-1's own measurement survives under its own name, in this scope too
    assert overlay["realized_vol_21d"] == pytest.approx(ARCHIVED_VOL)
    # ... and the number that actually produced the factor is named, with its provenance
    assert overlay["consumed_vol_21d"] == pytest.approx(expected_21, rel=1e-12)
    assert overlay["consumed_vol_21d"] == overlay["portfolio_vol_21d"]
    assert overlay["consumed_vol_source"] == VOL_CEILING_SCOPE_PORTFOLIO
    # the factor semantics are untouched
    assert overlay["stage1_factor"] == pytest.approx(ARCHIVED_FACTOR)
    assert overlay["vol_scaling_factor"] == pytest.approx(expected_factor, rel=1e-9)
    assert overlay["effective_factor"] == pytest.approx(expected_factor, rel=1e-9)
    assert overlay["binding"] is True

    # CAPPED SERIES UNCHANGED vs. the pre-fix code: that code passed the portfolio vol as
    # `realized_vol_21d`; rebuild exactly that call and compare, exactly.
    old_capped, old_summary = apply_portfolio_vol_ceiling(
        targets,
        cash_proxy=CASH,
        vol_scaling_factor=expected_factor,
        realized_vol_21d=expected_21,          # the old (portfolio-vol) value
        book_scale_factor=expected_factor,
        scope=VOL_CEILING_SCOPE_PORTFOLIO,
    )
    pd.testing.assert_series_equal(capped, old_capped, check_exact=True)
    for key in (
        "applied",
        "mode",
        "scope",
        "vol_scaling_factor",
        "model_equity_sum",
        "pre_cap_equity_sum",
        "capped_equity_sum",
        "equity_scale_factor",
        "binding",
        "reason",
        "superseded_by_circuit_breaker",
    ):
        assert overlay[key] == old_summary[key], key
    # only the REPORTED number moved: 11.82% -> 30.33%
    assert old_summary["realized_vol_21d"] == pytest.approx(expected_21)
    assert overlay["realized_vol_21d"] != pytest.approx(expected_21)
    # the weights themselves are still the model book scaled by the portfolio factor
    assert capped["AAA.SH"] == pytest.approx(0.30 * expected_factor, rel=1e-8)
    assert capped["BBB.SH"] == pytest.approx(0.20 * expected_factor, rel=1e-8)
    assert capped[CASH] == pytest.approx(0.50)


def test_realized_vol_is_reporting_only_so_it_cannot_move_a_single_weight():
    """The old and new `realized_vol_21d` argument must be arithmetic-inert, in both scopes.

    This is what makes the NIT 7 change reporting-only BY CONSTRUCTION rather than by
    inspection: the same cap called with 0.1182 (old portfolio value) and 0.3033 (Stage-1
    median) produces an identical series and an identical audit block apart from the one
    key that carries the input.
    """
    targets = _targets({"600000.SH": 0.30, "600001.SH": 0.20, CASH: 0.50})
    for kwargs in (
        {"scope": VOL_CEILING_SCOPE_PORTFOLIO, "book_scale_factor": 0.60},
        {"scope": VOL_CEILING_SCOPE_STAGE1},
    ):
        old_capped, old_summary = apply_portfolio_vol_ceiling(
            targets, cash_proxy=CASH, vol_scaling_factor=0.60,
            realized_vol_21d=0.1182, **kwargs
        )
        new_capped, new_summary = apply_portfolio_vol_ceiling(
            targets, cash_proxy=CASH, vol_scaling_factor=0.60,
            realized_vol_21d=ARCHIVED_VOL, **kwargs
        )
        label = kwargs["scope"]
        pd.testing.assert_series_equal(new_capped, old_capped, check_exact=True)
        assert {
            k: v for k, v in new_summary.items() if k != "realized_vol_21d"
        } == {k: v for k, v in old_summary.items() if k != "realized_vol_21d"}, label
        assert old_summary["realized_vol_21d"] == 0.1182, label
        assert new_summary["realized_vol_21d"] == ARCHIVED_VOL, label


def test_consumed_vol_source_follows_the_scope_that_actually_produced_the_factor():
    """A degraded portfolio request consumed the Stage-1 median -> `stage1`, not `portfolio`."""
    universe = _universe()
    targets = _targets({"AAA.SH": 0.30, "BBB.SH": 0.20, CASH: 0.50})
    partial = compute_portfolio_volatility(
        targets, {"AAA.SH": universe["AAA.SH"]}, cash_proxy=CASH
    )
    _, degraded = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=ALLOWED,
        params={VOL_CEILING_SCOPE_PARAM: VOL_CEILING_SCOPE_PORTFOLIO},
        health_report=_health_report(),
        cash_proxy=CASH,
        portfolio_vol=partial,
        target_vol_used=VOL_CEILING_TARGET_BASE_VOL,
        target_vol_source="breadth",
    )
    assert degraded["scope"] == VOL_CEILING_SCOPE_PORTFOLIO       # what was asked
    assert degraded["scope_applied"] == VOL_CEILING_SCOPE_STAGE1  # what ran
    assert degraded["consumed_vol_source"] == VOL_CEILING_SCOPE_STAGE1
    assert degraded["consumed_vol_21d"] == pytest.approx(ARCHIVED_VOL)
    assert degraded["consumed_vol_21d"] == degraded["realized_vol_21d"]

    # no measurement at all -> both numbers are NULL, never a fabricated 0.0 / 1.0x
    _, unavailable = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=ALLOWED,
        params={},
        health_report={},
        cash_proxy=CASH,
    )
    assert unavailable["reason"] == "vol_unavailable"
    assert unavailable["realized_vol_21d"] is None
    assert unavailable["consumed_vol_21d"] is None
    assert unavailable["consumed_vol_source"] == VOL_CEILING_SCOPE_STAGE1


def test_target_vol_resolution_reads_breadth_and_reports_absence():
    assert resolve_target_vol_for_scope({"market_macro": {"breadth": 0.65}}) == (
        VOL_CEILING_TARGET_BULL_VOL,
        "breadth",
    )
    assert resolve_target_vol_for_scope({"market_macro": {"breadth": 0.59}}) == (
        VOL_CEILING_TARGET_BASE_VOL,
        "breadth",
    )
    # top-level spelling is accepted too
    assert resolve_target_vol_for_scope({"breadth": 0.60})[0] == VOL_CEILING_TARGET_BULL_VOL
    # absent / unparsable breadth -> base target AND an honest source label
    assert resolve_target_vol_for_scope({}) == (
        VOL_CEILING_TARGET_BASE_VOL,
        "breadth_unavailable_default",
    )
    assert resolve_target_vol_for_scope(None)[1] == "breadth_unavailable_default"
    assert resolve_target_vol_for_scope({"market_macro": {"breadth": "n/a"}})[1] == (
        "breadth_unavailable_default"
    )


def test_factor_helper_caps_at_one_and_requires_both_windows():
    info = resolve_vol_ceiling_factor(
        scope=VOL_CEILING_SCOPE_PORTFOLIO,
        stage1_factor=ARCHIVED_FACTOR,
        portfolio_vol={"portfolio_vol_21d": 0.60, "portfolio_vol_63d": 0.55},
        target_vol_used=0.12,
        target_vol_source="breadth",
    )
    assert info["effective_factor"] == pytest.approx(0.12 / 0.60)
    assert info["effective_factor"] <= 1.0

    calm = resolve_vol_ceiling_factor(
        scope=VOL_CEILING_SCOPE_PORTFOLIO,
        stage1_factor=ARCHIVED_FACTOR,
        portfolio_vol={"portfolio_vol_21d": 0.05, "portfolio_vol_63d": 0.06},
        target_vol_used=0.24,
        target_vol_source="breadth",
    )
    assert calm["effective_factor"] == 1.0          # never levers up

    half = resolve_vol_ceiling_factor(
        scope=VOL_CEILING_SCOPE_PORTFOLIO,
        stage1_factor=ARCHIVED_FACTOR,
        portfolio_vol={"portfolio_vol_21d": 0.20, "portfolio_vol_63d": None},
        target_vol_used=0.12,
    )
    assert half["effective_factor"] is None
    assert half["scope_fallback_reason"] == VOL_CEILING_SCOPE_FALLBACK_REASON


# ---------------------------------------------------------------------------
# 3. unknown spelling -> stage1 + recorded reason
# ---------------------------------------------------------------------------


def test_unknown_scope_falls_back_to_stage1_and_records_the_reason():
    resolution = resolve_vol_ceiling_scope(
        ALLOWED, {VOL_CEILING_SCOPE_PARAM: "portfolio_vol"}
    )
    assert resolution["scope"] == VOL_CEILING_SCOPE_STAGE1
    assert resolution["fallback_reason"] == VOL_CEILING_SCOPE_UNKNOWN_REASON
    # the rejected spelling stays visible -- a typo must be readable, not silent
    assert resolution["declared"] == "portfolio_vol"

    targets = _targets({"600000.SH": 0.30, "600001.SH": 0.20, CASH: 0.0})
    capped, summary = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=ALLOWED,
        params={VOL_CEILING_SCOPE_PARAM: "portfolio_vol"},
        health_report=_health_report(),
        cash_proxy=CASH,
    )
    assert summary["scope"] == VOL_CEILING_SCOPE_STAGE1
    assert summary["scope_applied"] == VOL_CEILING_SCOPE_STAGE1
    assert summary["scope_fallback_reason"] == VOL_CEILING_SCOPE_UNKNOWN_REASON
    assert summary["portfolio_vol_21d"] is None
    # and the cap still behaves exactly like stage1
    assert _eq_sum(capped) == pytest.approx(ARCHIVED_FACTOR)


def test_unknown_scope_via_literal_kwarg_is_also_rejected():
    resolution = resolve_vol_ceiling_scope(ALLOWED, {}, cli_value="portfolio!")
    assert resolution["scope"] == VOL_CEILING_SCOPE_STAGE1
    assert resolution["fallback_reason"] == VOL_CEILING_SCOPE_UNKNOWN_REASON
    assert resolution["source"] == SOURCE_OVERRIDE


# ---------------------------------------------------------------------------
# 4. precedence: prefixed key > colliding bare field; CLI > dump
# ---------------------------------------------------------------------------


def test_prefixed_key_wins_over_colliding_bare_field():
    # a bare `vol_ceiling_scope` / `portfolio_vol_ceiling_scope` must never be read
    mixed = resolve_vol_ceiling_scope(
        ALLOWED,
        {"vol_ceiling_scope": "portfolio", "portfolio_vol_ceiling_scope": "portfolio"},
    )
    assert mixed["scope"] == VOL_CEILING_SCOPE_STAGE1
    assert mixed["source"] == SOURCE_DEFAULT
    assert mixed["declared"] is None

    prefixed = resolve_vol_ceiling_scope(
        ALLOWED,
        {"vol_ceiling_scope": "stage1", VOL_CEILING_SCOPE_PARAM: "portfolio"},
    )
    assert prefixed["scope"] == VOL_CEILING_SCOPE_PORTFOLIO
    assert prefixed["source"] == SOURCE_DUMP

    # ... and the bare collision cannot change an ORDER either
    targets = _targets({"600000.SH": 0.30, "600001.SH": 0.20, CASH: 0.0})
    capped, summary = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=ALLOWED,
        params={"vol_ceiling_scope": "portfolio"},
        health_report=_health_report(),
        cash_proxy=CASH,
    )
    assert summary["scope_applied"] == VOL_CEILING_SCOPE_STAGE1
    assert _eq_sum(capped) == pytest.approx(ARCHIVED_FACTOR)


@pytest.mark.parametrize(
    "declared,cli,expected_scope,expected_source",
    [
        ("stage1", "portfolio", VOL_CEILING_SCOPE_PORTFOLIO, SOURCE_OVERRIDE),
        ("portfolio", "stage1", VOL_CEILING_SCOPE_STAGE1, SOURCE_OVERRIDE),
        ("portfolio", None, VOL_CEILING_SCOPE_PORTFOLIO, SOURCE_DUMP),
        (None, None, VOL_CEILING_SCOPE_STAGE1, SOURCE_DEFAULT),
    ],
)
def test_cli_override_wins_over_the_dump_value(declared, cli, expected_scope, expected_source):
    params = {} if declared is None else {VOL_CEILING_SCOPE_PARAM: declared}
    resolution = resolve_vol_ceiling_scope(
        ALLOWED, params, cli_value=cli, config_origin=SOURCE_DUMP
    )
    assert resolution["scope"] == expected_scope
    assert resolution["source"] == expected_source
    assert resolution["cli_override"] is (cli is not None)


def test_scope_is_registered_as_a_provenance_triple_beside_the_overlay_mode():
    entries = {}
    resolution = record_vol_ceiling_scope_effective_param(
        entries,
        {VOL_CEILING_SCOPE_PARAM: "portfolio"},
        {VOL_CEILING_SCOPE_PARAM: "portfolio"},
        ALLOWED,
        config_origin=SOURCE_DUMP,
    )
    assert entries[VOL_CEILING_SCOPE_PARAM] == {
        "declared": "portfolio",
        "resolved": "portfolio",
        "source": SOURCE_DUMP,
    }
    # the returned dict IS what main() hands to the overlay, so the snapshot and the cap
    # cannot disagree
    assert resolution["scope"] == entries[VOL_CEILING_SCOPE_PARAM]["resolved"]

    overridden = {}
    record_vol_ceiling_scope_effective_param(
        overridden,
        {},
        {},
        ALLOWED,
        cli_value="portfolio",
        config_origin=SOURCE_DUMP,
    )
    assert overridden[VOL_CEILING_SCOPE_PARAM] == {
        "declared": None,
        "resolved": "portfolio",
        "source": SOURCE_OVERRIDE,
    }

    payload = build_effective_params(
        strategy_id=ALLOWED,
        strategy_version="1.6.0",
        base_version="1.1.0",
        config_source="strategies_config.json:chan_four_state_blend",
        strategy_dump_sha256="0" * 64,
        as_of="2026-11-16",
        run_id="2026-11-16_180000",
        params=entries,
    )
    assert payload["params"][VOL_CEILING_SCOPE_PARAM]["resolved"] == "portfolio"
    assert payload["params"][VOL_CEILING_SCOPE_PARAM]["source"] == SOURCE_DUMP


def test_non_allowlisted_strategy_registers_stage1_as_the_gates_default():
    entries = {}
    resolution = record_vol_ceiling_scope_effective_param(
        entries,
        {VOL_CEILING_SCOPE_PARAM: "portfolio"},
        {VOL_CEILING_SCOPE_PARAM: "portfolio"},
        "turtle_breakout",
        config_origin=SOURCE_DUMP,
    )
    assert entries[VOL_CEILING_SCOPE_PARAM] == {
        "declared": "portfolio",
        "resolved": VOL_CEILING_SCOPE_STAGE1,
        "source": SOURCE_DEFAULT,
    }
    assert resolution["allowlisted"] is False


# ---------------------------------------------------------------------------
# 5. non-allowlisted strategies: no overlay, untouched series
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy_key", ["chan_risk_managed_blend", "turtle_breakout", ""])
def test_non_allowlisted_strategy_still_gets_no_overlay(strategy_key):
    targets = _targets({"600000.SH": 0.30, "600001.SH": 0.20, CASH: 0.50})
    capped, summary = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=strategy_key,
        params={VOL_CEILING_SCOPE_PARAM: "portfolio"},
        health_report=_health_report(),
        cash_proxy=CASH,
        scope=VOL_CEILING_SCOPE_PORTFOLIO,
        portfolio_vol={"portfolio_vol_21d": 0.40, "portfolio_vol_63d": 0.40},
        target_vol_used=0.12,
        target_vol_source="breadth",
    )
    assert summary is None            # -> no `vol_overlay` key on the ticket
    assert capped is targets          # -> same object, weights untouched


# ---------------------------------------------------------------------------
# 6. honest banner on a binding stage1 ceiling
# ---------------------------------------------------------------------------


def _binding_stage1_overlay():
    targets = _targets({"600000.SH": 0.30, "600001.SH": 0.20, CASH: 0.0})
    _, summary = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=ALLOWED,
        params={},
        health_report=_health_report(),
        cash_proxy=CASH,
    )
    assert summary["binding"] is True
    return summary


def test_binding_stage1_banner_states_the_scope_and_the_ablation_numbers():
    line = format_vol_scope_alert_line(_binding_stage1_overlay(), _health_report())
    assert line is not None
    assert "口径提示" in line
    assert "单名波动率横截面中位数" in line
    assert "30.3%" in line
    # Stage-1's own same-window comparison is quoted when the health report carries it
    assert "9.9%" in line and "3.05x" in line
    # the measured semantics, verbatim from the archived ablation
    assert "CAGR 14.19%->6.04%" in line
    assert "Sharpe 1.173->1.128" in line
    assert "差异不显著" in line
    assert "不等于风险改善" in line


def test_banner_omits_the_comparison_clause_when_the_diagnostic_is_absent():
    report = {"volatility_targeting": {"realized_vol_21d": ARCHIVED_VOL}}
    line = format_vol_scope_alert_line(_binding_stage1_overlay(), report)
    assert line is not None and "口径提示" in line
    assert "同窗口等权组合口径" not in line
    assert "3.05x" not in line
    # no health report at all must not crash
    assert format_vol_scope_alert_line(_binding_stage1_overlay(), None) is not None


def test_banner_is_silent_for_portfolio_scope_and_for_non_binding_rounds():
    portfolio_summary = dict(_binding_stage1_overlay())
    portfolio_summary.update(scope=VOL_CEILING_SCOPE_PORTFOLIO,
                             scope_applied=VOL_CEILING_SCOPE_PORTFOLIO)
    assert format_vol_scope_alert_line(portfolio_summary, _health_report()) is None

    non_binding = dict(_binding_stage1_overlay(), binding=False)
    assert format_vol_scope_alert_line(non_binding, _health_report()) is None
    assert format_vol_scope_alert_line(None, _health_report()) is None


# ---------------------------------------------------------------------------
# 7. end-to-end: the key reaches `effective_params.json` (non-allowlisted template)
# ---------------------------------------------------------------------------


def test_main_writes_the_scope_key_into_effective_params(tmp_path, monkeypatch):
    import scripts.run_live_strategy as runner

    strategy_file = tmp_path / "strategy.json"
    strategy_file.write_text(
        json.dumps({"template_name": "equal_weight", "params": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    monkeypatch.delenv("LIVETRADING_PREFER_INSTANCE_STATE", raising=False)
    runner.main(
        [
            "--strategy-file",
            str(strategy_file),
            "--data-provider",
            "synthetic",
            "--as-of-date",
            "2026-11-06",
            "--output-dir",
            str(out_dir),
        ]
    )

    params = json.loads((out_dir / "effective_params.json").read_text(encoding="utf-8"))["params"]
    assert params[VOL_CEILING_SCOPE_PARAM] == {
        "declared": None,
        "resolved": VOL_CEILING_SCOPE_STAGE1,
        "source": SOURCE_DEFAULT,
    }
