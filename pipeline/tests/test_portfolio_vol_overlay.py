"""`LV18` wiring -- Stage-1's Barroso factor becomes a CEILING on the equity budget.

FORENSIC CONTEXT
----------------
`scripts/check_live_portfolio_health.py::evaluate_realized_volatility` has always computed
`vol_scaling_factor = clip(target_vol / realized_vol_21d)` (target_vol = 0.12) and archived it
(`2026-11-11/205137`: `realized_vol_21d = 0.3240854096084951`, factor `0.3702727628033721`),
but nothing in Stage 2 consumed it -- `docs/development_plan.md` LV18 registered that the
number was PRESENTED as an applied risk action while being completely inert.

WHY THE WIRING IS A CAP AND NOT A MULTIPLICATION
------------------------------------------------
`pipeline/research/blend/blend_base.py` already applies its own Garman-Klass target-vol
scaling (`blend_target_vol = 0.12`), which is why the model's own equity target on that
archived round was **0.463** rather than a fully invested book. Multiplying the two
independent estimates of "how much risk to take" would ask the book to hold
`0.463 * 0.370 = 0.171` of NAV -- i.e. de-risk the SAME elevated-volatility observation
twice. The overlay therefore asks for

    equity_budget = min(model_equity_sum, vol_scaling_factor)

WHY IT SCALES THE TARGET WEIGHT VECTOR (not already-built ticket rows)
---------------------------------------------------------------------
Measured on a synthetic replay of the 2026-11-11 book: a pass over BUILT ROWS cannot
de-risk an already-invested book, because `normalize_model_target_weights`' contract forbids
inventing a SELL when a scaled target falls below the held quantity. That pass left the
delivered book at 78.6% while the row claimed a 3.7% target (`partial=true`) -- the exact
"presented as applied, actually inert" shape LV18 exists to kill. Emitting SELLs from the
row pass would be worse (it would bypass `step_position_lifecycle`'s `can_sell`, i.e. China
T+1, plus the tenure lock). So the cap is applied to the TARGET VECTOR -- the same place
`apply_r1_intraday_derisk` and Stage-1's Tier-2 / linear-damping defenses operate.

WHAT IS PINNED HERE
-------------------
1. 0.44 model equity + 0.37 factor -> budget 0.37, `binding=True`, explicitly NOT the
   product (0.163);
2. 0.30 model equity + 0.37 factor -> budget 0.30, `binding=False`, series untouched;
3. a missing / degraded factor -> series untouched, `applied=False`,
   `reason="vol_unavailable"` -- a missing measurement must never default to 1.0x;
4. the config switch OFF -> no audit block at all and the series object is returned as-is;
5. a non-allowlisted strategy -> same as (4): other strategies are unaffected.
6. delivery shortfall: a ceiling that whole lots cannot deliver (0.037 capped vs 0.0048
   delivered) is flagged + printed, while 0.37 vs 0.35 is not -- report-only, no weight is
   enlarged and `weight_budget_ok` keeps its 100%-budget meaning;
7. ordering vs. the Stage-1 defenses: Tier-2 damping + vol cap applies the STRICTER scale
   (never the product), and a Tier-3 halt that already zeroed the book is attributed to the
   breaker (`superseded_by_circuit_breaker`), never to the vol cap.

8. AT8 PROVENANCE (2026-11-12 defect): the ticket carried `vol_overlay.applied = true` while
   `effective_params.json::params` held neither `cfsb_portfolio_vol_overlay_enabled` nor
   `cfsb_portfolio_vol_overlay_mode`, so the "why was a cap on this round?" question could not
   be answered from the snapshot. The two keys are now registered through the SAME resolver the
   run executes, with honest `source` labels, and a NON-allowlisted strategy records them too
   (`enabled=False`, `mode="off"`, `source="default"`) so the snapshot also explains absence.

100% offline: pandas Series only, no market data, no network, no filesystem (the one `main()`
test uses the synthetic provider and a pinned past session, like `test_effective_params.py`).
"""

