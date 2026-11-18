"""Regression tests for the summary card's cash precedence (``闲置可用现金``).

THE BUG (2026-11-18 round)
--------------------------
The card derived cash as ``NAV箱 − Σ持仓市值`` unconditionally.  Both legs are refreshed at
different moments: the holdings table is repainted from a round's PLANNED book (re-priced)
by ``app.js::refreshHoldingsAfterDeploy``, while ``#input-portfolio-value`` still held the
NAV the operator had typed BEFORE that round.  The subtraction therefore mixed two
timebases:

* engine mark of that round: ``nav_effective = 198,739.52``, ``nav_equity = 129,706.00``,
  cash ``69,033.52`` (``198,739.52 − 129,706.00``) -- and the NAV-provenance strip on the
  SAME screen already printed 69,033.52;
* what the card showed: ``197,652.52 − 129,706.00 = 67,946.52`` (the operator's stale form
  NAV), off by exactly ``1,087.00``.

Ruled out by the investigating session: cash-sweep double subtraction, fee double count,
BUY cost counted twice, SELL proceeds missing.  The 11-18 ticket has ZERO BUY/SELL rows, and
its BIL row is a ``SWEEP/RELEASE_CASH`` with 0 shares and ``trade_value -0.0``.

WHAT IS PINNED HERE
-------------------
1. The SHIPPED arithmetic (``static/js/cash_resolve.js``) is executed under ``node`` -- not
   re-implemented in Python -- and must return 69,033.52 for the reported round while the
   stale-NAV subtraction is demonstrably NOT what it produces.
2. The SHIPPED resolver (``pickRoundMark``) resolves the NAV leg and the cash leg as ONE
   lockstep snapshot (stage-2 pair -> gate pair -> carried NAV-only), so the two legs can
   never come from different marks.
3. Structural assertions (always run, no JS runtime needed) that ``app.js`` binds the card
   to the run/bundle timebase and that the derived residual is no longer unconditional.

Everything is offline and hermetic: no fixture under ``data/`` is read, no network call is
made, and the only external dependency is an optional ``node`` binary.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CASH_RESOLVE_JS = os.path.join(_WEB_DIR, "static", "js", "cash_resolve.js")
_APP_JS = os.path.join(_WEB_DIR, "static", "js", "app.js")
_INDEX_HTML = os.path.join(_WEB_DIR, "static", "index.html")

#: The reported round, verbatim.  `nav_equity + cash == nav_effective` must hold, and the
#: operator's stale form NAV must NOT be the leg the card subtracts.
NAV_EFFECTIVE = 198739.52
NAV_EQUITY = 129706.00
ROUND_CASH = 69033.52
STALE_FORM_NAV = 197652.52
BUGGY_RESIDUAL = 67946.52


def _node() -> str | None:
    return shutil.which("node") or shutil.which("nodejs")


def _run_node(script: str) -> "subprocess.CompletedProcess[str]":
    node = _node()
    if not node:
        pytest.skip("no node/nodejs on PATH: cash-resolution arithmetic cannot be executed")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"node failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc


def _js_brace_block(source: str, start: int) -> str:
    """The balanced ``{...}`` block beginning at ``start`` (inclusive)."""
    assert source[start] == "{", "expected a '{' at the start of a balanced block"
    depth = 0
    for idx in range(start, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError("unbalanced braces while extracting a JS block")


def _extract_js_function(source: str, name: str) -> str:
    marker = f"function {name}("
    idx = source.find(marker)
    assert idx >= 0, f"`{name}` is missing from the JS source"
    brace = source.find("{", idx)
    return f"function {name}(" + source[source.find("(", idx) + 1 : brace] + _js_brace_block(source, brace)


def _app_js() -> str:
    with open(_APP_JS, "r", encoding="utf-8") as f:
        return f.read()


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 1. The shipped resolver: exact numbers of the reported round
# ---------------------------------------------------------------------------
def test_shipped_resolver_returns_the_round_cash_not_the_stale_nav_residual():
    """69,033.52 -- and provably NOT the 67,946.52 the stale form NAV produced."""
    script = "\n".join(
        [
            f"const {{resolveDisplayedCash}} = require({json.dumps(_CASH_RESOLVE_JS)});",
            "const out = {};",
            "out.authoritative = resolveDisplayedCash({",
            "  authoritativeCash: " + repr(ROUND_CASH) + ",",
            "  operatorCash: null,",
            "  nav: " + repr(NAV_EFFECTIVE) + ",",
            "  totalEquity: " + repr(NAV_EQUITY) + ",",
            "});",
            # The SAME legs minus the run's cash binding == the historical (buggy) preview.
            # Kept in the test to prove the fix is the SOURCE, not a new subtraction.
            "out.staleDerived = resolveDisplayedCash({",
            "  authoritativeCash: null,",
            "  operatorCash: null,",
            "  nav: " + repr(STALE_FORM_NAV) + ",",
            "  totalEquity: " + repr(NAV_EQUITY) + ",",
            "});",
            "process.stdout.write(JSON.stringify(out));",
        ]
    )
    out = json.loads(_run_node(script).stdout)

    auth = out["authoritative"]
    assert auth["source"] == "authoritative_run"
    assert auth["cash"] == pytest.approx(ROUND_CASH, abs=1e-9), (
        f"the run's own measured cash must be shown verbatim; got {auth['cash']!r}"
    )
    assert auth["cash"] != pytest.approx(BUGGY_RESIDUAL, abs=1e-6), (
        "the card re-derived the stale-NAV residual; the authoritative branch did not win"
    )
    assert auth["cash"] == pytest.approx(NAV_EFFECTIVE - NAV_EQUITY, abs=1e-6), (
        "the displayed cash must equal the round's own nav_effective − nav_equity"
    )
    # Formatting contract preserved (`recalcHoldings` prints ratio with 1dp + '%').
    assert f"{auth['cashRatio']:.1f}%" == "34.7%"

    stale = out["staleDerived"]
    assert stale["source"] == "derived_residual"
    assert stale["cash"] == pytest.approx(BUGGY_RESIDUAL, abs=1e-6), (
        "the derived fallback must still reproduce the historical 67,946.52 for a stale NAV "
        "-- it is the AUTHORITATIVE branch that has to win, not a changed subtraction"
    )


def test_precedence_is_authoritative_then_operator_then_derived():
    """All three branches, in order, including the >= 0 clamp on the derived one."""
    script = "\n".join(
        [
            f"const {{resolveDisplayedCash}} = require({json.dumps(_CASH_RESOLVE_JS)});",
            "const out = {};",
            "out.both = resolveDisplayedCash({authoritativeCash: 69033.52, operatorCash: 1.0,"
            " nav: 198739.52, totalEquity: 129706.0});",
            "out.operator = resolveDisplayedCash({authoritativeCash: null, operatorCash: 1234.5,"
            " nav: 197652.52, totalEquity: 129706.0});",
            "out.derived = resolveDisplayedCash({authoritativeCash: null, operatorCash: null,"
            " nav: 100000.0, totalEquity: 40000.0});",
            "out.clamped = resolveDisplayedCash({authoritativeCash: null, operatorCash: null,"
            " nav: 100000.0, totalEquity: 120000.0});",
            # Blank / absent inputs must be ABSENT (null), never 0 -- `#input-cash` is blank
            # by default and sending 0 would silently claim the account holds no cash.
            "out.blankOperator = resolveDisplayedCash({authoritativeCash: null, operatorCash: null,"
            " nav: 100000.0, totalEquity: 40000.0});",
            "out.blankStrings = resolveDisplayedCash({authoritativeCash: '', operatorCash: '  ',"
            " nav: '', totalEquity: null});",
            "process.stdout.write(JSON.stringify(out));",
        ]
    )
    out = json.loads(_run_node(script).stdout)

    assert out["both"]["source"] == "authoritative_run"
    assert out["both"]["cash"] == pytest.approx(69033.52)
    assert out["operator"]["source"] == "operator_input"
    assert out["operator"]["cash"] == pytest.approx(1234.5)
    assert out["derived"]["source"] == "derived_residual"
    assert out["derived"]["cash"] == pytest.approx(60000.0)
    assert out["derived"]["cashRatio"] == pytest.approx(60.0)
    assert out["clamped"]["cash"] == pytest.approx(0.0)
    assert out["blankOperator"]["cash"] == pytest.approx(60000.0)
    # Blank NAV falls back to the 100,000 default (what `recalcHoldings` always did), and a
    # blank/absent equity contributes 0 rather than NaN.
    assert out["blankStrings"]["cash"] == pytest.approx(100000.0)
    assert out["blankStrings"]["source"] == "derived_residual"


# ---------------------------------------------------------------------------
# 2. The shipped resolver: ONE lockstep snapshot for both legs
# ---------------------------------------------------------------------------
def test_round_mark_is_one_lockstep_snapshot():
    """Both legs come from the SAME producer, or the cash leg is honestly `null`.

    ``summary.json`` publishes both halves side by side (D8: ``nav_effective`` = what the
    breaker saw, ``nav_effective_stage2`` = what the deployed book was priced at).  The old
    implementation picked each leg independently, so a Stage-2 NAV could be paired with the
    gate's cash (review MINOR 3).
    """
    src = _app_js()
    script = "\n".join(
        [
            "var window = {};",  # `toFiniteNumber` probes `window.LiveTradingCash` first
            _extract_js_function(src, "toFiniteNumber"),
            _extract_js_function(src, "pickRoundMark"),
            "const out = {};",
            # (2) Both stage-2 legs present: both come from stage 2.
            #     gate 197,899.90 / 25,596.90, deploy 197,899.60 / 25,596.60.
            "out.bundle = pickRoundMark({summary: {",
            "  nav_effective: 197899.90, cash: 25596.90,",
            "  nav_effective_stage2: 197899.60, cash_stage2: 25596.60}});",
            # (1) Stage-2 NAV present but `cash_stage2` unrecorded: the gate's cash must NOT
            #     be borrowed -- the pair is incomplete, so cash is null.
            "out.unpairedStage2 = pickRoundMark({summary: {",
            "  nav_effective_stage2: 197899.60, cash_stage2: null,",
            "  nav_effective: 197899.90, cash: 25596.90}});",
            # A Stage-1-only archive: the gate pair IS the round's own pair.
            "out.gateOnly = pickRoundMark({summary: {nav_effective: 197899.90, cash: 25596.90}});",
            # A run response: top-level scalars are already the DEPLOY values.
            "out.run = pickRoundMark({nav_effective: 198739.52, cash: 69033.52});",
            # A pre-NAV archive: no mark anywhere -> nulls, so the card keeps its input.
            "out.legacy = pickRoundMark({summary: {}});",
            # Bundle whose only mark is the carried account state: NAV only, never a cash guess.
            "out.carried = pickRoundMark({account_state: {current_nav: 102000.0}});",
            # A fully-invested round is a RECORDED zero, not "unrecorded".
            "out.zeroCash = pickRoundMark({nav_effective: 100000.0, cash: 0.0});",
            "process.stdout.write(JSON.stringify(out));",
        ]
    )
    out = json.loads(_run_node(script).stdout)

    assert out["bundle"]["nav"] == pytest.approx(197899.60), "gate NAV leaked into the NAV box"
    assert out["bundle"]["cash"] == pytest.approx(25596.60), "gate cash leaked into the card"
    assert out["bundle"]["stage"] == "stage2"

    # MINOR 3: the incomplete stage-2 pair must not borrow the gate's cash.
    assert out["unpairedStage2"]["nav"] == pytest.approx(197899.60)
    assert out["unpairedStage2"]["cash"] is None, (
        "stage-2 NAV was paired with the GATE cash: cash must be null so the card derives"
    )
    assert out["unpairedStage2"]["cash"] != pytest.approx(25596.90)
    assert out["unpairedStage2"]["stage"] == "stage2"

    assert out["gateOnly"]["nav"] == pytest.approx(197899.90)
    assert out["gateOnly"]["cash"] == pytest.approx(25596.90)
    assert out["gateOnly"]["stage"] == "gate"

    # The reported round still resolves verbatim.
    assert out["run"]["nav"] == pytest.approx(NAV_EFFECTIVE)
    assert out["run"]["cash"] == pytest.approx(ROUND_CASH)

    assert out["legacy"]["nav"] is None
    assert out["legacy"]["cash"] is None, (
        "an unrecorded cash leg must stay null, never be recycled as 0"
    )
    assert out["legacy"]["stage"] is None

    assert out["carried"]["nav"] == pytest.approx(102000.0)
    assert out["carried"]["cash"] is None
    assert out["carried"]["stage"] == "account_state"

    assert out["zeroCash"]["cash"] == pytest.approx(0.0), (
        "0 is a legitimate fully-invested balance; only a negative leg is unusable"
    )


def test_reported_round_end_to_end_through_mark_and_resolver():
    """The reported scenario, through BOTH shipped pieces: mark -> card.

    `pickRoundMark` supplies the pair, `resolveDisplayedCash` prints the cash leg verbatim:
    `nav 198739.52 -> cash 69033.52`, `source == "authoritative_run"`.
    """
    src = _app_js()
    script = "\n".join(
        [
            "var window = {};",
            _extract_js_function(src, "toFiniteNumber"),
            _extract_js_function(src, "pickRoundMark"),
            f"const {{resolveDisplayedCash}} = require({json.dumps(_CASH_RESOLVE_JS)});",
            "const mark = pickRoundMark({nav_effective: " + repr(NAV_EFFECTIVE)
            + ", cash: " + repr(ROUND_CASH) + "});",
            "const card = resolveDisplayedCash({authoritativeCash: mark.cash, operatorCash: null,"
            " nav: mark.nav, totalEquity: " + repr(NAV_EQUITY) + "});",
            "process.stdout.write(JSON.stringify({mark, card}));",
        ]
    )
    out = json.loads(_run_node(script).stdout)
    assert out["mark"]["nav"] == pytest.approx(NAV_EFFECTIVE)
    assert out["mark"]["cash"] == pytest.approx(ROUND_CASH)
    assert out["card"]["cash"] == pytest.approx(ROUND_CASH)
    assert out["card"]["source"] == "authoritative_run"
    assert f"{out['card']['cashRatio']:.1f}%" == "34.7%"


# ---------------------------------------------------------------------------
# 3. Structural contract (no JS runtime required)
# ---------------------------------------------------------------------------
def test_index_loads_the_cash_resolver_before_app_js():
    html = _read(_INDEX_HTML)
    resolver = html.find("/static/js/cash_resolve.js")
    app = html.find("/static/js/app.js")
    assert resolver >= 0, "index.html does not load cash_resolve.js"
    assert app >= 0
    assert resolver < app, "cash_resolve.js must be loaded BEFORE app.js"


def test_recalc_holdings_resolves_cash_through_the_shared_precedence():
    """The card's cash is no longer an unconditional `nav - totalEquity`."""
    src = _app_js()
    body = _extract_js_function(src, "recalcHoldings")

    assert "resolveCashForCard({" in body
    assert "authoritativeCash: state.authoritativeCash" in body
    assert "operatorCash: getCashInput()" in body
    assert "Math.max(0, nav - totalEquity)" not in body, (
        "the derived residual is still computed unconditionally in recalcHoldings"
    )
    # Formatting contract unchanged (2dp money, 1dp percent).
    assert "minimumFractionDigits: 2, maximumFractionDigits: 2" in body
    assert ".toFixed(1)}%" in body
    assert "dataset.cashSource" in body


