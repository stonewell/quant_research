"""Regression suite for the 2026-11-12 defect B: unattributable `vol_overlay` audit block.

FORENSIC CONTEXT (archived `2026-11-12/162857/live_trading_ticket.json`)
-----------------------------------------------------------------------
The ticket carried `model_equity_sum=0.56984055`, `capped_equity_sum=0.38944536`,
`equity_scale_factor=0.683428644`, `binding=true`, `delivered_equity_sum=0.2506507`,
`delivery_shortfall=false`, `delivery_shortfall_ratio=0.6436` -- while the ticket's own
`Σ target_weight` (equity) was only `0.26098735`. i.e. **0.128458pp of the 0.38944536
ceiling (33%) had already disappeared before lot rounding**: ~12.85pp from the lifecycle
pass / 5% minimum-entry filter / entry confirmation, ~1.03pp from whole-lot rounding. The
old `delivery_shortfall_ratio` divided by the CAP (`0.2507/0.3894 = 0.64`), cleared its 0.5
threshold, and stayed silent. `target_weight_model`'s comment also claimed to be "the
model's ORIGINAL target ... frozen before ... the delivered cap" while it was actually
computed AFTER the cap and AFTER `step_position_lifecycle`.

WHAT IS PINNED HERE
-------------------
1. the archived numbers reproduce `lifecycle_filtered_equity_sum ≈ 0.1284`,
   `lifecycle_shortfall_ratio ≈ 0.67` (flags the WARN) and `delivery_shortfall_ratio ≈ 0.96`
   (does NOT flag) -- the two legs now name different failures;
2. a lot-rounding-only scenario flags ONLY the delivery leg;
3. a completely filtered book flags the lifecycle leg and does not fabricate a delivery ratio;
4. the WARN text is greppable and states "上限 X% 但有 Ypp 被生命周期/最低建仓过滤掉";
5. `apply_portfolio_vol_ceiling` exposes `pre_cap_equity_sum` as an alias of the
   post-gate/pre-cap sum (and keeps `model_equity_sum` for archived readers);
6. source contract: the ticket captures the pre-gate equity sum BEFORE the intraday
   distribution gate, feeds `post_lifecycle` into the delivery evaluation, and archives the
   per-reason-code attribution.

100% offline: pure arithmetic + source/static checks; no market data, no network.
"""

import os
import sys

import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.run_live_strategy import (  # noqa: E402
    VOL_OVERLAY_DELIVERY_SHORTFALL_RATIO,
    VOL_OVERLAY_LIFECYCLE_SHORTFALL_RATIO,
    apply_portfolio_vol_ceiling,
    evaluate_vol_overlay_delivery,
    format_vol_overlay_delivery_warning,
    format_vol_overlay_lifecycle_warning,
)

CASH = "BIL"

#: The archived `2026-11-12/162857` numbers, reproduced as literals.
ARCHIVED_CAPPED = 0.38944536
ARCHIVED_POST_LIFECYCLE = 0.26098735
ARCHIVED_DELIVERED = 0.2506507


# ---------------------------------------------------------------------------
# 1. the archived round: lifecycle leg fires, delivery leg does not
# ---------------------------------------------------------------------------


def test_archived_round_flags_lifecycle_attrition_not_lot_rounding():
    report = evaluate_vol_overlay_delivery(
        ARCHIVED_CAPPED,
        ARCHIVED_DELIVERED,
        post_lifecycle_equity_sum=ARCHIVED_POST_LIFECYCLE,
        binding=True,
    )

    lifecycle_filtered = ARCHIVED_CAPPED - ARCHIVED_POST_LIFECYCLE
    assert lifecycle_filtered == pytest.approx(0.12845801, abs=1e-8)
    assert report["lifecycle_shortfall_ratio"] == pytest.approx(0.6701, abs=1e-3)
    assert report["lifecycle_shortfall"] is True
    assert report["lifecycle_ok"] is False

    # The TRUE lot-rounding loss is 0.01033665 of 0.26098735 ≈ 3.96% -> ratio 0.96 -> OK.
    assert report["delivery_shortfall_ratio"] == pytest.approx(0.9604, abs=1e-3)
    assert report["delivery_shortfall"] is False
    assert report["delivery_ok"] is True