import json
import os
import sys

import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.run_live_strategy import (  # noqa: E402
    PORTFOLIO_VOL_OVERLAY_MODE_OFF,
    VOL_OVERLAY_DELIVERY_SHORTFALL_RATIO,
    VOL_CEILING_FORCE_EXITS_PARAM,
    VOL_OVERLAY_ENABLED_PARAM,
    VOL_OVERLAY_MODE_PARAM,
    VOL_OVERLAY_SUPERSEDED_BY_CIRCUIT_BREAKER,
    VOL_OVERLAY_UNAVAILABLE_REASON,
    apply_portfolio_vol_ceiling,
    apply_portfolio_vol_overlay_for_strategy,
    evaluate_vol_overlay_delivery,
    format_vol_overlay_delivery_warning,
    record_portfolio_vol_overlay_effective_params,
    resolve_portfolio_vol_overlay_params,
    resolve_portfolio_vol_overlay_provenance,
    resolve_stage1_vol_inputs,
)
from common.effective_params import (  # noqa: E402
    SOURCE_DEFAULT,
    SOURCE_DUMP,
    SOURCE_OVERRIDE,
    build_effective_params,
)

CASH = "BIL"

#: Archived Stage-1 numbers for `2026-11-11/205137` (tagged "15:00 口径" by the operator).
ARCHIVED_VOL = 0.3240854096084951
ARCHIVED_FACTOR = 0.3702727628033721

#: The deployed `chan_four_state_blend` book: 权益 46.3% (archived 11-11/205137).
ARCHIVED_MODEL_EQUITY = 0.463


def _targets(weights):
    return pd.Series(dict(weights), dtype="float64")


def _eq_sum(series, cash_proxy=CASH):
    return float(sum(v for s, v in series.items() if str(s) != cash_proxy))


def _params(enabled=True, mode="cap"):
    return {
        "cfsb_portfolio_vol_overlay_enabled": enabled,
        "cfsb_portfolio_vol_overlay_mode": mode,
    }


def _health_report(factor=ARCHIVED_FACTOR, realized=ARCHIVED_VOL, **overrides):
    block = {
        "realized_vol_21d": realized,
        "vol_scaling_factor": factor,
        "vol_degraded": False,
    }
    block.update(overrides)
    return {"volatility_targeting": block}


# ---------------------------------------------------------------------------
# 1. 0.44 model equity + 0.37 factor -> budget 0.37 (NOT 0.163)
# ---------------------------------------------------------------------------


def test_cap_binds_at_factor_and_is_not_a_multiplication():
    targets = _targets({f"60000{i}.SH": 0.11 for i in range(4)})
    targets[CASH] = 0.0

    capped, summary = apply_portfolio_vol_ceiling(
        targets, cash_proxy=CASH, vol_scaling_factor=0.37
    )

    assert summary["applied"] is True
    assert summary["mode"] == "cap"
    assert summary["binding"] is True
    assert summary["model_equity_sum"] == pytest.approx(0.44)
    # cap semantics: the ceiling is the FACTOR itself, not the product
    assert summary["capped_equity_sum"] == pytest.approx(0.37)
    assert _eq_sum(capped) == pytest.approx(0.37)
    assert summary["capped_equity_sum"] != pytest.approx(0.44 * 0.37)
    assert _eq_sum(capped) != pytest.approx(0.44 * 0.37)
    # each equity name is scaled proportionally; cash is NOT rescaled here (the caller
    # recomputes the implied cash residual right after)
    for i in range(4):
        assert capped[f"60000{i}.SH"] == pytest.approx(0.0925)
    assert capped[CASH] == pytest.approx(0.0)
    # the input series is never mutated
    assert _eq_sum(targets) == pytest.approx(0.44)