def test_derived_branch_is_last_inside_the_resolver():
    """Belt-and-braces TEXT check, explicitly not a behavioural proof.

    The behavioural precedence is pinned by the ``node`` tests above (which execute the shipped
    function); this one only guards the reading order when no JS runtime exists, so a swapped
    branch here is a strong smell but not by itself proof -- hence the ``if``-condition
    assertions below, which tie each label to its guard rather than to a bare string.
    """
    src = _read(_CASH_RESOLVE_JS)
    auth_guard = "if (authoritative !== null) {"
    operator_guard = "} else if (operator !== null) {"
    assert auth_guard in src and operator_guard in src, (
        "the resolver's branch guards were renamed; re-anchor this check"
    )
    assert src.find(auth_guard) < src.find(operator_guard) < src.find("cash = Math.max(0.0,"), (
        "guards are not in authoritative -> operator -> derived order"
    )
    auth = src.find("source = \"authoritative_run\"")
    operator = src.find('source = "operator_input"')
    derived = src.find('source = "derived_residual"', operator)
    assert 0 <= auth < operator < derived, (
        "the resolver must check authoritative -> operator -> derived, in that order"
    )
    assert src.find(auth_guard) < auth < src.find(operator_guard), (
        "the `authoritative_run` label is not inside the authoritative branch"
    )
    assert src.find(operator_guard) < operator < src.find("} else {", operator), (
        "the `operator_input` label is not inside the operator branch"
    )


