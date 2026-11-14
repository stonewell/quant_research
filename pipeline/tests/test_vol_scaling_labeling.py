"""`LV18` LABEL HONESTY + THE VOL-CAP LAYER'S MISSING ACCEPTANCE TESTS.

THE DEFECT UNDER TEST (label)
-----------------------------
`LV18` was filed because `scripts/check_live_portfolio_health.py` COMPUTED
`vol_scaling_factor = clip(target_vol / realized_vol_21d)` (Barroso & Santa-Clara 2015) and
PRESENTED it as an applied risk action while nothing in Stage 2 consumed it -- "presented as
an applied action, actually inert". The wiring (`scripts/run_live_strategy.py::
apply_portfolio_vol_overlay_for_strategy`, `mode="cap"`) fixed the GLOBAL case, but it is
STRATEGY GATED: the factor is consumed as a `min()` ceiling on total equity ONLY for the
vol-ceiling ALLOWLIST (`PORTFOLIO_VOL_OVERLAY_STRATEGY_KEYS`, currently
`chan_four_state_blend`). For the other 50+ strategies it is INERT, yet Stage 1 still said,
unconditionally:

  * ``Barroso scaling scales equity by 0.42x.``                        (gate actions)
  * ``Volatility Scaling    :   0.42x``                                 (terminal dashboard)
  * ``- **Barroso Volatility Scaling**: `0.42x```                       (Markdown report)
  * ``Elevated volatility: Rescaling portfolio equity exposure downwards.``  (assessment)

-- i.e. the SAME LIE as LV18, one level down: per strategy instead of globally. Every one of
those strings now interpolates ONE scope qualifier (`VOL_CAP_SCOPE_QUALIFIER`) that is true in
BOTH cases: it never claims application for an off-allowlist strategy, and it never implies the
factor multiplies or raises exposure for an allowlisted one (it is a ceiling, not a product).

WHAT THIS FILE PINS (beyond the wording)
----------------------------------------
The `LV18` acceptance criteria required order-level tests that were never written. They live
here, on the REAL Stage-2 entry point and a REAL Stage-1 report fixture:

1. cap does NOT bind   -> the delivered weights are BIT-IDENTICAL to an uncapped run;
2. cap DOES bind       -> the book shrinks and NO symbol's target rises (downwards only);
3. factor in {0.0, -0.2, None, NaN} -> treated as UNAVAILABLE
   (`reason="vol_unavailable"`, `applied=False`), target series untouched -- a missing
   measurement must never default to a full-risk 1.0x;
4. no UNQUALIFIED claim can come back: a STATIC assertion on the Stage-1 source text
   (the repo's other static checks do the same) plus rendered-output assertions;
5. the ALLOWLIST BOUNDARY: the same factor shrinks an allowlisted strategy's book and leaves
   a non-allowlisted one bit-identical, and the report says so correspondingly (Stage 2 emits
   the applied-cap audit block / wording on the allowlisted run and NO `vol_overlay` block --
   hence no application claim -- on the other).

100% offline: a JSON Stage-1 report fixture in `tmp_path`, pandas Series, and the synthetic
provider is never contacted (no `main()` run, no network, no `data/runs/**`).
"""

import json
import os
import sys

import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import scripts.check_live_portfolio_health as health_mod  # noqa: E402
from scripts.run_live_strategy import (  # noqa: E402
    PORTFOLIO_VOL_OVERLAY_MODE_CAP,
    PORTFOLIO_VOL_OVERLAY_MODE_OFF,
    PORTFOLIO_VOL_OVERLAY_STRATEGY_KEYS,
    VOL_OVERLAY_UNAVAILABLE_REASON,
    apply_portfolio_vol_overlay_for_strategy,
    resolve_portfolio_vol_overlay_params,
    resolve_stage1_vol_inputs,
)

CASH = "BIL"

#: Archived Stage-1 numbers for `2026-11-11/205137` (the round LV18 was filed against).
ARCHIVED_VOL = 0.3240854096084951
ARCHIVED_FACTOR = 0.3702727628033721

#: The one strategy the overlay is wired for (read from Stage 2, never re-spelled here).
ALLOWLISTED = PORTFOLIO_VOL_OVERLAY_STRATEGY_KEYS[0]