def test_archived_11_11_numbers_would_be_0_171_if_multiplied():
    """The archived round (46.3% model equity, 0.3703 factor) pins the double-scaling hazard."""
    targets = _targets({f"60000{i}.SH": ARCHIVED_MODEL_EQUITY / 4 for i in range(4)})
    capped, summary = apply_portfolio_vol_ceiling(
        targets,
        cash_proxy=CASH,
        vol_scaling_factor=ARCHIVED_FACTOR,
        realized_vol_21d=ARCHIVED_VOL,
    )
    assert summary["binding"] is True
    assert _eq_sum(capped) == pytest.approx(ARCHIVED_FACTOR)
    assert _eq_sum(capped) > 0.34          # nowhere near 0.171
    assert _eq_sum(capped) != pytest.approx(ARCHIVED_MODEL_EQUITY * ARCHIVED_FACTOR)


# ---------------------------------------------------------------------------
# 2. 0.30 model equity + 0.37 factor -> 0.30, cap not binding
# ---------------------------------------------------------------------------


def test_cap_does_not_bind_when_model_book_is_below_the_ceiling():
    targets = _targets({f"00000{i}.SZ": 0.10 for i in range(3)})
    targets[CASH] = 0.0

    capped, summary = apply_portfolio_vol_ceiling(
        targets, cash_proxy=CASH, vol_scaling_factor=0.37
    )

    assert summary["applied"] is True
    assert summary["binding"] is False
    assert summary["model_equity_sum"] == pytest.approx(0.30)
    assert summary["capped_equity_sum"] == pytest.approx(0.30)
    assert summary["equity_scale_factor"] is None
    # bit-identical weights: the overlay consulted, then declined to touch anything
    pd.testing.assert_series_equal(capped, targets, check_exact=True)


# ---------------------------------------------------------------------------
# 3. degraded / missing factor -> no scaling, applied=False, reason=vol_unavailable
# ---------------------------------------------------------------------------


def test_missing_factor_keeps_model_weights_and_marks_unavailable():
    targets = _targets({"600000.SH": 0.80, CASH: 0.0})

    capped, summary = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key="chan_four_state_blend",
        params=_params(),
        health_report=_health_report(factor=None, realized=None),
        cash_proxy=CASH,
    )

    assert summary is not None
    assert summary["applied"] is False
    assert summary["reason"] == VOL_OVERLAY_UNAVAILABLE_REASON
    assert summary["binding"] is False
    # the model's own sum is still reported, so the ticket says what was NOT capped
    assert summary["model_equity_sum"] == pytest.approx(0.80)
    assert summary["vol_scaling_factor"] is None
    pd.testing.assert_series_equal(capped, targets, check_exact=True)


def test_degraded_stage1_neutral_1_0x_is_not_read_as_a_measurement():
    """D7's degradation path labels a NEUTRAL 1.0x -- the overlay must not treat it as calm."""
    degraded_report = {
        "volatility_targeting": {
            "realized_vol_21d": None,
            "vol_scaling_factor": 1.0,
            "vol_degraded": True,
            "vol_scaling_fallback": "insufficient_history",
        }
    }
    vol = resolve_stage1_vol_inputs(degraded_report)
    assert vol["available"] is False
    assert vol["reason"] == VOL_OVERLAY_UNAVAILABLE_REASON
    assert vol["realized_vol_21d"] is None
    assert vol["vol_scaling_factor"] is None

    targets = _targets({"600000.SH": 0.80})
    capped, summary = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key="chan_four_state_blend",
        params=_params(),
        health_report=degraded_report,
        cash_proxy=CASH,
    )
    assert summary["applied"] is False
    assert summary["reason"] == VOL_OVERLAY_UNAVAILABLE_REASON
    pd.testing.assert_series_equal(capped, targets, check_exact=True)


def test_empty_health_report_is_unavailable_not_neutral():
    assert resolve_stage1_vol_inputs(None)["reason"] == VOL_OVERLAY_UNAVAILABLE_REASON
    assert resolve_stage1_vol_inputs({})["available"] is False