def test_run_completion_writes_the_engine_mark_back_into_the_nav_box():
    """A completed run must stamp `nav_effective` into `#input-portfolio-value`."""
    src = _app_js()

    # The write is programmatic and guarded, never a bare `.value =`.
    assert "function setNavInputValue(value)" in src
    assert "state.isProgrammaticNavWrite = true;" in src

    # Engine mark selection: ONE lockstep resolver (stage-2 pair -> gate pair -> carried NAV).
    pick = _extract_js_function(src, "pickRoundMark")
    for key in ('navOf("nav_effective_stage2")', 'cashOf("cash_stage2", "cash_source_stage2")',
                'navOf("nav_effective")', 'cashOf("cash", "cash_source")',
                "account.current_nav", "account.nav_mtm"):
        assert key in pick, f"pickRoundMark lost the `{key}` leg"
    assert "pickEngineNav" not in src and "pickAuthoritativeCash" not in src, (
        "the two independent (unguarded) pickers must be gone, not merely unused"
    )
    apply_body = _extract_js_function(src, "applyRunResultTimebase")
    assert "const mark = pickRoundMark(data);" in apply_body
    assert "state.authoritativeCash = mark.cash;" in apply_body
    assert "setNavInputValue(mark.nav)" in apply_body, (
        "the NAV box must be written from the SAME resolver result as the cash"
    )

    # Bound by every run/bundle path...
    assert "applyRunResultTimebase(bundle);" in _extract_js_function(src, "renderRunBundle")
    deploy = _extract_js_function(src, "refreshHoldingsAfterDeploy")
    assert "applyRunResultTimebase(data);" in deploy
    assert deploy.find("applyRunResultTimebase(data);") < deploy.find("renderHoldingsTable(state.holdings)"), (
        "the timebase must be bound BEFORE the table is repainted, or the first recalc is stale"
    )