#: A strategy that is DELIBERATELY off the allowlist. Asserted off below, so adding it to
#: the allowlist turns this file red instead of silently weakening test 5.
NOT_ALLOWLISTED = "chan_risk_managed_blend"


# --------------------------------------------------------------------------- #
# Helpers: a real Stage-1 report fixture + the real overlay entry point
# --------------------------------------------------------------------------- #
def _write_stage1_report(tmp_path, *, factor, realized=ARCHIVED_VOL, degraded=False):
    """Writes the SHAPE Stage 2 reads back (`stage1_health_report.json`), 100% offline.

    Keys mirror `scripts/check_live_portfolio_health.py::evaluate_realized_volatility`'s
    return value (nested `volatility_targeting` + the top-level mirror Stage 2 also accepts).
    """
    block = {
        "realized_vol_21d": realized,
        "target_vol": 0.12,
        "vol_scaling_factor": factor,
        "vol_elevated": bool(realized is not None and realized > 0.12),
        "vol_degraded": degraded,
        "vol_scaling_fallback": "insufficient_history" if degraded else None,
    }
    path = os.path.join(str(tmp_path), "stage1_health_report.json")
    with open(path, "w", encoding="utf-8") as f:
        # `json` round-trips NaN/Infinity by default; a Stage-1 report carrying `NaN` is
        # exactly the "no usable measurement" shape this file pins.
        json.dump({"volatility_targeting": block, **block}, f, indent=2)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _params(enabled=True, mode="cap"):
    return {
        "cfsb_portfolio_vol_overlay_enabled": enabled,
        "cfsb_portfolio_vol_overlay_mode": mode,
    }


def _targets(weights):
    return pd.Series(dict(weights), dtype="float64")


def _eq_sum(series):
    return float(sum(v for s, v in series.items() if str(s) != CASH))


def _book(weights, cash_rest=0.0):
    """Equity book + an explicit cash-proxy row (the shape Stage 2 hands the overlay)."""
    series = _targets(weights)
    series[CASH] = cash_rest
    return series


def _overlay(targets, *, strategy_key=ALLOWLISTED, params=None, report=None):
    return apply_portfolio_vol_overlay_for_strategy(
        targets,
        strategy_key=strategy_key,
        params=_params() if params is None else params,
        health_report=report,
        cash_proxy=CASH,
    )


# --------------------------------------------------------------------------- #
# 1. Cap does NOT bind => orders bit-identical to a run without the cap
# --------------------------------------------------------------------------- #
def test_non_binding_cap_leaves_orders_bit_identical(tmp_path):
    """A consulted-but-not-binding cap must not perturb ONE bit of the order vector.

    PREVENTS: the regression where an "audit-only" ceiling still mutates the book -- e.g. a
    re-normalisation or a rounding pass riding along with the overlay -- so a 30% book gets
    shipped as something other than 30% on a day the ceiling (37%) was never hit. Compared
    against the SAME book with the overlay switched OFF, which is exactly what a pre-LV18
    ticket was: `capped == uncapped` byte-for-byte, no `vol_overlay` block needed to explain
    a difference that must not exist.

    LEVEL: the equity TARGET VECTOR, which is where Stage 2 sizes orders (`apply_portfolio_
    vol_overlay_for_strategy`'s documented "returns the caller's series UNCHANGED ... keeps
    the other strategies byte-identical"). A vector-identical book with no audit block can
    only produce identical rows; a row-level harness would re-test lot rounding, not the cap.
    """
    targets = _book({f"00000{i}.SZ": 0.10 for i in range(3)}, cash_rest=0.70)
    report = _write_stage1_report(tmp_path, factor=ARCHIVED_FACTOR)

    capped, summary = _overlay(targets, report=report)
    uncapped, off_summary = _overlay(targets, params=_params(enabled=False), report=report)

    assert summary is not None and summary["binding"] is False
    assert summary["applied"] is True
    # Nothing that could re-size a leg is recorded when nothing was capped.
    assert summary["equity_scale_factor"] is None
    assert summary["capped_equity_sum"] == pytest.approx(summary["model_equity_sum"])
    assert summary["reason"] is None

    # Bit-identical: exact equality, not `approx`, and identical ordering/index.
    pd.testing.assert_series_equal(capped, targets, check_exact=True)
    pd.testing.assert_series_equal(capped, uncapped, check_exact=True)
    # The switched-off run cannot even claim it looked (`vol_overlay` absent from the ticket).
    assert off_summary is None