def test_flat_day_report_shape_is_accepted():
    """Stage-1 also duplicates the keys at the top level -- both shapes must be read."""
    vol = resolve_stage1_vol_inputs(
        {"realized_vol_21d": 0.30, "vol_scaling_factor": 0.40, "vol_degraded": False}
    )
    assert vol["available"] is True
    assert vol["vol_scaling_factor"] == pytest.approx(0.40)
    assert vol["realized_vol_21d"] == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# 4. switch OFF -> no audit block, series untouched
# ---------------------------------------------------------------------------


def test_disabled_overlay_is_a_full_no_op():
    targets = _targets({"600000.SH": 0.80, CASH: 0.0})

    capped, summary = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key="chan_four_state_blend",
        params=_params(enabled=False),
        health_report=_health_report(),
        cash_proxy=CASH,
    )
    assert summary is None                      # ticket simply carries no `vol_overlay` key
    assert capped is targets                    # same object, nothing rebuilt

    capped_off, summary_off = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key="chan_four_state_blend",
        params=_params(mode=PORTFOLIO_VOL_OVERLAY_MODE_OFF),
        health_report=_health_report(),
        cash_proxy=CASH,
    )
    assert summary_off is None
    assert capped_off is targets


def test_mode_resolution_defaults_to_cap_and_is_prefixed_only():
    assert resolve_portfolio_vol_overlay_params("chan_four_state_blend", {}) == (True, "cap")
    assert resolve_portfolio_vol_overlay_params(
        "chan_four_state_blend", {"cfsb_portfolio_vol_overlay_enabled": "false"}
    ) == (False, "cap")
    # a BARE (unprefixed) key must not switch the overlay off for the allowlisted strategy
    assert resolve_portfolio_vol_overlay_params(
        "chan_four_state_blend", {"portfolio_vol_overlay_mode": "off"}
    ) == (True, "cap")
    # unknown spelling degrades to the conservative-but-active ceiling, never to "off"
    assert resolve_portfolio_vol_overlay_params(
        "chan_four_state_blend", {"cfsb_portfolio_vol_overlay_mode": "multiply"}
    ) == (True, "cap")


# ---------------------------------------------------------------------------
# 5. other strategies are unaffected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "strategy_key",
    ["chan_risk_managed_blend", "turtle_breakout", "equal_weight", ""],
)
def test_non_allowlisted_strategy_gets_no_overlay(strategy_key):
    targets = _targets({"600000.SH": 0.80, CASH: 0.0})

    capped, summary = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=strategy_key,
        params=_params(),           # even with the switch declared ON
        health_report=_health_report(),
        cash_proxy=CASH,
    )

    assert summary is None          # -> no `vol_overlay` on the ticket
    assert capped is targets        # -> weights untouched


def test_empty_equity_book_is_reported_not_divided_by_zero():
    targets = _targets({CASH: 1.0})
    capped, summary = apply_portfolio_vol_ceiling(
        targets, cash_proxy=CASH, vol_scaling_factor=0.37
    )
    assert summary["binding"] is False
    assert summary["reason"] == "empty_equity_book"
    assert summary["model_equity_sum"] == pytest.approx(0.0)
    pd.testing.assert_series_equal(capped, targets, check_exact=True)


# ---------------------------------------------------------------------------
# 6. DELIVERY SHORTFALL: a ceiling that whole lots cannot deliver
# ---------------------------------------------------------------------------


def test_tiny_ceiling_delivers_only_a_sliver_and_is_flagged():
    """0.037 ceiling -> 0.0048 delivered is a shortfall, and the WARN says so greppably."""
    report = evaluate_vol_overlay_delivery(0.037, 0.0048, binding=True)

    assert report["delivery_shortfall"] is True
    assert report["delivery_ok"] is False
    assert report["delivery_shortfall_ratio"] == pytest.approx(0.0048 / 0.037, abs=1e-4)

    warning = format_vol_overlay_delivery_warning(0.037, 0.0048)
    assert "[WARN/VOL OVERLAY]" in warning
    assert "3.70%" in warning            # the ceiling
    assert "0.48%" in warning            # what whole lots actually deliver
    assert "缺口" in warning             # the shortfall itself