def test_operator_edit_invalidates_the_authoritative_cash_explicitly():
    src = _app_js()
    idx = src.find('el.inputValue.addEventListener("input"')
    assert idx >= 0
    handler = _src_slice(src, idx, "});")
    assert "state.authoritativeCash = null;" in handler
    assert "state.isProgrammaticNavWrite" in handler, (
        "a programmatic NAV write must not be treated as an operator edit"
    )


def test_operator_cash_entry_clears_the_run_binding_and_takes_effect():
    """`#input-cash` is symmetric with the NAV box, and it must be READ, not just stored.

    Without this listener the field was inert: precedence 1 (the run's own cash leg) always
    won, so typing `#input-cash` changed nothing on screen, while a stale value left in the
    box kept influencing the card across date switches with no visible reason.
    """
    src = _app_js()

    # (a) A real-user listener exists on `#input-cash` and clears the binding.
    idx = src.find('el.inputCash.addEventListener("input"')
    assert idx >= 0, "no `input` listener on #input-cash: an operator cash entry is inert"
    handler = _src_slice(src, idx, "});")
    assert "state.authoritativeCash = null;" in handler, (
        "typing cash must drop the run binding, or precedence 1 keeps overriding the entry"
    )
    assert "state.isProgrammaticNavWrite" in handler, (
        "a programmatic write into #input-cash must not be read as an operator edit"
    )
    assert "recalcHoldings();" in handler, "the card must repaint on a cash entry"
    # The listener must be bound, not merely defined...
    assert src.find("if (el.inputCash) {") < src.find('el.inputCash.addEventListener("input"'), (
        "the #input-cash listener is registered outside its null-guarded block"
    )
    # ...and `getCashInput()` semantics are untouched (blank => null, never 0).
    cash_input = _extract_js_function(src, "getCashInput")
    assert 'if (raw === "") return null;' in cash_input
    assert "return null;" in cash_input and "return 0" not in cash_input
    # A date switch must not destroy the operator's typed cash value.
    assert "el.inputCash.value" not in _extract_js_function(src, "handleDateSelected")

    # (b) With the binding cleared, the resolver reads the typed value (precedence 2) and
    # falls back to the derived residual when the box is blank.
    script = "\n".join(
        [
            f"const {{resolveDisplayedCash}} = require({json.dumps(_CASH_RESOLVE_JS)});",
            "const out = {};",
            # Same legs as the reported round, binding cleared by a keystroke, cash typed in.
            "out.typed = resolveDisplayedCash({authoritativeCash: null, operatorCash: 69033.52,"
            " nav: 197652.52, totalEquity: 129706.0});",
            # ...and the operator then blanks the field.
            "out.blank = resolveDisplayedCash({authoritativeCash: null, operatorCash: null,"
            " nav: 198739.52, totalEquity: 129706.0});",
            "process.stdout.write(JSON.stringify(out));",
        ]
    )
    out = json.loads(_run_node(script).stdout)
    assert out["typed"]["source"] == "operator_input"
    assert out["typed"]["cash"] == pytest.approx(69033.52)
    assert out["typed"]["cash"] != pytest.approx(BUGGY_RESIDUAL, abs=1e-6)
    assert out["blank"]["source"] == "derived_residual"
    assert out["blank"]["cash"] == pytest.approx(69033.52), (
        "blanking the field must hand the card back to the two visible legs"
    )


