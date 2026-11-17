"""Offline tests for `common/nav_hwm.py` and `scripts/repair_live_nav_hwm.py`.

FORENSIC CONTEXT (the incident every assertion here is written against)
-----------------------------------------------------------------------
The live archive ``202609/runs/chan_four_state_blend/custom_astock_202609_20_new/`` stores
a round as ``<date>/<HHMMSS>/{account_state.json,nav_breakdown.json,...}``.
``nav_breakdown.json`` carries the NAV as two legs (``equity`` + ``cash``); the account
state carries the single number the engine ratcheted.

On **2026-11-05** the two disagreed:

* ``nav_breakdown.json`` -> ``equity = 186218.00``, ``cash = 12592.57``, true NAV
  **198810.57** (both legs, that round);
* ``account_state.json`` -> ``current_nav = 208830.78``
  = ``186218.00`` (today's equity) + ``22612.78`` (**2026-11-04's cash leg**).

That is a mixed-timebase sum, +10020.21 of phantom equity. ``peak_nav`` only ratchets up,
so 208830.78 became the permanent high-water mark; ``drawdown_pct`` -- and therefore the
circuit breaker -- has been measured against a number the account never reached. The real
ATH is **203049.96** (2026-09-29): reported -5.77% vs true -3.08% at 2026-11-16.

WHAT THIS SUITE PINS
--------------------
1. ``leg_nav`` / ``hwm_eligibility`` truth tables, including the structured
   ``cash_as_of != as_of`` proof, the writer's own ``identical_to_previous_round_residual``
   warning, and ``stale_symbols``.
2. ``restate_peak`` cannot be set by an ineligible phantom -- otherwise the repair would
   reproduce the defect it exists to undo.
3. ``dedupe_adjacent`` collapses the re-posted 199935.46 pair.
4. A miniature archive that reproduces 2026-11-05 end to end: the dry run exits 1 and
   writes nothing, ``--apply`` exits 0 and restores the peak, a byte-exact ``.orig`` is
   left behind, the second ``--apply`` is byte-identical, and a manifest records the
   before/after md5s.
5. The MANIFEST-ERROR abort (``ManifestError``): a corrupt ``nav_hwm_manifest.<date>.json``
   beside a genuinely pending repair exits 2 having written **0** files (no ``.orig``, no
   rewritten ``account_state.json``, ledger byte-identical, error naming its full path), and
   the same corrupt ledger with nothing pending is never silently replaced. A malformed
   ``passes`` ENTRY is the same abort (it used to be silently dropped while the ledger was
   rewritten).
6. The WRITE path is transactional and scoped: an ``OSError`` on the second of three
   targets reports which files were rewritten and which failed, exits 2, leaves no
   ``*.tmp-navhwm`` anywhere, and still writes a manifest pass marked
   ``incomplete: true``; a rewrite that would move a field outside the repair scope aborts
   that file with a named error instead of trusting ``fields_changed``.
7. (NV13) The peak-provenance truth table, including the ``missing`` vs. ``unverifiable``
   split (a peak IS present but this run has no leg-verified series) and the opt-in
   ``--require-provable-peak`` gate refusing BOTH unprovable labels -- it must never fail
   open exactly where it cannot verify. An explicit ``--peak`` is bounded ABOVE by
   ``PEAK_PROVENANCE_TOL`` and loudly warned, never silently honoured.

100% offline: every fixture is built in ``tmp_path``; the real archive is never opened.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.nav_hwm import (  # noqa: E402
    PEAK_NAV_OPERATOR_ATH_KEY,
    PEAK_PROVENANCE_ATTRIBUTABLE_EXTERNAL,
    PEAK_PROVENANCE_LABELS,
    PEAK_PROVENANCE_LEG_VERIFIED,
    PEAK_PROVENANCE_MISSING,
    PEAK_PROVENANCE_TOL,
    PEAK_PROVENANCE_UNVERIFIABLE,
    PEAK_PROVENANCE_UNVERIFIED_INHERITED,
    dedupe_adjacent,
    drawdown,
    hwm_eligibility,
    hwm_eligibility_detail,
    leg_nav,
    max_leg_verified_nav,
    peak_provenance,
    restate_peak,
)

SCRIPT_PATH = REPO_ROOT / "scripts" / "repair_live_nav_hwm.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("repair_live_nav_hwm", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_health_module():
    """`scripts/check_live_portfolio_health.py` -- the consumer of the repaired state.

    Loaded here so the same-day-swap regression is asserted against the REAL function,
    not a transcription of it (the swap condition lives in that module).
    """
    path = REPO_ROOT / "scripts" / "check_live_portfolio_health.py"
    spec = importlib.util.spec_from_file_location("check_live_portfolio_health", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


repair = _load_script()

#: The incident's own numbers, verbatim.
MIXED_NAV = 208830.78          # account_state.current_nav, 2026-11-05 (phantom)
TRUE_NAV_1105 = 198810.57      # 186218.00 + 12592.57 (same-instant legs)
PREV_CASH = 22612.78           # 2026-11-04's cash leg, wrongly added to 11-05
REAL_ATH = 203049.96           # 2026-09-29


def _breakdown(**overrides):
    payload = {
        "as_of": "2026-11-05",
        "cash_as_of": "2026-11-05",
        "equity": 186218.00,
        "cash": 12592.57,
        "nav_source": "computed",
        "nav_warnings": [],
        "stale_symbols": [],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 1. leg_nav
# ---------------------------------------------------------------------------
def test_leg_nav_adds_the_two_same_instant_legs():
    assert leg_nav(_breakdown()) == pytest.approx(TRUE_NAV_1105)
    # the phantom is exactly "today's equity + yesterday's cash": same legs, other cash
    assert leg_nav(_breakdown(cash=PREV_CASH)) == pytest.approx(MIXED_NAV)


@pytest.mark.parametrize(
    "breakdown",
    [
        {"equity": 186218.00},                                  # cash missing
        {"cash": 12592.57},                                     # equity missing
        {"equity": 186218.00, "cash": None},
        {"equity": 186218.00, "cash": float("nan")},
        {"equity": float("inf"), "cash": 12592.57},
        {"equity": 186218.00, "cash": -1.0},                    # negative cash: not a balance
        {"equity": "n/a", "cash": 12592.57},
        {},                                                     # empty snapshot
    ],
)
def test_leg_nav_rejects_anything_it_cannot_add(breakdown):
    assert leg_nav(breakdown) is None


def test_leg_nav_rejects_a_non_mapping():
    assert leg_nav(None) is None
    assert leg_nav([("equity", 1.0), ("cash", 1.0)]) is None


# ---------------------------------------------------------------------------
# 2. hwm_eligibility truth table (PRECEDENCE: structured field > warning text)
#
# The producer (`execution_service.py`) appends `cash_from_archive:` on ANY
# archive-sourced cash, INCLUDING a same-session re-run (`_archive_cash_is_same_session`),
# and it writes `identical_to_previous_round_residual` to `cash_as_of_warning`, never to
# `nav_warnings`. A guard that barres on the warning alone freezes the HWM on a new-high
# day (the breaker then UNDER-trips), and a guard that only reads `nav_warnings` never
# sees the residual marker at all. Both are pinned below.
# ---------------------------------------------------------------------------
ARCHIVE_WARNING = "cash_from_archive: cash=109685.11 取自上一轮 summary.json (/x/2026-11-11)"
RESIDUAL = "identical_to_previous_round_residual"


@pytest.mark.parametrize(
    ("label", "breakdown", "expected_ok", "reason_fragment"),
    [
        # --- 1. structured pair present and EQUAL: the field decides, alone -------------
        ("structured_equal", _breakdown(), True, ""),
        # THE MEDIUM FINDING: same-session re-run -- the marker is there, the legs are one
        # instant, and the NAV must stay eligible or the HWM never advances.
        (
            "structured_equal_plus_cash_from_archive",
            _breakdown(nav_warnings=[ARCHIVE_WARNING]),
            True,
            "",
        ),
        # THE REAL 2026-11-16 PAYLOAD SHAPE: residual marker lives in `cash_as_of_warning`.
        (
            "structured_equal_plus_residual_warning",
            _breakdown(cash_as_of_warning=RESIDUAL),
            True,
            "",
        ),
        # --- 2. structured pair present and UNEQUAL: hard bar ---------------------------
        (
            "structured_mismatch",
            _breakdown(cash_as_of="2026-11-04"),
            False,
            "cash_as_of='2026-11-04' != as_of='2026-11-05'",
        ),
        # the structured bar wins even when the warnings would have been silent
        (
            "structured_mismatch_no_warnings",
            _breakdown(cash_as_of="2026-11-04", nav_warnings=[]),
            False,
            "mixed-timebase",
        ),
        # --- 3. no structured pair: warnings are the only evidence ---------------------
        # legacy snapshot, nothing suspicious -> absence is not proof of staleness
        ("legacy_clean", _breakdown(cash_as_of=None), True, ""),
        (
            "legacy_cash_from_archive",
            _breakdown(cash_as_of=None, nav_warnings=[ARCHIVE_WARNING]),
            False,
            "cash_from_archive",
        ),
        # NIT 5: the residual marker is written to `cash_as_of_warning`, not `nav_warnings`
        (
            "legacy_residual_marker_in_cash_as_of_warning",
            _breakdown(cash_as_of=None, cash_as_of_warning=RESIDUAL),
            False,
            "identical_to_previous_round_residual",
        ),
        (
            "legacy_residual_marker_in_nav_warnings",
            _breakdown(cash_as_of=None, nav_warnings=[RESIDUAL + ": cash=22612.78"]),
            False,
            "identical_to_previous_round_residual",
        ),
        # NIT 6: PREFIX matching, mirroring `_cash_from_archive_warning`. A warning that
        # merely mentions the phrase later in its text is not the producer's claim.
        (
            "legacy_marker_mentioned_not_prefixed",
            _breakdown(cash_as_of=None, nav_warnings=[f"note: {ARCHIVE_WARNING}"]),
            True,
            "",
        ),
        # --- 4. unconditional bars (not timebase claims) -------------------------------
        # a carried-forward mark is not this instant's equity, whatever cash_as_of says
        (
            "structured_equal_but_stale_symbols",
            _breakdown(stale_symbols=["600900.SH"]),
            False,
            "stale_symbols",
        ),
        (
            "legacy_stale_symbols",
            _breakdown(cash_as_of=None, stale_symbols=["600900.SH"]),
            False,
            "stale_symbols",
        ),
        # operator form value: a human number, not a snapshot
        (
            "structured_equal_but_form_fallback",
            _breakdown(nav_source="form_fallback"),
            False,
            "form_fallback",
        ),
        # unrelated warning (the real 11-05 file carries only this one)
        (
            "unrelated_warning",
            _breakdown(nav_warnings=["provider_key_resolved: fuyao -> FuyaoDataProvider"]),
            True,
            "",
        ),
    ],
)
def test_hwm_eligibility_truth_table(label, breakdown, expected_ok, reason_fragment):
    ok, reason = hwm_eligibility(breakdown)
    assert ok is expected_ok, f"{label}: {reason}"
    assert reason_fragment in reason, f"{label}: {reason}"
    if expected_ok:
        assert reason == ""


def test_hwm_eligibility_detail_records_the_deciding_evidence_and_corroboration():
    """The verdict alone is not auditable: the detail says WHICH evidence won.

    On a same-instant re-run the snapshot carries `cash_from_archive` and is STILL
    eligible, so `reason` is empty and only `deciding_evidence` /
    `corroborating_warnings` can explain the call.
    """
    eligible = hwm_eligibility_detail(_breakdown(nav_warnings=[ARCHIVE_WARNING]))
    assert eligible["eligible"] is True
    assert eligible["structured_timebase"] is True
    assert "cash_as_of='2026-11-05' == as_of='2026-11-05'" in eligible["deciding_evidence"]
    assert "warning text cannot override" in eligible["deciding_evidence"]
    assert [w["marker"] for w in eligible["corroborating_warnings"]] == ["cash_from_archive"]
    assert eligible["corroborating_warnings"][0]["field"] == "nav_warnings"

    barred = hwm_eligibility_detail(_breakdown(cash_as_of="2026-11-04"))
    assert barred["eligible"] is False
    assert barred["structured_timebase"] is False
    assert "structured:" in barred["deciding_evidence"]

    legacy = hwm_eligibility_detail(_breakdown(cash_as_of=None, cash_as_of_warning=RESIDUAL))
    assert legacy["eligible"] is False
    assert legacy["structured_timebase"] is None
    assert "cash_as_of_warning" in legacy["deciding_evidence"]
    assert "no structured" in legacy["deciding_evidence"]


def test_hwm_eligibility_rejects_a_bare_string_warning_container():
    """``nav_warnings`` may arrive as a single string; it must still be scanned (legacy)."""
    ok, reason = hwm_eligibility(
        _breakdown(cash_as_of=None, nav_warnings=RESIDUAL + ": cash=22612.78")
    )
    assert ok is False
    assert "identical_to_previous_round_residual" in reason


def test_hwm_eligibility_rejects_a_non_mapping():
    ok, reason = hwm_eligibility(None)
    assert ok is False
    assert "mapping" in reason


# ---------------------------------------------------------------------------
# 3. restate_peak / dedupe_adjacent / drawdown
# ---------------------------------------------------------------------------
def test_restate_peak_ignores_the_ineligible_phantom():
    candidates = [
        ("2026-11-04/203553", 200076.78, True),
        ("2026-11-05/202418", MIXED_NAV, False),   # the phantom: not eligible
        ("2026-09-29/215823", REAL_ATH, True),
        ("2026-11-06/184826", 186476.57, False),
    ]
    assert restate_peak(candidates) == pytest.approx(REAL_ATH)


def test_restate_peak_raises_when_nothing_is_eligible():
    """A peak must never be invented: 0.0 would defeat the caller's guard rail."""
    with pytest.raises(ValueError):
        restate_peak([("2026-11-05/202418", MIXED_NAV, False)])
    with pytest.raises(ValueError):
        restate_peak([])