def test_archived_replay_delivery_is_flagged():
    """The measured 2026-11-12 replay: capped 0.037, delivered 0.00482777 (~13%)."""
    report = evaluate_vol_overlay_delivery(0.037, 0.00482777, binding=True)
    assert report["delivery_shortfall"] is True
    assert report["delivery_shortfall_ratio"] == pytest.approx(0.1305, abs=1e-3)
    assert report["delivery_ok"] is False


def test_usable_ceiling_delivers_most_of_itself_and_is_not_flagged():
    """0.37 ceiling -> 0.35 delivered (95%) is lot-rounding drift, not a shortfall."""
    report = evaluate_vol_overlay_delivery(0.37, 0.35, binding=True)

    assert report["delivery_shortfall"] is False
    assert report["delivery_ok"] is True
    assert report["delivery_shortfall_ratio"] == pytest.approx(0.35 / 0.37, abs=1e-4)


def test_non_binding_or_zero_ceiling_has_no_shortfall_to_measure():
    """Nothing was capped -> there is no delivery to measure against."""
    for capped, binding in ((0.37, False), (0.0, True), (None, True)):
        report = evaluate_vol_overlay_delivery(capped, 0.0, binding=binding)
        assert report["delivery_shortfall"] is False
        assert report["delivery_shortfall_ratio"] is None
        assert report["delivery_ok"] is True


def test_delivery_guardrail_is_thresholded_at_half_and_is_not_a_multiplier():
    assert VOL_OVERLAY_DELIVERY_SHORTFALL_RATIO == pytest.approx(0.5)
    # exact-binary numbers so the boundary is not decided by float noise
    assert evaluate_vol_overlay_delivery(0.5, 0.25, binding=True)["delivery_shortfall"] is False
    assert evaluate_vol_overlay_delivery(0.5, 0.2499, binding=True)["delivery_shortfall"] is True


def test_ticket_carries_delivery_ok_without_touching_weight_budget_ok():
    """Source contract: the new flag is ADDITIVE; `weight_budget_ok` keeps its own meaning."""
    path = os.path.join(_REPO_ROOT, "scripts", "run_live_strategy.py")
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()

    assert 'ticket_payload["vol_overlay_delivery_ok"]' in source
    assert (
        'ticket_payload["weight_budget_ok"] = bool(weight_budget.get("budget_ok", True))'
        in source
    )
    # report-only guardrail: nothing in the delivery path may re-scale a weight or a lot
    delivery_helper = source[
        source.index("def evaluate_vol_overlay_delivery("):
        source.index("def format_vol_overlay_delivery_warning(")
    ]
    for forbidden in ("target_weight", "lot_size", "target_shares", "capped["):
        assert forbidden not in delivery_helper


# ---------------------------------------------------------------------------
# 7. ORDERING vs. the Stage-1 defenses -- STRICTER WINS, NEVER THE PRODUCT
# ---------------------------------------------------------------------------


def _fresh_book_then_damped(scale):
    """A fully invested 4-name book, then the Stage-1 damping the runner applies first."""
    targets = _targets({f"60000{i}.SH": 0.25 for i in range(4)})
    targets[CASH] = 0.0
    risky = [s for s in targets.index if s != CASH]
    damped = targets.copy()
    damped[risky] = damped[risky] * scale
    return targets, damped