# --------------------------------------------------------------------------- #
# 2. Cap DOES bind => orders shrink, and only downwards
# --------------------------------------------------------------------------- #
def test_binding_cap_shrinks_orders_and_never_increases_a_target(tmp_path):
    """When the cap binds, every target weight may only go DOWN.

    PREVENTS: two opposite lies at once. (a) A ceiling implemented as a MULTIPLICATION
    (0.44 x 0.37 = 0.163) -- the double-de-risking hazard `blend_base.py`'s own Garman-Klass
    target-vol scaling creates. (b) Any "rebalance" that lifts a leg while claiming to
    de-risk. Both would pass a naive "the book got smaller" assertion, so the per-symbol
    direction (<=, with at least one strict <) is what is pinned here.
    """
    targets = _book({f"60000{i}.SH": 0.11 for i in range(4)}, cash_rest=0.56)
    report = _write_stage1_report(tmp_path, factor=ARCHIVED_FACTOR)

    capped, summary = _overlay(targets, report=report)

    assert summary["binding"] is True
    assert summary["capped_equity_sum"] == pytest.approx(ARCHIVED_FACTOR)
    assert _eq_sum(capped) == pytest.approx(ARCHIVED_FACTOR)
    # The archived number pins cap-not-product: 0.44 * 0.3702 = 0.1629, nowhere near 0.3703.
    assert _eq_sum(capped) != pytest.approx(_eq_sum(targets) * ARCHIVED_FACTOR)

    decreases = 0
    for symbol in capped.index:
        assert capped[symbol] <= float(targets[symbol]) + 1e-12, (
            f"{symbol} was INCREASED by a cap that may only reduce exposure: "
            f"{targets[symbol]} -> {capped[symbol]}"
        )
        if capped[symbol] < float(targets[symbol]) - 1e-12:
            decreases += 1
    assert decreases >= 1, "a binding cap that shrinks nothing is not a cap"
    # The input vector is never mutated in place.
    assert _eq_sum(targets) == pytest.approx(0.44)


# --------------------------------------------------------------------------- #
# 3. 0.0 / negative / None / NaN => UNAVAILABLE, nothing rescaled
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("unusable_factor", [0.0, -0.2, None, float("nan")])
def test_unusable_factor_is_unavailable_not_a_full_risk_passthrough(
    tmp_path, unusable_factor
):
    """`0.0`, a negative factor, `None` and `NaN` are all "no usable measurement".

    PREVENTS: the double-sided trap these values create. Read naively, `0.0` means "hold no
    equity" (a liquidation invented from a broken input) while `NaN`/`None` mean "scale by
    nothing" (a full-risk pass-through). Both are lies about what was MEASURED, so all four
    must land on the SAME explicit verdict -- `reason="vol_unavailable"`, `applied=False` --
    and leave the target series untouched.
    """
    targets = _book({"600000.SH": 0.80}, cash_rest=0.20)
    report = _write_stage1_report(tmp_path, factor=unusable_factor)

    resolved = resolve_stage1_vol_inputs(report)
    assert resolved["available"] is False
    assert resolved["reason"] == VOL_OVERLAY_UNAVAILABLE_REASON
    assert VOL_OVERLAY_UNAVAILABLE_REASON == "vol_unavailable"
    assert resolved["vol_scaling_factor"] is None, (
        "an unusable factor must resolve to None, not to the raw 0.0/-0.2/NaN, so no "
        "consumer can compute a size from it"
    )

    capped, summary = _overlay(targets, report=report)

    assert summary is not None
    assert summary["applied"] is False
    assert summary["reason"] == VOL_OVERLAY_UNAVAILABLE_REASON
    assert summary["binding"] is False
    assert summary["vol_scaling_factor"] is None
    # Untouched: an unavailable factor never liquidates (0.0) and never passes through
    # silently as an applied 1.0x (NaN/None).
    pd.testing.assert_series_equal(capped, targets, check_exact=True)