def test_old_capped_denominator_would_have_hidden_the_lifecycle_loss():
    """The pre-fix arithmetic (delivered / capped = 0.6436) cleared its 0.5 threshold."""
    old_style = evaluate_vol_overlay_delivery(ARCHIVED_CAPPED, ARCHIVED_DELIVERED, binding=True)
    assert old_style["delivery_shortfall_ratio"] == pytest.approx(0.6436, abs=1e-3)
    assert old_style["delivery_shortfall"] is False
    # ... while the fixed, post-lifecycle denominator reports the real lot loss as healthy
    # and the separate lifecycle leg reports the 33% attrition.
    fixed = evaluate_vol_overlay_delivery(
        ARCHIVED_CAPPED,
        ARCHIVED_DELIVERED,
        post_lifecycle_equity_sum=ARCHIVED_POST_LIFECYCLE,
        binding=True,
    )
    assert fixed["delivery_shortfall"] is False
    assert fixed["lifecycle_shortfall"] is True


# ---------------------------------------------------------------------------
# 2. a genuinely lot-starved book flags only the delivery leg
# ---------------------------------------------------------------------------


def test_lot_rounding_only_scenario_flags_only_the_delivery_leg():
    report = evaluate_vol_overlay_delivery(
        0.2610,
        0.0500,
        post_lifecycle_equity_sum=0.2610,
        binding=True,
    )
    assert report["lifecycle_shortfall_ratio"] == pytest.approx(1.0)
    assert report["lifecycle_shortfall"] is False
    assert report["delivery_shortfall_ratio"] == pytest.approx(0.1916, abs=1e-3)
    assert report["delivery_shortfall"] is True
    assert report["delivery_ok"] is False


def test_fully_filtered_book_flags_lifecycle_and_measures_no_delivery():
    report = evaluate_vol_overlay_delivery(
        0.30,
        0.0,
        post_lifecycle_equity_sum=0.0,
        binding=True,
    )
    assert report["lifecycle_shortfall"] is True
    assert report["lifecycle_shortfall_ratio"] == pytest.approx(0.0)
    # no denominator survives -> no fabricated delivery ratio / no duplicate delivery WARN
    assert report["delivery_shortfall_ratio"] is None
    assert report["delivery_shortfall"] is False


def test_non_binding_book_measures_nothing_on_either_leg():
    report = evaluate_vol_overlay_delivery(
        ARCHIVED_CAPPED,
        ARCHIVED_DELIVERED,
        post_lifecycle_equity_sum=0.10,
        binding=False,
    )
    assert report["lifecycle_shortfall"] is False
    assert report["lifecycle_shortfall_ratio"] is None
    assert report["delivery_shortfall"] is False
    assert report["delivery_shortfall_ratio"] is None


# ---------------------------------------------------------------------------
# 3. thresholds are separate and configurable
# ---------------------------------------------------------------------------


def test_two_thresholds_are_separable_and_default_to_half_and_eighty_percent():
    assert VOL_OVERLAY_DELIVERY_SHORTFALL_RATIO == pytest.approx(0.5)
    assert VOL_OVERLAY_LIFECYCLE_SHORTFALL_RATIO == pytest.approx(0.8)

    # lifecycle ratio 0.79 flags at the default 0.8 but not at a relaxed 0.7
    strict = evaluate_vol_overlay_delivery(
        1.0, 0.79, post_lifecycle_equity_sum=0.79, binding=True
    )
    relaxed = evaluate_vol_overlay_delivery(
        1.0,
        0.79,
        post_lifecycle_equity_sum=0.79,
        binding=True,
        lifecycle_shortfall_threshold=0.70,
    )
    assert strict["lifecycle_shortfall"] is True
    assert relaxed["lifecycle_shortfall"] is False
    # the delivery leg is untouched by the lifecycle threshold: 0.79/0.79 = 1.0
    assert strict["delivery_shortfall"] is False
    assert relaxed["delivery_shortfall"] is False