def test_tier2_damping_then_vol_cap_applies_the_stricter_scale_not_the_product():
    """0.50 damping + 0.37 cap -> 0.37 held (NOT 0.185): the cap only tightens."""
    _, damped = _fresh_book_then_damped(0.50)

    capped, summary = apply_portfolio_vol_ceiling(
        damped, cash_proxy=CASH, vol_scaling_factor=0.37
    )

    assert summary["model_equity_sum"] == pytest.approx(0.50)   # post-damping book
    assert summary["binding"] is True
    assert summary["equity_scale_factor"] == pytest.approx(0.74)  # 0.37 / 0.50
    assert _eq_sum(capped) == pytest.approx(0.37)
    assert _eq_sum(capped) != pytest.approx(0.50 * 0.37)          # double-scaling guard
    for i in range(4):
        assert capped[f"60000{i}.SH"] == pytest.approx(0.0925)    # 0.125 * 0.74


def test_tier2_damping_stricter_than_vol_cap_makes_the_cap_a_no_op():
    """0.30 damping already sits below the 0.37 ceiling -> not re-scaled to 0.111."""
    _, damped = _fresh_book_then_damped(0.30)

    capped, summary = apply_portfolio_vol_ceiling(
        damped, cash_proxy=CASH, vol_scaling_factor=0.37
    )

    assert summary["binding"] is False
    assert summary["equity_scale_factor"] is None
    assert _eq_sum(capped) == pytest.approx(0.30)
    assert _eq_sum(capped) != pytest.approx(0.30 * 0.37)
    pd.testing.assert_series_equal(capped, damped, check_exact=True)


def test_tier3_halt_book_is_attributed_to_the_breaker_not_the_vol_cap():
    """Tier-3 already zeroed the book: the audit may not credit the vol cap with it."""
    targets = _targets({"600000.SH": 0.0, "600036.SH": 0.0, CASH: 1.0})

    capped, summary = apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key="chan_four_state_blend",
        params=_params(),
        health_report=_health_report(),         # a perfectly usable 0.3703 factor
        cash_proxy=CASH,
        superseded_by=VOL_OVERLAY_SUPERSEDED_BY_CIRCUIT_BREAKER,
    )

    # flat book, but NOT because of the ceiling: `binding=False`, no scale factor, explicit
    # provenance marker, and no `vol_unavailable` confusion either.
    assert summary["superseded_by_circuit_breaker"] is True
    assert summary["reason"] == "circuit_breaker_tier3"
    assert summary["reason"] != VOL_OVERLAY_UNAVAILABLE_REASON
    assert summary["vol_scaling_factor"] == pytest.approx(ARCHIVED_FACTOR)
    assert summary["binding"] is False
    assert summary["equity_scale_factor"] is None
    assert summary["capped_equity_sum"] == pytest.approx(0.0)
    assert _eq_sum(capped) == pytest.approx(0.0)
    pd.testing.assert_series_equal(capped, targets, check_exact=True)


def test_linear_damping_to_zero_also_never_credits_the_vol_cap():
    """`linear_equity_scale=0.0` damps the book flat before the cap: empty, not "capped"."""
    targets = _targets({"600000.SH": 0.0, CASH: 1.0})

    capped, summary = apply_portfolio_vol_ceiling(
        targets, cash_proxy=CASH, vol_scaling_factor=0.37
    )

    assert summary["binding"] is False
    assert summary["equity_scale_factor"] is None
    assert summary["reason"] == "empty_equity_book"
    assert summary["superseded_by_circuit_breaker"] is False
    assert _eq_sum(capped) == pytest.approx(0.0)
    pd.testing.assert_series_equal(capped, targets, check_exact=True)