# --------------------------------------------------------------------------- #
# 4. No UNQUALIFIED claim can come back (static + rendered)
# --------------------------------------------------------------------------- #
LEGACY_UNQUALIFIED_CLAIMS = (
    # the pre-fix strings, verbatim
    "scales equity by",
    "Rescaling portfolio equity exposure downwards",
    'v_status = "⚠️ Scaled Down"',
)


def _source_lines():
    path = os.path.join(_REPO_ROOT, "scripts", "check_live_portfolio_health.py")
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    return source, source.splitlines()


def test_vol_section_source_never_drops_the_scope_qualifier():
    """STATIC: every user-visible presentation of the factor must carry its scope.

    PREVENTS: the silent re-introduction of the LV18 lie by a later edit that "tidies up" a
    long line, or that adds a FIFTH render site (a new report field, a new print) copying the
    pre-fix wording. The check is on the SOURCE TEXT, in the style of the repo's other static
    audit tests, because rendered-output tests only cover the sites that exist today.

    The qualifier must stay TRUE IN BOTH CASES, so the words that make it neutral are pinned
    too: no bare "applied", and an explicit statement that off-allowlist strategies are
    unaffected.
    """
    source, lines = _source_lines()

    # Comment lines are excluded: the disclosure block above the qualifier deliberately
    # QUOTES the pre-fix wording (naming the bug is load-bearing history in this repo), and
    # only an executable/formatting line can put a claim in front of an operator again.
    code = "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))
    for claim in LEGACY_UNQUALIFIED_CLAIMS:
        assert claim not in code, (
            f"unqualified Stage-1 volatility claim is back in the source: {claim!r} -- the "
            "factor is a cap consumed ONLY by the vol-ceiling allowlist"
        )

    qualifier = health_mod.VOL_CAP_SCOPE_QUALIFIER
    for required in ("cap", "allowlist", ALLOWLISTED, "no effect on other strategies"):
        assert required in qualifier, (
            f"the scope qualifier must state {required!r} to stay truthful for BOTH an "
            f"allowlisted and an off-allowlist strategy; got {qualifier!r}"
        )

    # Every line that turns the factor into text must reference the qualifier (the gate
    # action spans lines, hence the small window).
    factor_sites = [
        i
        for i, line in enumerate(lines)
        if "vol_scaling_factor" in line
        and ("Barroso" in line or "Volatility Scaling" in line)
        and not line.strip().startswith("#")
    ]
    assert len(factor_sites) >= 3, (
        "expected the three vol-factor render sites (actions / dashboard / markdown); "
        f"found {len(factor_sites)}: {[lines[i].strip() for i in factor_sites]}"
    )
    for i in factor_sites:
        window = "\n".join(lines[max(0, i - 3) : i + 4])
        assert "VOL_CAP_SCOPE_QUALIFIER" in window, (
            "a Stage-1 vol-factor presentation lost its scope qualifier:\n"
            f"{lines[i].strip()}"
        )

    # The Markdown assessment line is delegated to a scope-carrying variable rather than
    # inlined -- assert the wiring, so the delegation cannot be flattened back to a claim.
    assert "_vol_cap_assessment" in source
    assert "VOL_CAP_SCOPE_QUALIFIER" in source.split("_vol_cap_assessment = (")[1].split("md_content")[0]


def _dd_info():
    return {
        "current_nav": 100000.0,
        "peak_nav": 100000.0,
        "drawdown_pct": -0.05,
        "tier": "TIER_1_DAMPING",
        "tier_label": "🟡 TIER 1",
        "linear_equity_scale": 0.85,
        "tier1_counter": 0,
        "tier1_max": 15,
        "tier3_counter": 0,
        "tier3_max": 3,
        "freeze_remaining_bars": 0,
        "auto_healed": False,
        "heal_reason": "",
        "fast_recovery_nav": False,
        "nav_10d_status": "ok",
        "nav_10d_return": 0.01,
        "nav_10d_history_len": 10,
        "nav_source": "mtm",
        "nav_form": 100000.0,
        "nav_mtm": 100000.0,
        "nav_deviation_pct": 0.0,
        "nav_deviation_exceeded": False,
        "nav_breakdown_status": "OK",
    }