# ---------------------------------------------------------------------------
# 4. the WARN text names the lifecycle loss (and is greppable)
# ---------------------------------------------------------------------------


def test_lifecycle_warning_states_the_pp_gap_and_the_reason_codes():
    warning = format_vol_overlay_lifecycle_warning(
        ARCHIVED_CAPPED,
        ARCHIVED_POST_LIFECYCLE,
        {
            "min_entry_threshold": {"symbols": 3, "equity_sum": 0.0850},
            "exit_confirming": {"symbols": 1, "equity_sum": 0.0285},
            "no_position": {"symbols": 2, "equity_sum": 0.0150},
        },
    )
    assert "[WARN/VOL OVERLAY]" in warning
    assert "被生命周期/最低建仓过滤掉" in warning
    assert "38.94%" in warning                  # the ceiling
    assert "12.85pp" in warning                 # the lifecycle gap
    assert "26.10%" in warning                  # what survived
    assert "min_entry_threshold: 3 只 / 8.50pp" in warning
    assert "exit_confirming: 1 只 / 2.85pp" in warning
    assert "no_position: 2 只 / 1.50pp" in warning


def test_delivery_warning_names_both_brackets_when_the_lifecycle_sum_is_supplied():
    warning = format_vol_overlay_delivery_warning(
        ARCHIVED_CAPPED, ARCHIVED_DELIVERED, ARCHIVED_POST_LIFECYCLE
    )
    assert "38.94%" in warning        # the ceiling
    assert "26.10%" in warning        # post-lifecycle deliverable book
    assert "25.07%" in warning        # delivered
    assert "整手缺口 1.03pp" in warning


# ---------------------------------------------------------------------------
# 5. `pre_cap_equity_sum` alias on the ceiling helper
# ---------------------------------------------------------------------------


def test_ceiling_helper_records_the_pre_cap_sum_under_an_unambiguous_name():
    targets = pd.Series({"600000.SH": 0.25, "600036.SH": 0.25, CASH: 0.0}, dtype="float64")
    _, summary = apply_portfolio_vol_ceiling(targets, cash_proxy=CASH, vol_scaling_factor=0.37)
    assert summary["model_equity_sum"] == pytest.approx(0.50)
    assert summary["pre_cap_equity_sum"] == pytest.approx(0.50)
    assert summary["capped_equity_sum"] == pytest.approx(0.37)


# ---------------------------------------------------------------------------
# 6. source contract: capture order + wiring of the new fields
# ---------------------------------------------------------------------------


def _source():
    path = os.path.join(_REPO_ROOT, "scripts", "run_live_strategy.py")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_pre_gate_equity_sum_is_captured_before_the_distribution_gate():
    source = _source()
    capture = source.index("_pre_distribution_gate_equity_sum = float(")
    gate_call = source.index("target_series = apply_intraday_distribution_gate(")
    assert capture < gate_call
    # the gate's own scaling logic is not touched: the call keeps its original signature
    assert "health_report=health_report,\n        account_state=account_state,\n    )" in source[
        gate_call:gate_call + 400
    ]


def test_ticket_feeds_post_lifecycle_into_the_delivery_evaluation_and_archives_attribution():
    source = _source()
    assert "post_lifecycle_equity_sum=vol_overlay.get(\"post_lifecycle_equity_sum\")" in source
    for key in (
        'vol_overlay["pre_gate_equity_sum"]',
        'vol_overlay["post_lifecycle_equity_sum"]',
        'vol_overlay["lifecycle_filtered_equity_sum"]',
        'vol_overlay["lifecycle_filtered_reasons"]',
        'vol_overlay["lifecycle_shortfall"]',
        'vol_overlay["lifecycle_shortfall_ratio"]',
        'ticket_payload["vol_overlay_lifecycle_ok"]',
    ):
        assert key in source, key
    # the min-entry / no-position reason codes are aggregated, not silently dropped
    assert "_action == \"NO_POSITION\"" in source
    assert "BlockingReason.MIN_ENTRY_THRESHOLD" in source