def test_main_runs_the_cap_after_the_stage1_defenses():
    """Source contract pinning the ORDER that makes "stricter wins" true by construction."""
    path = os.path.join(_REPO_ROOT, "scripts", "run_live_strategy.py")
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()

    overlay_call = source.index(
        "target_series, vol_overlay = apply_portfolio_vol_overlay_for_strategy("
    )
    tier2_scale = source.index(
        "current_targets[risky_syms_in_targets] = "
        "current_targets[risky_syms_in_targets] * rec_scale"
    )
    linear_scale = source.index(
        "current_targets[risky_syms_in_targets] = "
        "current_targets[risky_syms_in_targets] * live_scale_clamped"
    )
    tier3_zero = source.index(
        "circuit_breaker_superseded = VOL_OVERLAY_SUPERSEDED_BY_CIRCUIT_BREAKER"
    )
    assert tier2_scale < overlay_call      # cap consumes the already-damped vector
    assert linear_scale < overlay_call
    assert tier3_zero < overlay_call
    assert "superseded_by=circuit_breaker_superseded" in source[overlay_call:overlay_call + 400]


# ---------------------------------------------------------------------------
# 8. AT8 PROVENANCE -- the snapshot must be able to explain the cap
# ---------------------------------------------------------------------------


def _entries(strategy_key="chan_four_state_blend", params=None, **kwargs):
    entries = {}
    provenance = record_portfolio_vol_overlay_effective_params(
        entries, params or {}, strategy_key, **kwargs
    )
    return entries, provenance


def test_allowlisted_strategy_registers_both_overlay_keys_with_config_source():
    """The observed defect: `vol_overlay.applied` was true with NO overlay key in `params`.

    The dump DECLARES both keys, so the honest source is the config origin (a
    `strategies_config.json` entry or a dump file -- `SOURCE_DUMP` here, matching the caller's
    `_effective_params_config_origin`).
    """
    declared = _params(enabled=True, mode="cap")
    entries, provenance = _entries(params=declared, config_origin=SOURCE_DUMP)

    assert set(entries) == {
        VOL_OVERLAY_ENABLED_PARAM,
        VOL_OVERLAY_MODE_PARAM,
        VOL_CEILING_FORCE_EXITS_PARAM,
    }
    assert entries[VOL_OVERLAY_ENABLED_PARAM] == {
        "declared": True,
        "resolved": True,
        "source": SOURCE_DUMP,
    }
    assert entries[VOL_OVERLAY_MODE_PARAM] == {
        "declared": "cap",
        "resolved": "cap",
        "source": SOURCE_DUMP,
    }
    # (PLAN C) the forced-exit switch rides in the same snapshot; absent from this dump, so
    # its honest provenance is the resolver's default (enabled) -- not "the config said so".
    assert entries[VOL_CEILING_FORCE_EXITS_PARAM] == {
        "declared": None,
        "resolved": True,
        "source": SOURCE_DEFAULT,
    }
    assert provenance["allowlisted"] is True

    # ... and the two keys survive the projection into the real artifact payload.
    payload = build_effective_params(
        strategy_id="chan_four_state_blend",
        strategy_version="1.6.0",
        base_version="1.1.0",
        config_source="strategies_config.json:chan_four_state_blend",
        strategy_dump_sha256="0" * 64,
        as_of="2026-11-12",
        run_id="2026-11-12_163000",
        params=entries,
    )
    assert payload["params"][VOL_OVERLAY_ENABLED_PARAM]["resolved"] is True
    assert payload["params"][VOL_OVERLAY_MODE_PARAM]["resolved"] == "cap"
    assert payload["params"][VOL_OVERLAY_ENABLED_PARAM]["source"] == SOURCE_DUMP


def test_allowlisted_strategy_without_declared_keys_reports_default_and_enabled():
    """A dump predating the wiring resolves ENABLED (user requirement) and says `default`.

    `declared` must stay literal `None` -- "the strategy never wrote this" is different from
    "the strategy wrote false", and only the latter may be read as an opt-out.
    """
    entries, provenance = _entries(params={}, config_origin=SOURCE_DUMP)

    assert entries[VOL_OVERLAY_ENABLED_PARAM] == {
        "declared": None,
        "resolved": True,
        "source": SOURCE_DEFAULT,
    }
    assert entries[VOL_OVERLAY_MODE_PARAM] == {
        "declared": None,
        "resolved": "cap",
        "source": SOURCE_DEFAULT,
    }
    assert provenance["enabled"] is True and provenance["mode"] == "cap"