def test_date_change_clears_the_binding_and_fallback_keeps_last_settled_separate():
    src = _app_js()
    body = _extract_js_function(src, "handleDateSelected")

    assert body.find("state.authoritativeCash = null;") < body.find("/api/runs/"), (
        "a date switch must invalidate the cash binding before any archive is loaded"
    )
    # The previous-run fallback must NOT bind last-settled cash to a re-priced book...
    fallback = body[body.find("/api/runs/previous?date=") :]
    assert "state.authoritativeCash = null;" in fallback
    # ...and must say where the cash it mentions actually came from.
    assert "renderCashLastRecord(prevData.bundle?.summary || null)" in fallback


def test_session_state_never_persists_the_cash_binding():
    src = _app_js()
    save = _extract_js_function(src, "saveSessionState")
    assert "authoritativeCash" not in save, (
        "persisting the cash binding would resurrect another date's cash after a restore"
    )
    restore = _extract_js_function(src, "restoreSessionState")
    assert "state.authoritativeCash = null;" in restore


# ---------------------------------------------------------------------------
# 4. Review closures (2026-11-18): MINOR 1 / MINOR 2 / NIT 4
# ---------------------------------------------------------------------------
def test_empty_plan_deploy_branch_clears_the_cash_binding():
    """MINOR 1: keeping the previous round's cash against a fresh NAV is a mixed timebase.

    The branch refreshes the NAV box but cannot repaint the table (no `holdings_plan`), so the
    only coherent card is the derived residual against the new mark -- never last round's cash
    (an all-cash account would print the old cash instead of ~`nav`).
    """
    src = _app_js()
    deploy = _extract_js_function(src, "refreshHoldingsAfterDeploy")
    idx = deploy.find("if (!plan || Object.keys(plan).length === 0)")
    assert idx >= 0
    branch = _src_slice(deploy, idx, "return;")
    assert "state.authoritativeCash = null;" in branch, (
        "the empty-plan branch keeps the previous round's cash bound to a new NAV"
    )
    assert branch.find("state.authoritativeCash = null;") < branch.find("recalcHoldings();"), (
        "the binding must be cleared BEFORE the card repaints"
    )
    # The bound path must still bind from the SAME lockstep resolver.
    assert "applyRunResultTimebase(data);" in deploy