def test_restate_peak_skips_a_non_finite_nav_even_when_flagged_eligible():
    assert restate_peak(
        [("a", float("nan"), True), ("b", float("inf"), True), ("c", 199935.46, True)]
    ) == pytest.approx(199935.46)


def test_dedupe_adjacent_collapses_the_reposted_nav():
    assert dedupe_adjacent([199935.46, 199935.46]) == [199935.46]
    assert dedupe_adjacent([199595.4, 199935.46, 199935.46, 200076.78]) == [
        199595.4,
        199935.46,
        200076.78,
    ]
    # a genuine one-cent move is not a duplicate
    assert dedupe_adjacent([199935.46, 199935.47]) == [199935.46, 199935.47]
    assert dedupe_adjacent([]) == []


def test_drawdown_matches_the_engine_convention():
    assert drawdown(196789.34, REAL_ATH) == pytest.approx(-0.0308, abs=5e-5)
    assert drawdown(MIXED_NAV, MIXED_NAV) == 0.0
    with pytest.raises(ValueError):
        drawdown(196789.34, 0.0)
    with pytest.raises(ValueError):
        drawdown(MIXED_NAV, float("nan"))


# ---------------------------------------------------------------------------
# 4. end-to-end miniature archive reproducing 2026-11-05
# ---------------------------------------------------------------------------
def _write_json(path: Path, payload) -> bytes:
    """The archive's own writer style (indent=2, UTF-8, no trailing newline)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    path.write_bytes(raw)
    return raw


def _round(root: Path, when: str, instance: str, *, equity: float, cash: float,
           current_nav: float, peak_nav: float, drawdown_pct: float,
           nav_history: list, extra_state: dict | None = None,
           cash_as_of: str | None = None,
           prior_state: dict | None = None) -> Path:
    run = root / when / instance
    breakdown = _breakdown(as_of=when, cash_as_of=cash_as_of or when, equity=equity, cash=cash)
    _write_json(run / "nav_breakdown.json", breakdown)
    state = {
        "as_of_date": when,
        "current_nav": current_nav,
        "peak_nav": peak_nav,
        "drawdown_pct": drawdown_pct,
        "tier1_consecutive_bars": 0,
        "circuit_breaker_tier": "NORMAL",
        "nav_history_10d": list(nav_history),
        "base_version": "1.8.0",
        "lifecycle_states": {"600900.SH": {"state": "HELD_ACTIVE", "current_weight": 0.1186}},
    }
    if prior_state is not None:
        state["prior_state"] = prior_state
    state.update(extra_state or {})
    _write_json(run / "account_state.json", state)
    return run


#: The stale nested copy the production archive actually carries: previous day's
#: `current_nav`, but the PHANTOM peak, the stale drawdown and a duplicate-laden history.
def _stale_prior(as_of: str, current_nav: float, *, peak: float = MIXED_NAV,
                 drawdown_pct: float = -0.05851359651101242,
                 history: list | None = None) -> dict:
    return {
        "as_of_date": as_of,
        "current_nav": current_nav,
        "peak_nav": peak,
        "drawdown_pct": drawdown_pct,
        "tier1_consecutive_bars": 0,
        "circuit_breaker_tier": "NORMAL",
        "nav_history_10d": list(history or [199935.46, 199935.46, 200076.78, MIXED_NAV]),
        "entry_prices": {"600900.SH": 29.57},
    }


@pytest.fixture
def mini_archive(tmp_path):
    """Five rounds: the ATH, a clean 11-04, the 11-05 defect, its 11-06 repeat, 11-16.

    ``prior_state`` blocks mirror the production shapes: 11-05/11-06/11-16 copy the
    phantom peak + duplicate history from the round before, which is the NESTED gap found
    after the first repair pass.
    """
    root = tmp_path / "runs"
    _round(
        root, "2026-09-29", "215823",
        equity=190000.00, cash=REAL_ATH - 190000.00,
        current_nav=REAL_ATH, peak_nav=REAL_ATH, drawdown_pct=0.0,
        nav_history=[REAL_ATH],
    )
    # 2026-11-04: already consistent (top level AND its nested copy) -> must stay untouched
    _round(
        root, "2026-11-04", "203553",
        equity=177464.00, cash=22612.78,      # 200076.78
        current_nav=200076.78, peak_nav=REAL_ATH,
        drawdown_pct=200076.78 / REAL_ATH - 1.0,
        nav_history=[REAL_ATH, 200076.78],
        prior_state={
            "as_of_date": "2026-09-29",
            "current_nav": REAL_ATH,
            "peak_nav": REAL_ATH,
            "drawdown_pct": 0.0,
            "nav_history_10d": [REAL_ATH],
        },
    )
    # 2026-11-05: true 198810.57, account says 208830.78 (yesterday's cash added in)
    _round(
        root, "2026-11-05", "202418",
        equity=186218.00, cash=12592.57,
        current_nav=MIXED_NAV, peak_nav=MIXED_NAV, drawdown_pct=0.0,
        nav_history=[199595.4, 200076.78, MIXED_NAV],
        prior_state=_stale_prior(
            "2026-11-04", 200076.78, history=[REAL_ATH, 200076.78, 200076.78]
        ),
    )
    # 2026-11-06: the repeat -- the account says 186476.57 = 173884.00 + 12592.57 while
    # the snapshot's own legs are 173884.00 + 25596.90 = 199480.90
    _round(
        root, "2026-11-06", "184826",
        equity=173884.00, cash=25596.90,
        current_nav=186476.57, peak_nav=MIXED_NAV, drawdown_pct=186476.57 / MIXED_NAV - 1.0,
        nav_history=[199595.4, 200076.78, MIXED_NAV, 186476.57],
        prior_state=_stale_prior("2026-11-05", MIXED_NAV),
    )
    # 2026-11-16/124508: the production round whose same-day re-run reads `prior_state`
    _round(
        root, "2026-11-16", "124508",
        equity=49624.00, cash=147165.34,     # 196789.34
        current_nav=196789.34, peak_nav=MIXED_NAV,
        drawdown_pct=196789.34 / MIXED_NAV - 1.0,
        nav_history=[199935.46, 200076.78, MIXED_NAV, 199480.90, 196397.11, 196611.34, 196789.34],
        prior_state=_stale_prior("2026-11-06", 196611.34),
    )
    # auxiliary directory (a re-run snapshot copy): must never be repaired
    aux = root / "2026-11-06" / "184826.pre-repair-20261112T073101Z"
    _write_json(aux / "account_state.json", {"current_nav": 186476.57, "peak_nav": MIXED_NAV})
    return root


def _tree_fingerprint(root: Path) -> dict:
    """md5 + mtime + size of every file under ``root`` (a "was it touched?" probe)."""
    out = {}
    for dirpath, _dirnames, filenames in os.walk(str(root)):
        for name in filenames:
            path = Path(dirpath) / name
            st = path.stat()
            out[str(path.relative_to(root))] = (
                hashlib.md5(path.read_bytes()).hexdigest(),
                st.st_mtime_ns,
                st.st_size,
            )
    return out


def test_dry_run_reports_the_defect_and_writes_nothing(mini_archive, tmp_path, capsys):
    before = _tree_fingerprint(mini_archive)
    out_path = tmp_path / "report.json"

    code = repair.main(
        ["--runs-root", str(mini_archive), "--json-out", str(out_path)]
    )

    assert code == 1  # changes proposed but not applied
    assert _tree_fingerprint(mini_archive) == before  # nothing under the archive moved
    assert not list(mini_archive.rglob("*.orig"))
    assert not list(mini_archive.rglob("nav_hwm_manifest.*.json"))

    printed = capsys.readouterr().out
    assert "2026-11-05" in printed
    assert "MIXED_TIMEBASE" in printed
    assert "2026-11-06" in printed
    assert f"{REAL_ATH:.2f}" in printed

    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["restored_peak"] == pytest.approx(REAL_ATH)
    assert report["peak_source"] == "max eligible leg_nav"
    by_dir = {entry["dir"]: entry for entry in report["runs"]}
    defect = by_dir["2026-11-05/202418"]
    assert defect["leg_nav"] == pytest.approx(TRUE_NAV_1105)
    assert defect["current_nav"] == pytest.approx(MIXED_NAV)
    assert defect["mixed_timebase_delta"] == pytest.approx(MIXED_NAV - TRUE_NAV_1105)
    assert "MIXED_TIMEBASE" in defect["codes"]
    # the proposal restates the peak, but never the NAV itself
    assert defect["changes"]["peak_nav"]["after"] == pytest.approx(REAL_ATH)
    assert "current_nav" not in defect["changes"]
    assert by_dir["2026-11-06/184826"]["mixed_timebase_delta"] == pytest.approx(
        186476.57 - 199480.90
    )
    # the auxiliary pre-repair copy is reported as skipped, not repaired
    assert [s["dir"] for s in report["skipped"]] == [
        "2026-11-06/184826.pre-repair-20261112T073101Z"
    ]


def test_apply_restores_the_peak_and_is_idempotent(mini_archive, capsys):
    run_1105 = mini_archive / "2026-11-05" / "202418"
    state_1105 = run_1105 / "account_state.json"
    state_1106 = mini_archive / "2026-11-06" / "184826" / "account_state.json"
    breakdown_1105_raw = (run_1105 / "nav_breakdown.json").read_bytes()
    raw_before = state_1105.read_bytes()
    original = json.loads(raw_before)

    assert repair.main(["--runs-root", str(mini_archive), "--apply"]) == 0
    capsys.readouterr()

    repaired = json.loads(state_1105.read_text(encoding="utf-8"))
    assert repaired["peak_nav"] == pytest.approx(REAL_ATH)
    assert repaired["drawdown_pct"] == pytest.approx(TRUE_NAV_1105 / REAL_ATH - 1.0)
    assert repaired["nav_history_10d"] == pytest.approx(
        [REAL_ATH, 200076.78, TRUE_NAV_1105]
    )
    # 11-06 keeps its own (true) NAV in the history, no longer the phantom
    repaired_1106 = json.loads(state_1106.read_text(encoding="utf-8"))
    assert repaired_1106["peak_nav"] == pytest.approx(REAL_ATH)
    assert repaired_1106["nav_history_10d"] == pytest.approx(
        [REAL_ATH, 200076.78, TRUE_NAV_1105, 199480.90]
    )

    # ONLY the three repaired fields changed -- current_nav and every other key is intact
    for key, value in original.items():
        if key in ("peak_nav", "drawdown_pct", "nav_history_10d"):
            continue
        if key == "prior_state":
            # the nested copy has its own scope: same three fields, everything else equal
            for prior_key, prior_value in value.items():
                if prior_key in ("peak_nav", "drawdown_pct", "nav_history_10d"):
                    continue
                assert repaired["prior_state"][prior_key] == prior_value, prior_key
            continue
        assert repaired[key] == value, key
    assert (run_1105 / "nav_breakdown.json").read_bytes() == breakdown_1105_raw

    # a byte-exact .orig was left behind, and it is the pre-repair file
    orig = state_1105.with_name("account_state.json.orig")
    assert orig.exists()
    assert orig.read_bytes() == raw_before
    assert hashlib.md5(orig.read_bytes()).hexdigest() == hashlib.md5(raw_before).hexdigest()

    # the manifest records both rewritten files with before/after md5
    manifests = sorted(mini_archive.glob("nav_hwm_manifest.*.json"))
    assert len(manifests) == 1
    ledger = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert ledger["restored_peak"] == pytest.approx(REAL_ATH)
    assert ledger["rewritten"] == 3          # 11-05, 11-06, 11-16 (11-04 is already clean)
    assert ledger["nested_rewritten"] == 3   # ... and every one of them had a stale copy
    files = {entry["path"]: entry for entry in ledger["files"]}
    assert "2026-11-05/202418/account_state.json" in files
    assert "2026-11-04/203553/account_state.json" not in files
    entry = files["2026-11-05/202418/account_state.json"]
    assert entry["md5_before"] == hashlib.md5(raw_before).hexdigest()
    assert entry["md5_after"] == hashlib.md5(state_1105.read_bytes()).hexdigest()
    assert entry["md5_before"] != entry["md5_after"]
    assert set(entry["fields_changed"]) == {
        "peak_nav", "drawdown_pct", "nav_history_10d",
        "prior_state.peak_nav", "prior_state.drawdown_pct", "prior_state.nav_history_10d",
    }
    assert entry["nested_fields_changed"] == [
        "prior_state.peak_nav", "prior_state.drawdown_pct", "prior_state.nav_history_10d",
    ]
    assert entry["fields_changed"]["peak_nav"]["before"] == pytest.approx(MIXED_NAV)
    assert entry["fields_changed"]["peak_nav"]["after"] == pytest.approx(REAL_ATH)
    assert entry["fields_changed"]["prior_state.peak_nav"]["before"] == pytest.approx(MIXED_NAV)
    assert entry["fields_changed"]["prior_state.peak_nav"]["after"] == pytest.approx(REAL_ATH)
    assert "account_state.current_nav" in ledger["never_touched"]
    # the auxiliary backup directory was never rewritten
    aux_state = mini_archive / "2026-11-06" / "184826.pre-repair-20261112T073101Z"
    assert json.loads((aux_state / "account_state.json").read_text())["peak_nav"] == MIXED_NAV

    # --- second --apply: byte-identical, and it does not invent a second backup -------
    after_first = {p: p.read_bytes() for p in mini_archive.rglob("account_state.json")}
    manifest_raw = manifests[0].read_bytes()
    assert repair.main(["--runs-root", str(mini_archive), "--apply"]) == 0
    capsys.readouterr()
    after_second = {p: p.read_bytes() for p in mini_archive.rglob("account_state.json")}
    assert after_second == after_first
    assert manifests[0].read_bytes() == manifest_raw
    assert not list(mini_archive.rglob("account_state.json.orig.navhwm"))
    assert not list(mini_archive.rglob("*.tmp-navhwm"))


def test_explicit_peak_is_honoured_and_exempt_from_the_floor(mini_archive, capsys):
    """An override below the eligible maximum is the OPERATOR's call, not an error.

    The floor exists so a cross-timebase leg sum cannot pull the auto-derived peak UP
    toward the phantom -- i.e. it keeps an auto-derived peak from being proposed BELOW the
    best leg evidence. It is a LOWER bound, so it cannot possibly guard the other
    direction; that is what the upper-band warning below is for.
    """
    # 203049.96 is the real ATH; passing it explicitly must be accepted
    assert repair.main(["--runs-root", str(mini_archive), "--peak", str(REAL_ATH)]) == 1
    out = capsys.readouterr().out
    assert "floor: n/a (explicit --peak)" in out
    assert "[WARN]" not in out

    # a lower override is honoured (exit 1 = proposals, not 2 = error) and only warns
    before = _tree_fingerprint(mini_archive)
    assert repair.main(["--runs-root", str(mini_archive), "--peak", "190000"]) == 1
    out = capsys.readouterr().out
    assert "[WARN] explicit --peak 190000.00 is below the highest HWM-eligible leg NAV" in out
    assert _tree_fingerprint(mini_archive) == before   # still a dry run: nothing written


def test_explicit_peak_above_the_leg_band_is_loudly_warned_never_silent(mini_archive, capsys):
    """FINDING (MEDIUM): `--peak 208830.78` re-injects the phantom and must say so.

    The floor above is a LOWER bound and the phantom sits ABOVE it, so a below-floor-only
    warning let the operator write the exact number this tool exists to remove, verbatim
    and silently. The upper band (`max_leg_nav_all * (1 + PEAK_PROVENANCE_TOL)`) is what
    separates the two: the legitimate ATH is 0.28% above the archive's best leg NAV, the
    phantom 3.13% above it. Never a hard abort -- an operator-declared external ATH is
    legitimate -- but never silent either.
    """
    # the phantom, verbatim: warned, and still only a dry run (exit 1, nothing written)
    before = _tree_fingerprint(mini_archive)
    assert repair.main(["--runs-root", str(mini_archive), "--peak", str(MIXED_NAV)]) == 1
    out = capsys.readouterr().out
    assert _tree_fingerprint(mini_archive) == before

    warnings = [ln for ln in out.splitlines() if ln.startswith("[WARN] explicit --peak")]
    assert len(warnings) == 1, out
    warn = warnings[0]
    # both numbers and the tolerance, so a reader can judge the claim without re-deriving it
    assert "208830.78" in warn and "203049.96" in warn
    assert "0.50%" in warn and "ABOVE every leg NAV" in warn
    assert "phantom" in warn
    # it is NOT the below-floor warning: those are different findings
    assert "below the highest" not in warn

    report = repair.appraise(mini_archive, MIXED_NAV)
    assert len(report["peak_warnings"]) == 1
    assert report["restored_peak"] == pytest.approx(MIXED_NAV)  # honoured, not clamped
    # the band is a report, not a vetting gate: the proposal is still built
    assert report["targets"]

    # ... and the warning survives into the ledger, where a later reader can see it
    assert repair.main(["--runs-root", str(mini_archive), "--peak", str(MIXED_NAV), "--apply"]) == 0
    capsys.readouterr()
    ledger = json.loads(repair.manifest_path(mini_archive).read_text(encoding="utf-8"))
    assert ledger["peak_warnings"] == report["peak_warnings"]
    assert ledger["incomplete"] is False
    assert ledger["restored_peak"] == pytest.approx(MIXED_NAV)
    written = json.loads(
        (mini_archive / "2026-11-16" / "124508" / "account_state.json").read_text(encoding="utf-8")
    )
    assert written["peak_nav"] == pytest.approx(MIXED_NAV)


def test_explicit_peak_inside_the_band_is_silent(mini_archive, capsys):
    """The band is a tolerance, not "any value above the best leg NAV": 0.28% is fine."""
    # 203049.96 is 0.28% above max_leg_nav_all -> inside PEAK_PROVENANCE_TOL -> no WARN
    assert repair.main(["--runs-root", str(mini_archive), "--peak", str(REAL_ATH)]) == 1
    out = capsys.readouterr().out
    assert "[WARN]" not in out
    # the boundary itself: exactly max * (1 + tol) is still inside; one cent more is not
    max_leg_all = repair.appraise(mini_archive, None)["max_leg_nav_all"]
    edge = max_leg_all * (1.0 + PEAK_PROVENANCE_TOL)
    assert repair.appraise(mini_archive, edge)["peak_warnings"] == []
    assert repair.appraise(mini_archive, edge * 1.001)["peak_warnings"]


def test_auto_peak_floors_on_eligible_rounds_only(mini_archive):
    """A cross-timebase leg sum must not be able to pull the auto-derived peak up.

    The 11-06 round is MIXED_TIMEBASE; if its *account* NAV (the phantom direction) or an
    ineligible leg sum were allowed into the floor, the auto path would be forced toward
    the phantom instead of the eligible ATH.
    """
    report = repair.appraise(mini_archive, None)
    assert report["restored_peak"] == pytest.approx(REAL_ATH)
    assert report["max_eligible_leg_nav"] == pytest.approx(REAL_ATH)
    assert report["peak_floor_applied"] is True
    # every round's raw leg sum is still reported, just never used as a floor
    assert report["max_leg_nav_all"] == pytest.approx(REAL_ATH)


def test_concurrent_edit_between_appraise_and_apply_aborts_before_any_write(
    mini_archive, monkeypatch, capsys
):
    """The documented "concurrent edit => abort" rail must be real (review finding).

    PREVENTS: a repair that re-reads and overwrites whatever it finds. The peak is the one
    number the archive cannot re-derive, so clobbering somebody's edit destroys the record
    of what the engine actually read that day.
    """
    stale_report = repair.appraise(mini_archive, None)
    target = mini_archive / "2026-11-06" / "184826" / "account_state.json"

    # somebody else fixes this round's peak by hand between appraisal and apply
    obj = json.loads(target.read_text(encoding="utf-8"))
    obj["peak_nav"] = 199480.90
    target.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")

    before = _tree_fingerprint(mini_archive)
    with pytest.raises(repair.ConcurrentEditError) as excinfo:
        repair.apply_repairs(stale_report, mini_archive)
    assert "2026-11-06/184826/account_state.json:peak_nav" in str(excinfo.value)
    assert "appraised=208830.78" in str(excinfo.value)
    assert _tree_fingerprint(mini_archive) == before   # zero writes

    # ... and the CLI exit code is 2, still with zero writes and no manifest/backup
    monkeypatch.setattr(repair, "appraise", lambda root, peak: stale_report)
    assert repair.main(["--runs-root", str(mini_archive), "--apply"]) == 2
    err = capsys.readouterr().err
    assert "concurrent edit detected" in err
    assert "0 files written" in err
    assert _tree_fingerprint(mini_archive) == before
    assert not list(mini_archive.rglob("*.orig"))
    assert not list(mini_archive.rglob("nav_hwm_manifest.*.json"))


def test_missing_root_is_a_usage_error(tmp_path):
    assert repair.main(["--runs-root", str(tmp_path / "nope")]) == 2


# ---------------------------------------------------------------------------
# 5. NESTED `prior_state` (the gap left by the first repair pass)
#
# Every account_state also nests a copy of the PREVIOUS round's state. The first pass
# repaired only the top level, so the copies kept the phantom peak 208830.78 while the
# snapshots they came from now say 203049.96. `inspect_portfolio_drawdown` swaps to
# `prior_state` when `as_of_date == state["as_of_date"]` -- exactly the same-day re-run --
# so the stale copy is what the breaker would read next.
# ---------------------------------------------------------------------------
def test_nested_prior_state_repair_truth_table(mini_archive):
    """Per-field decision for the nested block: what is restated and what is never touched."""
    report = repair.appraise(mini_archive, None)
    by_dir = {entry["dir"]: entry for entry in report["runs"]}

    # 11-04: copy already agrees with the ATH and has no duplicate -> no nested change
    clean = by_dir["2026-11-04/203553"]
    assert clean["changes"] == {}
    assert "NESTED_PRIOR_STATE_STALE" not in clean["codes"]

    # 11-05: copy of 11-04 carries the phantom peak and a duplicated history
    stale = by_dir["2026-11-05/202418"]
    assert "NESTED_PRIOR_STATE_STALE" in stale["codes"]
    assert "NESTED_PRIOR_STATE_HISTORY_STALE" in stale["codes"]
    assert stale["prior_state"]["as_of_date"] == "2026-11-04"
    assert stale["prior_state"]["source_dir"] == "2026-11-04/203553"
    nested = stale["changes"]
    assert nested["prior_state.peak_nav"]["after"] == pytest.approx(REAL_ATH)
    assert nested["prior_state.nav_history_10d"]["after"] == pytest.approx(
        [REAL_ATH, 200076.78]
    )
    # the dd comes from the COPY's own current_nav, which is never rewritten
    assert nested["prior_state.drawdown_pct"]["after"] == pytest.approx(
        200076.78 / REAL_ATH - 1.0
    )
    assert stale["prior_state"]["stored"]["current_nav"] == pytest.approx(200076.78)
    # no top-level key may leak into the nested change set, and vice versa
    assert "prior_state.current_nav" not in nested
    assert "prior_state.circuit_breaker_tier" not in nested

    # 11-16: the production round -- phantom peak + duplicate-bearing history in the copy
    prod = by_dir["2026-11-16/124508"]
    assert "NESTED_PRIOR_STATE_STALE" in prod["codes"]
    assert "NESTED_PRIOR_STATE_HISTORY_STALE" in prod["codes"]


def test_nested_codes_are_reported_even_when_no_write_is_needed(mini_archive):
    """A finding must not depend on whether a write happens (report-only is the contract)."""
    run = mini_archive / "2026-11-04" / "203553"
    state = json.loads((run / "account_state.json").read_text(encoding="utf-8"))
    # stale NESTED values only; the top level is already correct
    state["prior_state"]["peak_nav"] = MIXED_NAV
    state["prior_state"]["nav_history_10d"] = [199935.46, 199935.46, MIXED_NAV]
    _write_json(run / "account_state.json", state)

    report = repair.appraise(mini_archive, None)
    entry = next(e for e in report["runs"] if e["dir"] == "2026-11-04/203553")
    assert "NESTED_PRIOR_STATE_STALE" in entry["codes"]
    assert "NESTED_PRIOR_STATE_HISTORY_STALE" in entry["codes"]
    assert entry["changes"]          # and it is proposed for repair
    assert set(entry["changes"]) <= {
        "prior_state.peak_nav", "prior_state.drawdown_pct", "prior_state.nav_history_10d"
    }


def test_nested_apply_removes_the_phantom_collapses_the_duplicate_and_keeps_current_nav(
    mini_archive, capsys
):
    """End to end on the nested block: 208830.78 gone, no adjacent dup, current_nav intact."""
    state_1116 = mini_archive / "2026-11-16" / "124508" / "account_state.json"
    before = json.loads(state_1116.read_text(encoding="utf-8"))
    assert before["prior_state"]["peak_nav"] == pytest.approx(MIXED_NAV)
    assert before["prior_state"]["nav_history_10d"][0] == before["prior_state"]["nav_history_10d"][1]

    assert repair.main(["--runs-root", str(mini_archive), "--apply"]) == 0
    capsys.readouterr()

    after = json.loads(state_1116.read_text(encoding="utf-8"))
    prior = after["prior_state"]
    assert prior["peak_nav"] == pytest.approx(REAL_ATH)
    assert MIXED_NAV not in prior["nav_history_10d"]
    values = prior["nav_history_10d"]
    assert all(values[i] != values[i - 1] for i in range(1, len(values)))
    # dd is derived from the copy's own NAV: 196611.34 / 203049.96 - 1
    assert prior["drawdown_pct"] == pytest.approx(196611.34 / REAL_ATH - 1.0)
    # ... and that NAV, the counters, the tier and unrelated keys are untouched
    assert prior["current_nav"] == pytest.approx(196611.34)
    assert prior["current_nav"] == before["prior_state"]["current_nav"]
    assert prior["tier1_consecutive_bars"] == before["prior_state"]["tier1_consecutive_bars"]
    assert prior["circuit_breaker_tier"] == before["prior_state"]["circuit_breaker_tier"]
    assert prior["entry_prices"] == before["prior_state"]["entry_prices"]
    assert prior["as_of_date"] == before["prior_state"]["as_of_date"]


def test_nested_prior_state_matches_the_snapshot_it_was_copied_from(mini_archive, capsys):
    """Cross-file consistency: the copy's peak equals the source round's top-level peak."""
    assert repair.main(["--runs-root", str(mini_archive), "--apply"]) == 0
    capsys.readouterr()

    for rel, source in (
        ("2026-11-05/202418", "2026-11-04/203553"),
        ("2026-11-06/184826", "2026-11-05/202418"),
        ("2026-11-16/124508", "2026-11-06/184826"),
    ):
        nested = json.loads((mini_archive / rel / "account_state.json").read_text())["prior_state"]
        source_state = json.loads((mini_archive / source / "account_state.json").read_text())
        assert nested["peak_nav"] == pytest.approx(source_state["peak_nav"]), rel
        assert nested["nav_history_10d"] == pytest.approx(source_state["nav_history_10d"]), rel

    report = repair.appraise(mini_archive, None)
    assert report["prior_state_source_mismatches"] == []


def test_prior_state_source_mismatch_is_reported_not_resolved(mini_archive, capsys):
    """A copy that disagrees with its source AFTER the repair is reported, never rewritten."""
    # make the SOURCE round's top-level peak disagree with the verified mark, and leave it
    # out of the repair set by making it already "repaired" to a different value
    source = mini_archive / "2026-11-04" / "203553"
    state = json.loads((source / "account_state.json").read_text(encoding="utf-8"))
    state["peak_nav"] = 201000.00          # neither the phantom nor the ATH
    state["drawdown_pct"] = 200076.78 / 201000.00 - 1.0
    _write_json(source / "account_state.json", state)

    report = repair.appraise(mini_archive, None)
    # the tool proposes to restore the source too, so no mismatch is left behind ...
    entry = next(e for e in report["runs"] if e["dir"] == "2026-11-05/202418")
    assert "PRIOR_STATE_SOURCE_MISMATCH" not in entry["codes"]

    # ... but if the copy is pinned to a peak that no write can reconcile, it is reported
    broken = mini_archive / "2026-11-06" / "184826" / "account_state.json"
    obj = json.loads(broken.read_text(encoding="utf-8"))
    obj["prior_state"]["as_of_date"] = "2026-11-03"   # a date with no round under the root
    _write_json(broken, obj)

    report = repair.appraise(mini_archive, None)
    entry = next(e for e in report["runs"] if e["dir"] == "2026-11-06/184826")
    assert "PRIOR_STATE_SOURCE_MISSING" in entry["codes"]
    assert "prior_state.peak_nav" not in entry["changes"]  # nothing to derive it from


def test_concurrent_edit_inside_the_nested_block_also_aborts(mini_archive, monkeypatch, capsys):
    """The abort rail covers nested writes: a copied-block edit is a concurrent edit too."""
    stale_report = repair.appraise(mini_archive, None)
    target = mini_archive / "2026-11-06" / "184826" / "account_state.json"
    obj = json.loads(target.read_text(encoding="utf-8"))
    obj["prior_state"]["peak_nav"] = 199480.90      # hand-fixed nested peak
    _write_json(target, obj)

    before = _tree_fingerprint(mini_archive)
    with pytest.raises(repair.ConcurrentEditError) as excinfo:
        repair.apply_repairs(stale_report, mini_archive)
    assert "prior_state.peak_nav" in str(excinfo.value)
    assert _tree_fingerprint(mini_archive) == before

    monkeypatch.setattr(repair, "appraise", lambda root, peak: stale_report)
    assert repair.main(["--runs-root", str(mini_archive), "--apply"]) == 2
    assert "concurrent edit detected" in capsys.readouterr().err
    assert _tree_fingerprint(mini_archive) == before


def test_same_day_swap_regression_reads_the_repaired_prior_state(mini_archive, capsys):
    """THE PRODUCTION REGRESSION: the same-day swap must read the repaired copy.

    `inspect_portfolio_drawdown` replaces `state` with `prior_state` when
    `as_of_date == state["as_of_date"]` -- the re-run path. With the phantom peak in the
    copy the reported drawdown was the stale -0.0585-class number; after the nested repair
    it must be the true -0.0308 (196789.34 / 203049.96 - 1).
    """
    health = _load_health_module()
    state_path = mini_archive / "2026-11-16" / "124508" / "account_state.json"
    state_before = json.loads(state_path.read_text(encoding="utf-8"))

    stale = health.inspect_portfolio_drawdown(196789.34, None, dict(state_before), {},
                                              as_of_date="2026-11-16")
    assert stale["drawdown_pct"] == pytest.approx(196789.34 / MIXED_NAV - 1.0)
    assert stale["drawdown_pct"] < -0.05            # the phantom class of number

    assert repair.main(["--runs-root", str(mini_archive), "--apply"]) == 0
    capsys.readouterr()

    state_after = json.loads(state_path.read_text(encoding="utf-8"))
    fresh = health.inspect_portfolio_drawdown(196789.34, None, dict(state_after), {},
                                              as_of_date="2026-11-16")
    assert fresh["drawdown_pct"] == pytest.approx(-0.030833, abs=1e-5)
    assert fresh["peak_nav"] == pytest.approx(REAL_ATH)
    # the copies' histories are no longer duplicate-laden either
    assert fresh["nav_10d_status"] == "INSUFFICIENT_HISTORY"  # short fixture history



def test_clean_archive_exits_zero(mini_archive, capsys):
    """After the repair the archive agrees with its own legs -> 0, no findings."""
    assert repair.main(["--runs-root", str(mini_archive), "--apply"]) == 0
    capsys.readouterr()
    assert repair.main(["--runs-root", str(mini_archive)]) == 0
    out = capsys.readouterr().out
    assert "0 = nothing to change" in out


# ---------------------------------------------------------------------------
# 5. the manifest is an append-only ledger across passes (1.2.0)
# ---------------------------------------------------------------------------
def test_manifest_appends_each_pass_and_a_noop_run_leaves_it_byte_identical(
    mini_archive, monkeypatch, capsys
):
    """Two successive rewrites -> 2 passes with both file counts; a 3rd run is a no-op.

    WHY: the production day ran twice -- a top-level pass, then a ``prior_state`` pass --
    and 1.1.0's last-write-wins manifest lost the first pass's record (its ``.orig``
    backups survived, the ledger did not). The two passes are simulated the way they
    really differed: pass 1 has no nested logic at all
    (``propose_prior_state`` -> ``None``) and skips one round, pass 2 has both.
    """
    real_propose_for = repair.propose_for
    real_propose_prior_state = repair.propose_prior_state

    def _top_level_only(fact, **kwargs):
        if fact.rel == "2026-11-05/202418":     # the round pass 1 could not appraise
            return None
        return real_propose_for(fact, **kwargs)

    # --- pass 1: top level only, one round skipped -------------------------------
    monkeypatch.setattr(repair, "propose_for", _top_level_only)
    monkeypatch.setattr(repair, "propose_prior_state", lambda *a, **k: None)
    assert repair.main(["--runs-root", str(mini_archive), "--apply"]) == 0
    capsys.readouterr()

    manifests = sorted(mini_archive.glob("nav_hwm_manifest.*.json"))
    assert len(manifests) == 1
    witness = manifests[0]
    first = json.loads(witness.read_text(encoding="utf-8"))
    assert first["pass_count"] == 1
    assert len(first["passes"]) == 1
    assert first["passes"][0]["file_count"] == 2          # 11-06, 11-16 (11-05 skipped)
    assert first["passes"][0]["nested_rewritten"] == 0    # no nested logic in pass 1
    assert {f["path"] for f in first["passes"][0]["files"]} == {
        "2026-11-06/184826/account_state.json",
        "2026-11-16/124508/account_state.json",
    }
    assert all(
        not f["nested_fields_changed"] for f in first["passes"][0]["files"]
    )
    # the newest pass is still mirrored at the top level (pre-1.2.0 reader shape)
    assert first["rewritten"] == first["passes"][-1]["file_count"] == 2
    assert first["nested_rewritten"] == first["passes"][-1]["nested_rewritten"]

    # --- pass 2: the nested repair (the real second pass) ------------------------
    monkeypatch.setattr(repair, "propose_for", real_propose_for)
    monkeypatch.setattr(repair, "propose_prior_state", real_propose_prior_state)
    assert repair.main(["--runs-root", str(mini_archive), "--apply"]) == 0
    capsys.readouterr()

    ledger = json.loads(witness.read_text(encoding="utf-8"))
    assert ledger["pass_count"] == 2
    assert [p["file_count"] for p in ledger["passes"]] == [2, 3]   # 11-05 back in pass 2
    assert [p["nested_rewritten"] for p in ledger["passes"]] == [0, 3]
    assert {f["path"] for f in ledger["passes"][1]["files"]} == {
        "2026-11-05/202418/account_state.json",
        "2026-11-06/184826/account_state.json",
        "2026-11-16/124508/account_state.json",
    }
    # ... and pass 1's record survived verbatim
    assert ledger["passes"][0] == first["passes"][0]
    # REQUIRED KEYS of a pass entry; nothing invented beyond them
    assert {"recorded_at", "script_version", "file_count", "nested_rewritten", "files"} <= set(
        ledger["passes"][1]
    )
    assert ledger["passes"][1]["script_version"] == repair.SCRIPT_VERSION
    # the top level mirrors the NEWEST pass, not the first
    assert ledger["rewritten"] == 3
    assert ledger["files"] == ledger["passes"][-1]["files"]

    # --- pass 3: nothing to do -> byte-identical ledger, whole tree untouched ----
    before = _tree_fingerprint(mini_archive)
    manifest_raw = witness.read_bytes()
    assert repair.main(["--runs-root", str(mini_archive), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "0 = nothing to change" in out
    assert witness.read_bytes() == manifest_raw
    assert _tree_fingerprint(mini_archive) == before


def test_merge_manifest_upgrades_a_legacy_single_pass_ledger():
    """A manifest written before 1.2.0 keeps working: its top level becomes pass 1.

    The upgrade must be loss-free in both directions -- every pre-1.2.0 key stays at the
    top level (now describing the newest pass) and the old pass is preserved with exactly
    the values it recorded (``generated_at_utc`` -> ``recorded_at``,
    ``rewritten`` -> ``file_count``), so the 2026-11-16 backfill is reproducible.
    """
    legacy = {
        "generator": "scripts/repair_live_nav_hwm.py",
        "script_version": "1.1.0",
        "generated_at_utc": "2026-11-16T22:35:51+00:00",
        "peak_warnings": [],
        "rewritten": 22,
        "nested_rewritten": 22,
        "files": [{"path": "2026-11-16/142959/account_state.json", "md5_before": "a"}],
        "flagged_but_unchanged": [],
    }
    new_pass = {
        "script_version": "1.2.0",
        "generated_at_utc": "2026-11-16T23:00:00+00:00",
        "peak_warnings": ["kept: this run's own warnings"],
        "rewritten": 1,
        "nested_rewritten": 0,
        "files": [{"path": "2026-11-16/124508/account_state.json"}],
    }

    merged = repair.merge_manifest(legacy, new_pass)

    assert merged["pass_count"] == 2
    assert [p["file_count"] for p in merged["passes"]] == [22, 1]
    assert merged["passes"][0]["recorded_at"] == "2026-11-16T22:35:51+00:00"
    assert merged["passes"][0]["script_version"] == "1.1.0"
    assert merged["passes"][0]["files"] == legacy["files"]
    # top level = the NEW pass, so a pre-1.2.0 reader still sees a coherent document
    assert merged["rewritten"] == 1
    assert merged["script_version"] == "1.2.0"
    assert merged["files"] == new_pass["files"]
    assert merged["peak_warnings"] == new_pass["peak_warnings"]


@pytest.mark.parametrize(
    "payload,label",
    [
        (b'{"passes": [{"recorded_at": "2026-11-16T22:35:51+00:00"}', "truncated_json"),
        (b"[1, 2, 3]", "json_array_not_object"),
    ],
)
def test_corrupt_manifest_aborts_before_the_first_write(
    mini_archive, capsys, payload, label
):
    """NIT 8: an unreadable ledger stops a PENDING repair with 0 files written.

    WHY: the manifest is the only record of what an earlier pass of the day changed, and a
    ``.orig`` md5 cannot be re-derived once the byte copy is gone. ``load_manifest`` is
    therefore called BEFORE ``apply_repairs``, and this is the first test that combines a
    genuinely pending repair with a corrupt ledger -- so the abort path is exercised end to
    end through the CLI (exit 2, empty archive), not just as a unit call.

    Both ``ManifestError`` branches are covered: a parse failure and a document that parses
    but is not an object (a ledger replacement written by hand, or by another tool).
    """
    # a real repair IS pending: without the corrupt ledger this run would rewrite files
    assert repair.appraise(mini_archive, None)["targets"]
    target = repair.manifest_path(mini_archive)
    target.write_bytes(payload)
    before = _tree_fingerprint(mini_archive)

    code = repair.main(["--runs-root", str(mini_archive), "--apply"])

    assert code == 2, label
    err = capsys.readouterr().err
    assert str(target) in err, label                 # the error names the manifest PATH
    assert "refusing to overwrite" in err, label
    # NOTHING was written: no account_state, no backup, no temp file, no ledger change
    assert _tree_fingerprint(mini_archive) == before, label
    assert target.read_bytes() == payload, label
    assert not list(mini_archive.rglob("*.orig")), label
    assert not list(mini_archive.rglob("*.tmp-navhwm")), label
    # ... and the archive's phantom peak is still on disk (the repair never ran)
    state = json.loads(
        (mini_archive / "2026-11-05" / "202418" / "account_state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["peak_nav"] == pytest.approx(MIXED_NAV)


@pytest.mark.parametrize("payload", [b"{not json", b"[]"])
def test_corrupt_manifest_with_no_pending_changes_is_never_replaced(
    mini_archive, capsys, payload
):
    """The same corrupt ledger without pending changes must not be silently replaced.

    With nothing to merge, ``main`` never opens the ledger at all -- so the guarantee here
    is byte-identity of the whole tree (the file is left exactly as found, and the run
    reports ``[clean]``) rather than an exit 2: there is no earlier pass to merge into and
    no write that could lose one. Whatever the ledger contains is preserved for an operator
    to inspect.
    """
    # repair once so the archive is clean AND a real ledger exists
    assert repair.main(["--runs-root", str(mini_archive), "--apply"]) == 0
    capsys.readouterr()
    assert repair.appraise(mini_archive, None)["targets"] == []

    target = repair.manifest_path(mini_archive)
    assert target.exists()
    target.write_bytes(payload)
    before = _tree_fingerprint(mini_archive)
    origs_before = sorted(mini_archive.rglob("*.orig"))

    assert repair.main(["--runs-root", str(mini_archive), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "0 = nothing to change" in out
    assert target.read_bytes() == payload            # ledger NOT replaced
    assert _tree_fingerprint(mini_archive) == before  # nothing anywhere was touched
    assert sorted(mini_archive.rglob("*.orig")) == origs_before


# ---------------------------------------------------------------------------
# 5b. The WRITE path is transactional, scoped and loud (review findings)
#
# `ConcurrentEditError` (section 4) is the CLEAN abort: it happens before the first write.
# These three tests are about the other failure shapes, each of which used to end in a
# partial repair that nobody recorded:
#
#   * a generic `OSError` on a later file (disk full, permissions) propagated out of
#     `main`: earlier files rewritten, the failing file keeping an orphaned `.orig`, a
#     possible `*.tmp-navhwm`, and NO manifest -- so the partial pass was invisible;
#   * the byte-level write scope was ASSERTED (`fields_changed` says three/six fields) and
#     never CHECKED: a serializer that touched an untouched field would rewrite it anyway;
#   * `merge_manifest` silently DROPPED a malformed `passes` entry while overwriting the
#     ledger that contained it.
# ---------------------------------------------------------------------------
def _applied_peaks(root: Path) -> dict:
    return {
        rel: json.loads((root / rel / "account_state.json").read_text(encoding="utf-8"))[
            "peak_nav"
        ]
        for rel in ("2026-11-05/202418", "2026-11-06/184826", "2026-11-16/124508")
    }


def test_oserror_mid_write_reports_the_partial_pass_and_leaves_no_temp_file(
    mini_archive, monkeypatch, capsys
):
    """FINDING (MEDIUM): an I/O error on file 2 of 3 must not be a silent partial pass.

    Injecting `OSError` on the SECOND target (11-06, after 11-05 was rewritten) must:
    report exactly which files were rewritten and which failed, exit 2 (distinct from a
    clean 0), leave a manifest pass marked `incomplete: true` listing only the rewritten
    file, and leave no `*.tmp-navhwm` behind (the temp-file leak in `write_bytes_atomic`).
    """
    real_write = repair.write_bytes_atomic
    failing = mini_archive / "2026-11-06" / "184826" / "account_state.json"

    def _flaky(path, data):
        if Path(path) == failing:
            raise OSError(28, "No space left on device")
        return real_write(path, data)

    monkeypatch.setattr(repair, "write_bytes_atomic", _flaky)

    code = repair.main(["--runs-root", str(mini_archive), "--apply"])
    captured = capsys.readouterr()

    assert code == 2, (code, captured.out, captured.err)
    assert "No space left on device" in captured.err
    assert "were already rewritten before this abort" in captured.err or (
        "rewritten BEFORE the abort" in captured.err
    )
    # which files: the first one is named as rewritten, the second as failed
    assert "2026-11-05/202418/account_state.json" in captured.err
    assert "failed: 2026-11-06/184826/account_state.json" in captured.err

    peaks = _applied_peaks(mini_archive)
    assert peaks["2026-11-05/202418"] == pytest.approx(REAL_ATH)   # rewritten
    assert peaks["2026-11-06/184826"] == pytest.approx(MIXED_NAV)  # untouched: still phantom
    assert peaks["2026-11-16/124508"] == pytest.approx(MIXED_NAV)  # never reached

    # the ledger cannot go silent about a partial pass
    ledger = json.loads(repair.manifest_path(mini_archive).read_text(encoding="utf-8"))
    assert ledger["incomplete"] is True
    assert ledger["rewritten"] == 1
    assert [f["path"] for f in ledger["files"]] == ["2026-11-05/202418/account_state.json"]
    assert ledger["failed_files"][0]["path"] == "2026-11-06/184826/account_state.json"
    # the orphaned `.orig` written before the live write failed is REPORTED, not hidden
    assert ledger["failed_files"][0]["backup"].endswith("account_state.json.orig")
    assert (mini_archive / "2026-11-06" / "184826" / "account_state.json.orig").exists()
    # the newest pass mirrors the incomplete state too (single-pass readers see it)
    assert ledger["passes"][-1]["incomplete"] is True
    assert ledger["passes"][-1]["failed_files"] == ledger["failed_files"]

    # ... and no half-written temp file is left anywhere (including the backup write)
    assert not list(mini_archive.rglob("*.tmp-navhwm"))


def test_write_bytes_atomic_unlinks_its_own_temp_file_on_failure(tmp_path, monkeypatch):
    """FINDING (MEDIUM, part b): a failed atomic write leaves the directory as it found it."""
    target = tmp_path / "account_state.json"
    target.write_bytes(b"original")
    tmp = tmp_path / "account_state.json.tmp-navhwm"

    # the REPLACE failing (disk full / the destination turned into a directory) used to
    # leave `*.tmp-navhwm` beside the archive, which reads as a half-finished repair.
    def _boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(repair.os, "replace", _boom)
    with pytest.raises(OSError):
        repair.write_bytes_atomic(target, b"new")
    assert not tmp.exists()
    assert target.read_bytes() == b"original"


def test_serializer_scope_guard_aborts_a_file_it_would_have_corrupted(
    mini_archive, monkeypatch
):
    """FINDING (LOW): `fields_changed` is checked against the BYTES actually written.

    A serializer (or a field-path bug) that also moves a `NEVER_TOUCHED` field would still
    be reported as "six fields changed", so the manifest could not reveal it. The
    post-serialization diff aborts the file with a named error and writes nothing.
    """
    report = repair.appraise(mini_archive, None)
    assert report["targets"]
    before = _tree_fingerprint(mini_archive)
    real_write_field = repair.write_field

    # (a) a top-level NEVER_TOUCHED field moved by the write path
    def _sneaky_top_level(obj, field, value):
        real_write_field(obj, field, value)
        obj["current_nav"] = 0.0

    monkeypatch.setattr(repair, "write_field", _sneaky_top_level)
    with pytest.raises(repair.RepairScopeError) as excinfo:
        repair.apply_repairs(report, mini_archive)
    message = str(excinfo.value)
    assert "outside the repair scope" in message
    assert "current_nav" in message
    assert excinfo.value.applied == []                 # nothing was written
    assert _tree_fingerprint(mini_archive) == before

    # (b) the NESTED block's untouched counters (`prior_state.current_nav` etc. are what
    # the manifest promises never to touch -- a whole-block comparison would miss them)
    def _sneaky_nested(obj, field, value):
        real_write_field(obj, field, value)
        if isinstance(obj.get("prior_state"), dict):
            obj["prior_state"]["tier1_consecutive_bars"] = 99

    monkeypatch.setattr(repair, "write_field", _sneaky_nested)
    with pytest.raises(repair.RepairScopeError) as excinfo:
        repair.apply_repairs(report, mini_archive)
    assert "prior_state.tier1_consecutive_bars" in str(excinfo.value)
    assert _tree_fingerprint(mini_archive) == before


def test_scope_guard_does_not_false_positive_on_a_nan_already_in_an_untouched_field(
    mini_archive, capsys
):
    """A `NaN` that was ALREADY on disk is not "a field the serializer moved".

    `float('nan') != float('nan')`, so a naive diff would abort a perfectly good repair
    because a broken mark inside `lifecycle_states` reads back as "changed". The guard
    compares JSON values (NaN == NaN) precisely so it cannot block a legitimate write.
    """
    run = mini_archive / "2026-11-16" / "124508"
    state = json.loads((run / "account_state.json").read_text(encoding="utf-8"))
    state["lifecycle_states"] = {
        "600900.SH": {"state": "HELD_ACTIVE", "current_weight": float("nan")}
    }
    _write_json(run / "account_state.json", state)

    assert repair.main(["--runs-root", str(mini_archive), "--apply"]) == 0
    capsys.readouterr()

    after = json.loads((run / "account_state.json").read_text(encoding="utf-8"))
    assert after["peak_nav"] == pytest.approx(REAL_ATH)          # the repair DID happen
    assert math.isnan(after["lifecycle_states"]["600900.SH"]["current_weight"])


def test_merge_manifest_refuses_a_malformed_pass_entry_instead_of_dropping_it(
    mini_archive, capsys
):
    """FINDING (LOW): a junk `passes` entry is a `ManifestError`, not a silent deletion.

    1.2.0 filtered non-mappings out (`[dict(e) for e in recorded if isinstance(e, Mapping)]`)
    and still rewrote the file, so the ledger claimed a shorter history than it was given.
    The abort must happen BEFORE the first write, exactly like an unparsable ledger.
    """
    # a direct call refuses too (the rule is in the merge, not only in the CLI)
    with pytest.raises(repair.ManifestError):
        repair.merge_manifest({"passes": ["junk"], "rewritten": 1}, {"rewritten": 0})
    with pytest.raises(repair.ManifestError):
        repair.merge_manifest({"passes": "nope"}, {"rewritten": 0})

    target = repair.manifest_path(mini_archive)
    corrupt = {
        "generator": "scripts/repair_live_nav_hwm.py",
        "script_version": "1.2.0",
        "generated_at_utc": "2026-11-16T22:35:51+00:00",
        "rewritten": 1,
        "files": [{"path": "2026-11-16/142959/account_state.json"}],
        "passes": [
            {"recorded_at": "2026-11-16T22:35:51+00:00", "script_version": "1.2.0",
             "file_count": 1, "nested_rewritten": 0, "files": []},
            "a pass record that is not an object",
        ],
    }
    target.write_bytes(json.dumps(corrupt, indent=2).encode("utf-8"))
    before = _tree_fingerprint(mini_archive)

    assert repair.main(["--runs-root", str(mini_archive), "--apply"]) == 2
    err = capsys.readouterr().err
    assert "passes[1]" in err and "refusing to merge" in err
    assert _tree_fingerprint(mini_archive) == before      # 0 files, ledger untouched
    assert not list(mini_archive.rglob("*.orig"))
    assert not list(mini_archive.rglob("*.tmp-navhwm"))


# ---------------------------------------------------------------------------
# 6. NV13 -- PEAK PROVENANCE (a label, not a gate)
#
# THE DEFECT THIS SECTION IS WRITTEN AGAINST (measured, not re-diagnosed):
# `NV1` repaired the DATA (21 top-level + 22 nested `prior_state` files), but nothing in
# the ENGINE stops a new round from copying the previous round's stale `peak_nav`
# forward. Proof: `2026-11-16/142959` landed *during* the second repair pass and inherited
# the phantom 208830.78 again; it reads 203049.96 today only because the source files had
# been repaired first. "Today is clean" is a data fact, not an engine guarantee.
#
# WHY THE PROVENANCE RULE IS NOT "peak <= max leg-verified NAV":
# the true ATH 203049.96 (2026-09-29) predates the run tree entirely -- the highest leg
# NAV the tree can re-add is 202483.94 (2026-11-03). A strict rule would therefore reject
# a LEGITIMATE peak by 0.28%, while the phantom sits 3.13% above the same maximum. The
# tolerance separates today's two cases and is labelled as a separation device, not proof.
# ---------------------------------------------------------------------------
MAX_LEG_NAV = 202483.94        # 2026-11-03, the highest leg-verified NAV in the tree
_LOWER_LEG_NAV = 195000.00     # comfortably below the peak: an "above every leg" case


def test_peak_provenance_truth_table_on_the_three_real_numbers():
    """The four labels, on the numbers the incident actually produced."""
    legs = [200076.78, MAX_LEG_NAV, 196789.34]

    label, detail = peak_provenance(REAL_ATH, legs)
    assert label == PEAK_PROVENANCE_LEG_VERIFIED
    assert "202,483.94" in detail and "+0.28%" in detail
    # the legitimate ATH is INSIDE the tolerance band -> not an accusation
    assert peak_provenance(REAL_ATH, [MAX_LEG_NAV])[0] == PEAK_PROVENANCE_LEG_VERIFIED

    label, detail = peak_provenance(MIXED_NAV, legs)
    assert label == PEAK_PROVENANCE_UNVERIFIED_INHERITED
    assert "202,483.94" in detail and "3.13%" in detail and "prior_state" in detail

    # The archive cannot see the true ATH, so an operator declaration is the ONLY way a
    # above-every-leg peak can be attributable rather than inherited.
    label, detail = peak_provenance(MIXED_NAV, legs, declared_external=True)
    assert label == PEAK_PROVENANCE_ATTRIBUTABLE_EXTERNAL
    assert "3.13%" in detail and "--peak-nav" in detail
    # ... and a declaration never overturns leg evidence: inside the band it is
    # `leg_verified`, because the archive already backs that peak.
    assert peak_provenance(REAL_ATH, legs, declared_external=True)[0] == (
        PEAK_PROVENANCE_LEG_VERIFIED
    )

    # equality counts as verified, and so does a peak BELOW the highest leg NAV
    assert peak_provenance(MAX_LEG_NAV, legs)[0] == PEAK_PROVENANCE_LEG_VERIFIED
    assert peak_provenance(190000.0, legs)[0] == PEAK_PROVENANCE_LEG_VERIFIED

    # `missing` is "no usable peak at all" -- nothing is being divided by, so it stays
    # silent and is never a refusal:
    for unusable in (None, "", "n/a", float("nan"), float("inf"), 0.0, -1.0):
        label, detail = peak_provenance(unusable, legs)
        assert label == PEAK_PROVENANCE_MISSING, unusable
        assert "no usable peak_nav" in detail
    # ... while a peak that IS present but has NOTHING to compare against is `unverifiable`
    # (review finding, HIGH): the run could not check it, and saying "missing" made the
    # opt-in `--require-provable-peak` gate pass exactly where it cannot verify anything.
    for no_legs in (None, [], [float("nan")], [0.0, -5.0]):
        label, detail = peak_provenance(REAL_ATH, no_legs)
        assert label == PEAK_PROVENANCE_UNVERIFIABLE, no_legs
        assert "no leg-verified NAV" in detail
        assert "could NOT check" in detail
        # a declaration cannot manufacture evidence out of nothing
        assert peak_provenance(REAL_ATH, no_legs, declared_external=True)[0] == (
            PEAK_PROVENANCE_UNVERIFIABLE
        )

    assert set(PEAK_PROVENANCE_LABELS) == {
        PEAK_PROVENANCE_LEG_VERIFIED,
        PEAK_PROVENANCE_ATTRIBUTABLE_EXTERNAL,
        PEAK_PROVENANCE_UNVERIFIED_INHERITED,
        PEAK_PROVENANCE_UNVERIFIABLE,
        PEAK_PROVENANCE_MISSING,
    }
    assert len(PEAK_PROVENANCE_LABELS) == len(set(PEAK_PROVENANCE_LABELS)) == 5
    assert 0.0 < PEAK_PROVENANCE_TOL < (MIXED_NAV / MAX_LEG_NAV - 1.0)


def test_peak_provenance_refuses_a_bogus_tolerance():
    """A tolerance nobody validated must not silently produce a label."""
    for bad in (None, float("nan"), float("inf"), -0.001):
        with pytest.raises(ValueError):
            peak_provenance(REAL_ATH, [MAX_LEG_NAV], tol=bad)


def test_max_leg_verified_nav_ignores_unusable_rows():
    assert max_leg_verified_nav([MAX_LEG_NAV, REAL_ATH, 196789.34]) == pytest.approx(REAL_ATH)
    assert max_leg_verified_nav([float("nan"), 0.0, -1.0, 190000.0]) == pytest.approx(190000.0)
    assert max_leg_verified_nav(None) is None
    assert max_leg_verified_nav([]) is None


#: The three additive keys NV13 adds to `inspect_portfolio_drawdown`'s result.
_PROVENANCE_KEYS = ("peak_provenance", "peak_provenance_detail", "max_leg_verified_nav")


def test_every_peak_provenance_label_leaves_the_decision_dict_bit_identical():
    """The rule-9-safe proof: the label is INFORMATIONAL, never a decision input.

    The four labels `inspect_portfolio_drawdown` can actually reach are produced from the
    SAME (current_nav, inherited peak, params) -- only the leg series and the operator's
    declaration differ, and neither is echoed into the dict. (`missing` is not reachable
    here: this function always has a positive peak to divide by -- `unverifiable`, review
    finding HIGH, is what a peak-with-no-leg-series now reports.) So if any label could
    reach `peak_nav`, `drawdown_pct`, the tier ladder, the counters or the scale, the
    stripped dicts below would diverge. They must be identical to each other AND to a call
    that passes no leg evidence at all (today's behaviour).
    """
    health = _load_health_module()
    current_nav, peak, params = 178000.0, REAL_ATH, {}
    base_state = {"peak_nav": peak, "nav_history_10d": _ten_distinct_history()}

    cases = {
        PEAK_PROVENANCE_LEG_VERIFIED: ([MAX_LEG_NAV], base_state),
        PEAK_PROVENANCE_ATTRIBUTABLE_EXTERNAL: (
            [_LOWER_LEG_NAV],
            dict(base_state, **{PEAK_NAV_OPERATOR_ATH_KEY: True}),
        ),
        PEAK_PROVENANCE_UNVERIFIED_INHERITED: ([_LOWER_LEG_NAV], base_state),
        PEAK_PROVENANCE_UNVERIFIABLE: (None, base_state),
    }

    stripped_by_label = {}
    for label, (legs, state) in cases.items():
        result = health.inspect_portfolio_drawdown(
            current_nav=current_nav,
            peak_nav=None,
            state=dict(state),
            params=params,
            leg_navs=legs,
        )
        assert result["peak_provenance"] == label
        assert result["max_leg_verified_nav"] == (
            None if legs is None else pytest.approx(max_leg_verified_nav(legs))
        )
        assert isinstance(result["peak_provenance_detail"], str) and (
            result["peak_provenance_detail"]
        )
        stripped_by_label[label] = {
            key: value for key, value in result.items() if key not in _PROVENANCE_KEYS
        }

    reference = stripped_by_label[PEAK_PROVENANCE_UNVERIFIABLE]
    for label, stripped in stripped_by_label.items():
        assert stripped == reference, f"{label} moved a decision field"

    # ... and the reference IS today's behaviour: the pre-NV13 call signature.
    legacy_signature = health.inspect_portfolio_drawdown(
        current_nav=current_nav,
        peak_nav=None,
        state=dict(base_state),
        params=params,
    )
    assert {k: v for k, v in legacy_signature.items() if k not in _PROVENANCE_KEYS} == (
        reference
    )

    # The absolute values are pinned too, so "identical" cannot mean "identically broken":
    # a 12.34% drawdown against the true ATH is TIER 1 damping with a fractional scale.
    assert reference["peak_nav"] == pytest.approx(REAL_ATH)
    assert reference["drawdown_pct"] == pytest.approx(current_nav / REAL_ATH - 1.0)
    assert reference["drawdown_mag"] == pytest.approx(abs(current_nav / REAL_ATH - 1.0))
    assert reference["tier"] == "TIER_1_DAMPING"
    assert reference["linear_equity_scale"] == pytest.approx(
        1.0 - (abs(current_nav / REAL_ATH - 1.0) - 0.10) / 0.10
    )
    assert reference["peak_nav_source"] == "prior_state"
    assert reference["tier1_counter"] == 1 and reference["tier3_counter"] == 0
    assert reference["hwm_guard_applied"] is False


def _ten_distinct_history(top: float = 100000.0) -> list:
    """Ten ADJACENTLY DISTINCT points -> the measured 10-day branch (no reason text)."""
    return [top - i * 100.0 for i in range(9, -1, -1)]


def test_cli_peak_is_the_declared_external_route_and_stays_attributable():
    """`--peak-nav` re-labels rather than silences: attributable, never leg_verified."""
    health = _load_health_module()
    result = health.inspect_portfolio_drawdown(
        current_nav=196789.34,
        peak_nav=MIXED_NAV,           # the operator declares the number explicitly
        state={"nav_history_10d": _ten_distinct_history()},
        params={},
        leg_navs=[MAX_LEG_NAV],
    )
    assert result["peak_nav_source"] == "cli"
    assert result["peak_provenance"] == PEAK_PROVENANCE_ATTRIBUTABLE_EXTERNAL
    assert result["max_leg_verified_nav"] == pytest.approx(MAX_LEG_NAV)


# ---------------------------------------------------------------------------
# 7. The OPT-IN fail-closed gate (`--require-provable-peak`, default OFF)
# ---------------------------------------------------------------------------
def _dd_with_provenance(label: str, *, peak: float, max_leg=None, source="prior_state"):
    """A minimal `dd_info` carrying a given label (the gate reads nothing else)."""
    return {
        "peak_nav": peak,
        "peak_nav_source": source,
        "peak_provenance": label,
        "peak_provenance_detail": f"detail for {label}",
        "max_leg_verified_nav": max_leg,
    }


def test_require_provable_peak_exits_2_on_the_phantom_and_0_on_a_leg_verified_peak(capsys):
    """The flag's whole contract: refuse the phantom, let a backed peak through."""
    health = _load_health_module()
    phantom = _dd_with_provenance(
        PEAK_PROVENANCE_UNVERIFIED_INHERITED, peak=MIXED_NAV, max_leg=MAX_LEG_NAV
    )
    backed = _dd_with_provenance(
        PEAK_PROVENANCE_LEG_VERIFIED, peak=REAL_ATH, max_leg=MAX_LEG_NAV
    )

    with pytest.raises(SystemExit) as excinfo:
        health.enforce_provable_peak(phantom, require_provable_peak=True)
    assert excinfo.value.code == health.PEAK_PROVENANCE_EXIT_CODE == 2

    out = capsys.readouterr().out
    assert "PROVABLE-PEAK GATE" in out
    # the three numbers an operator needs, and the documented escape hatch
    assert "208,830.78" in out
    assert "202,483.94" in out
    assert "0.50%" in out
    assert "--peak-nav" in out
    assert "退出码 2" in out

    # A leg-verified peak proceeds (no exception == exit 0 on the deployed path).
    assert health.enforce_provable_peak(backed, require_provable_peak=True) is None
    assert capsys.readouterr().out == ""

    # `missing` is NOT a refusal -- a run with NO usable peak at all is not carrying an
    # unprovable peak -- and a declared external ATH proceeds.
    for label in (PEAK_PROVENANCE_MISSING, PEAK_PROVENANCE_ATTRIBUTABLE_EXTERNAL):
        assert health.enforce_provable_peak(
            _dd_with_provenance(label, peak=REAL_ATH), require_provable_peak=True
        ) is None
    assert capsys.readouterr().out == ""


def test_unverifiable_peak_is_refused_while_missing_is_not(capsys):
    """FINDING (HIGH): the flag must not fail OPEN where it cannot verify.

    `unverifiable` = "a peak IS in use, but this run found no leg-verified series" -- the
    non-archive `--output-dir`, a failed archive scan, an empty tree. That case used to
    share the `missing` label, which the gate waves through, so `--require-provable-peak`
    passed silently in precisely the deployments that cannot verify anything. Now:

    * `unverifiable` + flag -> exit 2, with a message that says the run could NOT verify
      the peak (not that the peak is wrong) and how to supply the evidence;
    * `missing` + flag -> still exit 0: there is no peak to be unprovable about.
    """
    health = _load_health_module()

    unverifiable = _dd_with_provenance(
        PEAK_PROVENANCE_UNVERIFIABLE, peak=REAL_ATH, max_leg=None
    )
    with pytest.raises(SystemExit) as excinfo:
        health.enforce_provable_peak(unverifiable, require_provable_peak=True)
    assert excinfo.value.code == health.PEAK_PROVENANCE_EXIT_CODE == 2

    out = capsys.readouterr().out
    assert "PROVABLE-PEAK GATE" in out
    assert "无法校验" in out                     # "could not verify"
    assert "max_leg_verified_nav=N/A" in out
    assert "--peak-nav" in out and "--output-dir" in out
    assert "退出码 2" in out
    # It must NOT be the `unverified_inherited` refusal: the remedies differ.
    assert "没有可证出处" not in out
    assert "unverified_inherited" not in out

    # `missing`: no usable peak at all -> nothing to refuse.
    missing = _dd_with_provenance(PEAK_PROVENANCE_MISSING, peak=None, max_leg=None)
    assert health.enforce_provable_peak(missing, require_provable_peak=True) is None
    assert capsys.readouterr().out == ""

    # Default OFF: an unverifiable peak is a disclosure, never a refusal.
    assert health.enforce_provable_peak(unverifiable, require_provable_peak=False) is None
    assert capsys.readouterr().out == ""


def test_unverifiable_peak_prints_its_own_warn_line_explaining_it_could_not_verify(capsys):
    """The WARN fires for `unverifiable` too -- saying "could not verify", not "wrong"."""
    health = _load_health_module()
    warn = health.format_peak_provenance_warning(
        _dd_with_provenance(PEAK_PROVENANCE_UNVERIFIABLE, peak=REAL_ATH, max_leg=None)
    )
    assert warn is not None and warn.startswith("[WARN] ")
    assert "无法校验" in warn and "unverifiable" in warn
    assert "203,049.96" in warn                  # the peak under discussion
    assert "无法证明、也无法否证" in warn          # could neither verify nor refute
    assert "这不表示峰值有误" in warn              # explicitly NOT an accusation
    assert "--require-provable-peak" in warn
    # ... and it is not the phantom wording (that one asserts the peak has no provenance)
    assert "峰值无出处" not in warn

    # `missing` stays silent: there is no peak in play to disclose anything about.
    assert health.format_peak_provenance_warning(
        _dd_with_provenance(PEAK_PROVENANCE_MISSING, peak=None)
    ) is None


def test_flag_off_reproduces_todays_refusal_free_output_and_the_flag_parses_off_by_default(
    capsys, tmp_path
):
    """Default OFF = byte-identical behaviour, even on an unprovable peak."""
    health = _load_health_module()
    phantom = _dd_with_provenance(
        PEAK_PROVENANCE_UNVERIFIED_INHERITED, peak=MIXED_NAV, max_leg=MAX_LEG_NAV
    )
    assert health.enforce_provable_peak(phantom, require_provable_peak=False) is None
    assert capsys.readouterr().out == ""

    parsed_default = health.parse_args(
        ["--output-dir", str(tmp_path), "--data-provider", "synthetic"]
    )
    assert parsed_default.require_provable_peak is False
    parsed_on = health.parse_args(
        ["--output-dir", str(tmp_path), "--data-provider", "synthetic", "--require-provable-peak"]
    )
    assert parsed_on.require_provable_peak is True

    # The WARN line is NOT the gate: it fires on the phantom either way, and only there.
    assert health.format_peak_provenance_warning(phantom).startswith("[WARN] ")
    for label in (
        PEAK_PROVENANCE_LEG_VERIFIED,
        PEAK_PROVENANCE_ATTRIBUTABLE_EXTERNAL,
        PEAK_PROVENANCE_MISSING,
    ):
        assert health.format_peak_provenance_warning(
            _dd_with_provenance(label, peak=REAL_ATH)
        ) is None

    # argparse formats help with `% params`: a bare `%` in the flag's help used to swallow
    # the rest of the line and print the raw `Option` dicts instead of the tolerance.
    with pytest.raises(SystemExit) as helpexit:
        health.parse_args(["--help"])
    assert helpexit.value.code == 0
    help_out = capsys.readouterr().out
    assert "--require-provable-peak" in help_out
    assert "0.50%" in help_out
    assert "option_strings" not in help_out


# ---------------------------------------------------------------------------
# 8. The leg evidence base (read-only archive scan)
# ---------------------------------------------------------------------------
def _leg_archive(root: Path) -> Path:
    """Three rounds: two same-instant legs (max 202483.94) and one mixed-timebase round.

    Mirrors the real tree: the highest leg-verified NAV (202483.94, 2026-11-03) is BELOW
    the true ATH, and the 2026-11-05 phantom's legs are ineligible so its sum can never
    become the evidence maximum.
    """
    _round(
        root, "2026-11-03", "202418",
        equity=180000.00, cash=MAX_LEG_NAV - 180000.00,
        current_nav=MAX_LEG_NAV, peak_nav=MAX_LEG_NAV, drawdown_pct=0.0,
        nav_history=[MAX_LEG_NAV],
    )
    _round(
        root, "2026-11-04", "203553",
        equity=177000.00, cash=20000.00,          # 197000.00
        current_nav=197000.00, peak_nav=REAL_ATH, drawdown_pct=197000.00 / REAL_ATH - 1.0,
        nav_history=[MAX_LEG_NAV, 197000.00],
    )
    # 2026-11-05: cash struck on another day -> INELIGIBLE, and its legs sum ABOVE the
    # true ATH (186218.00 + 22612.78 = 208830.78). It must not enter the evidence base.
    _round(
        root, "2026-11-05", "202418b",
        equity=186218.00, cash=PREV_CASH,
        current_nav=MIXED_NAV, peak_nav=MIXED_NAV, drawdown_pct=0.0,
        nav_history=[MIXED_NAV], cash_as_of="2026-11-04",
    )
    return root


def test_collect_leg_verified_navs_reads_only_same_instant_legs(tmp_path):
    health = _load_health_module()
    root = _leg_archive(tmp_path / "runs")

    navs = health.collect_leg_verified_navs(str(root))

    assert len(navs) == 2  # the mixed-timebase round contributes nothing
    assert max(navs) == pytest.approx(MAX_LEG_NAV)
    assert MIXED_NAV not in navs
    # an ineligible round's legs must not be able to pull the evidence maximum UP
    assert max(navs) < REAL_ATH

    # point-in-time: a later as_of cannot be explained by an earlier... or later round
    assert health.collect_leg_verified_navs(str(root), as_of_date="2026-11-03") == pytest.approx(
        [MAX_LEG_NAV]
    )
    # no archive / unreadable tree -> no evidence, never an exception
    assert health.collect_leg_verified_navs(None) == []
    assert health.collect_leg_verified_navs(str(tmp_path / "does-not-exist")) == []

    # The peak the tree can back is 0.28% below the true ATH: `leg_verified`, not a phantom.
    assert peak_provenance(REAL_ATH, navs)[0] == PEAK_PROVENANCE_LEG_VERIFIED
    assert peak_provenance(MIXED_NAV, navs)[0] == PEAK_PROVENANCE_UNVERIFIED_INHERITED


def test_locate_run_archive_root_recognises_only_the_dated_layout(tmp_path):
    health = _load_health_module()
    root = tmp_path / "runs"
    run_dir = root / "2026-11-16" / "142959"
    run_dir.mkdir(parents=True)
    state_path = str(run_dir / "account_state.json")

    assert health.locate_run_archive_root(str(run_dir), state_path) == str(root)
    assert health.locate_run_archive_root(str(run_dir)) == str(root)

    # the default per-strategy output dir is NOT an archive: no evidence may be claimed
    flat = tmp_path / "live" / "chan_four_state_blend"
    flat.mkdir(parents=True)
    assert health.locate_run_archive_root(str(flat)) is None
    assert health.locate_run_archive_root(str(flat), str(flat / "account_state.json")) is None

    # a date-shaped instance directory is not enough: the day dir must be YYYY-MM-DD
    odd = tmp_path / "other" / "not-a-date" / "142959"
    odd.mkdir(parents=True)
    assert health.locate_run_archive_root(str(odd)) is None

    assert health.locate_run_archive_root(None) is None


def test_unprovable_peak_end_to_end_through_the_archive_scan(tmp_path, capsys):
    """END TO END: the same dict that triggers the flag, built from real archive files."""
    health = _load_health_module()
    root = _leg_archive(tmp_path / "runs")
    leg_navs = health.collect_leg_verified_navs(str(root))
    state = {"peak_nav": MIXED_NAV, "nav_history_10d": _ten_distinct_history()}

    phantom = health.inspect_portfolio_drawdown(
        current_nav=196789.34, peak_nav=None, state=dict(state), params={}, leg_navs=leg_navs
    )
    assert phantom["peak_provenance"] == PEAK_PROVENANCE_UNVERIFIED_INHERITED
    assert phantom["max_leg_verified_nav"] == pytest.approx(MAX_LEG_NAV)
    assert phantom["drawdown_pct"] == pytest.approx(196789.34 / MIXED_NAV - 1.0)

    with pytest.raises(SystemExit) as excinfo:
        health.enforce_provable_peak(phantom, require_provable_peak=True)
    assert excinfo.value.code == 2
    capsys.readouterr()

    # The SAME archive, the repaired peak: the run proceeds and nothing is printed.
    repaired = health.inspect_portfolio_drawdown(
        current_nav=196789.34,
        peak_nav=None,
        state={"peak_nav": REAL_ATH, "nav_history_10d": _ten_distinct_history()},
        params={},
        leg_navs=leg_navs,
    )
    assert repaired["peak_provenance"] == PEAK_PROVENANCE_LEG_VERIFIED
    assert repaired["drawdown_pct"] == pytest.approx(196789.34 / REAL_ATH - 1.0)
    assert health.enforce_provable_peak(repaired, require_provable_peak=True) is None