def _macro_info():
    return {
        "breadth": 0.5,
        "breadth_thresh": 0.3,
        "thrust": 0.5,
        "thrust_thresh": 0.6,
        "target_exposure": 0.5,
        "tracked_symbols_count": 2,
        "leaders": [],
        "breadth_active": True,
        "thrust_active": False,
    }


def _elevated_vol():
    """The archived elevated-volatility shape (factor 0.3703 << 1.0)."""
    return {
        "realized_vol_21d": ARCHIVED_VOL,
        "target_vol": 0.12,
        "vol_scaling_factor": ARCHIVED_FACTOR,
        "vol_elevated": True,
        "vol_degraded": False,
        "vol_model": "garman_klass",
        "vol_window_start": "2026-10-13",
        "vol_window_end": "2026-11-11",
        "vol_n_obs": 21,
        "vol_input_bars_last_date": "2026-11-11",
        "vol_input_lag_days": 0,
        "vol_input_source": "downside_semivariance_5m",
    }


def _dashboard_kwargs(strategy_info):
    return dict(
        as_of_date="2026-11-11",
        universe_path="/tmp/univ.txt",
        strategy_path="/tmp/strat.json",
        strategy_info=strategy_info,
        dd_info=_dd_info(),
        macro_info=_macro_info(),
        vol_info=_elevated_vol(),
        gate_code="CAUTION_DAMPING",
        gate_headline="🟡 CAUTION: TIER 1 SMOOTH LINEAR DAMPING ACTIVE",
        actions=["sweep into cash"],
        timing=None,
    )


def test_rendered_vol_lines_carry_the_scope_and_no_applied_claim(capsys):
    """RENDERED: the dashboard/markdown/actions put the scope ON the number.

    PREVENTS: a qualifier that exists only as a constant (referenced somewhere harmless)
    while the strings an operator actually reads still present `0.37x` as an applied rescale.
    """
    qualifier = health_mod.VOL_CAP_SCOPE_QUALIFIER

    health_mod.print_dashboard(**_dashboard_kwargs({"strategy_name": "x", "template_name": "chan_four_state_blend"}))
    out = capsys.readouterr().out
    factor_lines = [ln for ln in out.splitlines() if "Volatility Scaling" in ln]
    assert factor_lines, out
    for line in factor_lines:
        assert qualifier in line, f"dashboard factor line without scope: {line!r}"
        assert "Scaled Down" not in line

    # Gate actions (also archived into `trader_actions` in the JSON report).
    _code, _head, actions = health_mod.resolve_gate_directive(
        _dd_info(), _macro_info(), _elevated_vol()
    )
    vol_actions = [a for a in actions if "Barroso" in a]
    assert vol_actions, actions
    for action in vol_actions:
        assert qualifier in action
        assert "CEILING" in action          # cap semantics, never a multiplier
        assert "scales equity by" not in action


def test_markdown_report_vol_section_carries_the_scope(tmp_path):
    """RENDERED: the Markdown report (the archived artefact humans audit) is qualified too."""
    _json_path, md_path = health_mod.generate_reports(
        output_dir=str(tmp_path),
        as_of_date="2026-11-11",
        strategy_info={"strategy_name": "x", "template_name": "chan_four_state_blend"},
        dd_info=_dd_info(),
        macro_info=_macro_info(),
        vol_info=_elevated_vol(),
        gate_code="CAUTION_DAMPING",
        gate_headline="🟡 CAUTION",
        actions=["sweep into cash"],
    )
    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()

    qualifier = health_mod.VOL_CAP_SCOPE_QUALIFIER
    factor_bullets = [ln for ln in md.splitlines() if "Barroso Volatility Scaling" in ln]
    assert factor_bullets, md
    assert all(qualifier in ln for ln in factor_bullets)
    assessments = [ln for ln in md.splitlines() if ln.startswith("- **Assessment**")]
    assert assessments, md
    assert all(qualifier in ln for ln in assessments)
    assert "Rescaling portfolio equity exposure downwards" not in md