def test_nav_provenance_strip_shows_both_stages_when_they_differ():
    """MINOR 2: the strip printed gate cash while the card printed deploy cash."""
    src = _app_js()
    body = _extract_js_function(src, "renderNavProvenance")
    assert "data.cash_stage2" in body, "the strip ignores the deploy cash leg entirely"
    assert "现金(门禁/盘前)" in body and "现金(部署/盘后)" in body
    assert "nav_effective_stage2" in body and "部署净值(盘后)" in body
    # Dual labels are conditional on the two stages actually differing, and the single-stage
    # case keeps today's wording (one `现金 ...` part, no invented label).
    assert "stagesDiffer(cashGate, cashDeploy)" in body
    assert body.find("if (cashDual)") < body.find("现金(门禁/盘前)")


def test_cash_source_missing_is_unrecorded_but_explicit_zero_is_recorded():
    """MINOR A: mirror the backend's `cash_source` check instead of trusting any number.

    `missing` means "the engine could not value cash"; `_cash_residual_from_summary`
    (`execution_service.py:2706-2708`) refuses it, and `compute_nav` may only fail closed while
    that invariant holds.  `explicit` / `bil_fallback` are measured.  A carrier that states no
    source keeps the old numeric-only behaviour.
    """
    src = _app_js()
    script = "\n".join(
        [
            "var window = {};",
            _extract_js_function(src, "toFiniteNumber"),
            _extract_js_function(src, "pickRoundMark"),
            "const out = {};",
            'out.missingZero = pickRoundMark({cash: 0.0, cash_source: "missing",'
            " nav_effective: 200000});",
            'out.explicitZero = pickRoundMark({cash: 0.0, cash_source: "explicit",'
            " nav_effective: 200000});",
            # No source key at all -> numeric only (unchanged behaviour).
            "out.sourceAbsent = pickRoundMark({cash: 0.0, nav_effective: 200000});",
            'out.bilFallback = pickRoundMark({cash: 1234.0, cash_source: "bil_fallback",'
            " nav_effective: 200000});",
            # The stage-2 source is the ONLY source consulted for the stage-2 leg: the gate's
            # healthy "explicit" must not rescue a `missing` stage-2 cash.
            'out.stage2Missing = pickRoundMark({summary: {'
            'nav_effective_stage2: 197899.60, cash_stage2: 0.0, cash_source_stage2: "missing",'
            ' cash: 25596.90, cash_source: "explicit"}});',
            "out.reported = pickRoundMark({nav_effective: 198739.52, cash: 69033.52});",
            "process.stdout.write(JSON.stringify(out));",
        ]
    )
    out = json.loads(_run_node(script).stdout)

    assert out["missingZero"]["nav"] == pytest.approx(200000.0)
    assert out["missingZero"]["cash"] is None, (
        "a `missing` cash_source must never be printed as a measured 0.00"
    )
    assert out["explicitZero"]["cash"] == pytest.approx(0.0), (
        "a recorded fully-invested balance is a real zero and must stay bound"
    )
    assert out["sourceAbsent"]["cash"] == pytest.approx(0.0), (
        "an absent source key must keep the numeric-only behaviour"
    )
    assert out["bilFallback"]["cash"] == pytest.approx(1234.0)
    assert out["stage2Missing"]["cash"] is None, (
        "the gate's healthy source rescued a `missing` stage-2 cash: sources are per-carrier"
    )
    assert out["stage2Missing"]["nav"] == pytest.approx(197899.60)
    # The reported round is untouched by the provenance check.
    assert out["reported"]["nav"] == pytest.approx(NAV_EFFECTIVE)
    assert out["reported"]["cash"] == pytest.approx(ROUND_CASH)