def test_non_allowlisted_strategy_still_registers_keys_as_disabled_default():
    """Non-allowlisted: keys exist, `enabled=False`, and the ignored declaration stays visible."""
    entries, provenance = _entries(
        strategy_key="chan_risk_managed_blend",
        params=_params(enabled=True, mode="cap"),
        config_origin=SOURCE_DUMP,
    )

    assert entries[VOL_OVERLAY_ENABLED_PARAM] == {
        "declared": True,          # the dump asked for it ...
        "resolved": False,         # ... and the allowlist gate is why it is off
        "source": SOURCE_DEFAULT,  # the effective value is the gate's default, not the dump's
    }
    assert entries[VOL_OVERLAY_MODE_PARAM] == {
        "declared": "cap",
        "resolved": "off",
        "source": SOURCE_DEFAULT,
    }
    # (PLAN C) the forced-exit switch is gated by the SAME allowlist
    assert entries[VOL_CEILING_FORCE_EXITS_PARAM]["resolved"] is False
    assert entries[VOL_CEILING_FORCE_EXITS_PARAM]["source"] == SOURCE_DEFAULT
    assert provenance["allowlisted"] is False


def test_cli_override_is_reported_as_override_only_for_an_allowlisted_strategy():
    entries, _ = _entries(
        params=_params(enabled=False, mode="off"),
        config_origin=SOURCE_DUMP,
        cli_override=True,
    )
    assert entries[VOL_OVERLAY_ENABLED_PARAM]["source"] == SOURCE_OVERRIDE
    assert entries[VOL_OVERLAY_MODE_PARAM]["source"] == SOURCE_OVERRIDE

    # An override flag must NOT launder a non-allowlisted strategy into "override".
    blocked, _ = _entries(
        strategy_key="turtle_breakout", params=_params(), cli_override=True
    )
    assert blocked[VOL_OVERLAY_ENABLED_PARAM]["source"] == SOURCE_DEFAULT


def test_provenance_never_disagrees_with_the_resolver_it_projects():
    """One source of truth: provenance delegates to `resolve_portfolio_vol_overlay_params`."""
    cases = [
        ("chan_four_state_blend", {}),
        ("chan_four_state_blend", {"cfsb_portfolio_vol_overlay_enabled": "false"}),
        ("chan_four_state_blend", {"cfsb_portfolio_vol_overlay_mode": "multiply"}),
        ("chan_four_state_blend", {"portfolio_vol_overlay_mode": "off"}),
        ("chan_risk_managed_blend", _params()),
    ]
    for key, params in cases:
        provenance = resolve_portfolio_vol_overlay_provenance(key, params)
        assert (provenance["enabled"], provenance["mode"]) == (
            resolve_portfolio_vol_overlay_params(key, params)
        )


def test_main_writes_the_two_overlay_keys_into_effective_params(tmp_path, monkeypatch):
    """End-to-end on the real `main()`: an offline static template (non-allowlisted)."""
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

    artifact = out_dir / "effective_params.json"
    assert artifact.is_file()
    params = json.loads(artifact.read_text(encoding="utf-8"))["params"]
    assert VOL_OVERLAY_ENABLED_PARAM in params
    assert VOL_OVERLAY_MODE_PARAM in params
    # equal_weight is off the allowlist -> recorded as an explicit, explained absence.
    assert params[VOL_OVERLAY_ENABLED_PARAM] == {
        "declared": None,
        "resolved": False,
        "source": SOURCE_DEFAULT,
    }
    assert params[VOL_OVERLAY_MODE_PARAM]["resolved"] == PORTFOLIO_VOL_OVERLAY_MODE_OFF
    assert params[VOL_OVERLAY_MODE_PARAM]["source"] == SOURCE_DEFAULT