# --------------------------------------------------------------------------- #
# 5. Allowlist boundary: shrink vs bit-identical, and the report says so
# --------------------------------------------------------------------------- #
def test_allowlist_boundary_shrink_vs_bit_identical_and_matching_wording(tmp_path):
    """The SAME factor must de-risk only what the allowlist wires it into.

    PREVENTS: (a) a label fix that over-corrects into "inert everywhere" -- the allowlist
    member must still be capped; (b) a cap silently reaching the other 50+ strategies --
    the off-allowlist book must come back bit-identical with NO `vol_overlay` audit block,
    so its report cannot claim application; (c) the report's applied-cap wording appearing
    on a run where nothing was applied.

    The Stage-1 side of the wording is deliberately STRATEGY-NEUTRAL (no membership is
    available at those render sites), so it must read the same for both strategies while
    remaining truthful; the discriminating side is Stage 2's audit block, which is present,
    `mode="cap"`, `binding=True` only for the allowlisted run.
    """
    assert ALLOWLISTED != NOT_ALLOWLISTED
    assert NOT_ALLOWLISTED not in PORTFOLIO_VOL_OVERLAY_STRATEGY_KEYS, (
        f"{NOT_ALLOWLISTED!r} joined the vol-overlay allowlist -- this test's negative arm "
        "is no longer negative; pick another off-allowlist strategy instead of deleting it"
    )
    report = _write_stage1_report(tmp_path, factor=ARCHIVED_FACTOR)

    targets = _book({f"60000{i}.SH": 0.11 for i in range(4)}, cash_rest=0.56)

    # (a) allowlisted: capped, with the audit block Stage 2's "[RISK ALERT / VOL OVERLAY]
    #     上限生效" wording is gated on.
    capped_on, summary_on = _overlay(targets, strategy_key=ALLOWLISTED, report=report)
    assert summary_on is not None
    assert summary_on["applied"] is True and summary_on["binding"] is True
    assert summary_on["mode"] == PORTFOLIO_VOL_OVERLAY_MODE_CAP
    assert _eq_sum(capped_on) == pytest.approx(ARCHIVED_FACTOR)
    assert _eq_sum(capped_on) < _eq_sum(targets)

    # (b) off-allowlist: params that ASK for the overlay are outranked by the allowlist.
    assert resolve_portfolio_vol_overlay_params(NOT_ALLOWLISTED, _params()) == (
        False,
        PORTFOLIO_VOL_OVERLAY_MODE_OFF,
    )
    capped_off, summary_off = _overlay(targets, strategy_key=NOT_ALLOWLISTED, report=report)
    assert summary_off is None, "an off-allowlist strategy must carry NO vol_overlay block"
    pd.testing.assert_series_equal(capped_off, targets, check_exact=True)

    # (c) the two effects differ, which is the whole point of the allowlist
    assert not capped_on.equals(targets)
    assert capped_off.equals(targets)


def test_stage1_wording_is_strategy_neutral_but_never_unqualified(capsys):
    """The Stage-1 render sites know NO allowlist membership, so they must not guess one.

    PREVENTS: the tempting "fix" of branching the wording on `strategy_info`'s
    `template_name`/`strategy_name` -- exactly the invented lookup that rots the day the
    Stage-2 allowlist changes, and which would then either claim application for an inert
    strategy or deny it for the one strategy where the cap really runs. The one honest
    shape is a scope-qualified sentence that is identical for both and true for both.
    """
    qualifier = health_mod.VOL_CAP_SCOPE_QUALIFIER
    rendered = {}
    for name in (ALLOWLISTED, NOT_ALLOWLISTED):
        health_mod.print_dashboard(
            **_dashboard_kwargs({"strategy_name": name, "template_name": name})
        )
        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if "Volatility Scaling" in ln]
        assert lines, out
        rendered[name] = lines[0]
        assert qualifier in rendered[name]

    # Byte-identical factor line: no name-guessed membership branch can hide in here.
    assert rendered[ALLOWLISTED] == rendered[NOT_ALLOWLISTED]

    _c, _h, actions = health_mod.resolve_gate_directive(
        _dd_info(), _macro_info(), _elevated_vol()
    )
    vol_actions = [a for a in actions if "Barroso" in a]
    assert vol_actions and all(qualifier in a for a in vol_actions)