def test_empty_plan_branch_discloses_the_derived_residual():
    """MINOR B: the number stays, but it is labelled as a residual over a stale table."""
    src = _app_js()
    deploy = _extract_js_function(src, "refreshHoldingsAfterDeploy")
    idx = deploy.find("if (!plan || Object.keys(plan).length === 0)")
    assert idx >= 0
    branch = _src_slice(deploy, idx, "return;")

    assert 'el.valHoldingsCash.dataset.cashNote = "no_holdings_plan";' in branch, (
        "the empty-plan branch shows a residual over a table it refused to repaint, silently"
    )
    assert "本轮未返回持仓计划，表格保持上一轮" in branch
    assert "推导残差（非本轮实测）" in branch
    assert branch.find("recalcHoldings();") < branch.find("dataset.cashNote"), (
        "`recalcHoldings` repaints the tooltip, so the note must be applied after it"
    )
    # The disclosure must not rewrite the source label: `recalcHoldings` already reports
    # `derived_residual`, and this branch must not claim anything else.
    assert "dataset.cashSource" not in branch


def test_ratio_is_clamped_but_the_cash_figure_stays_verbatim():
    """NIT 4: only the PERCENTAGE is clamped; the cash figure is never corrected.

    Both out-of-range cases are engine-reported truth that the card must not silently rewrite:
    an over-NAV cash (e.g. an operator override typed beside a stale book) and a negative one.
    """
    script = "\n".join(
        [
            f"const {{resolveDisplayedCash}} = require({json.dumps(_CASH_RESOLVE_JS)});",
            "const out = {};",
            "out.over = resolveDisplayedCash({authoritativeCash: 250000.0, operatorCash: null,"
            " nav: 100000.0, totalEquity: 40000.0});",
            "out.negative = resolveDisplayedCash({authoritativeCash: -5000.0, operatorCash: null,"
            " nav: 100000.0, totalEquity: 40000.0});",
            "out.normal = resolveDisplayedCash({authoritativeCash: 69033.52, operatorCash: null,"
            " nav: 198739.52, totalEquity: 129706.0});",
            "process.stdout.write(JSON.stringify(out));",
        ]
    )
    out = json.loads(_run_node(script).stdout)

    assert out["over"]["cash"] == pytest.approx(250000.0), "the cash figure must stay verbatim"
    assert out["over"]["cashRatio"] == pytest.approx(100.0), "ratio must clamp at 100%"
    assert out["negative"]["cash"] == pytest.approx(-5000.0), "negative cash is shown as reported"
    assert out["negative"]["cashRatio"] == pytest.approx(0.0), "ratio must clamp at 0%"
    assert out["normal"]["cashRatio"] == pytest.approx(34.73567813789628, abs=1e-9), (
        "in-range ratios must be untouched by the clamp"
    )


def _src_slice(source: str, start: int, terminator: str) -> str:
    """The text from `start` up to the next unrelated `terminator` (crude, one handler)."""
    end = source.find(terminator, start)
    assert end > start
    return source[start : end + len(terminator)]
