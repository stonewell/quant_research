"""`LV12` -- BOTH stages must run on ONE NAV: the marked-to-market one.

FORENSIC CONTEXT
----------------
`LV1` moved the DAILY gate (`drawdown_pct` / `tier` / `tier1_consecutive_bars`) onto
`nav_breakdown.json::nav_effective`, but four consumers were left on the operator form value
(`--portfolio-value`), so one run could still publish two different NAVs:

===================================  ==========================================  ==========================
consumer                             used before LV12                            now
===================================  ==========================================  ==========================
Stage-1 5m intraday base             `args.portfolio_value` (form)               `gate_nav` (MTM)
(`hwm_intra` / `dd_trough`)          `check_live_portfolio_health.py` 5m block
CLI `--auto-run-stage2` argv         `str(args.portfolio_value)` (form)          threaded MTM NAV
(`--portfolio-value` for Stage 2)    `build_stage2_command`
===================================  ==========================================  ==========================

The archived 10-30 / 11-02 pair is the fact base: the form value was frozen at
``199935.46`` on BOTH days while the book marked ``191630.06``.  The daily gate was fixed by
LV1; the 5m trough was still measured against a NAV that never existed, and the auto-launched
Stage 2 sized its orders against that same frozen number.

WHAT THIS SUITE PINS
--------------------
1. The threaded MTM NAV reaches Stage 2's argv (and the default stays the form value, so no
   other caller changes).
2. The 5m `hwm_intra` / `dd_trough` derive from `nav_effective`, not from the form value.
3. `form == MTM` is a NO-OP: the 5m block is bit-identical to the pre-fix arithmetic.
4. A DEGRADED run (`nav_effective` unusable) still falls back to the form value and does NOT
   raise -- the LV1 placeholder fallback is preserved deliberately.
5. The form-vs-MTM difference is RECORDED (`intraday_5m_metrics.nav_base`) when it exceeds the
   frozen tolerance and ABSENT when the two agree -- a difference is never silently absorbed.
6/7. `distribution_alert` / `intraday_buy_scale` and the daily-gate fields
   (`tier` / `drawdown_pct` / `nav_mtm` / `nav_source`) are untouched by the base swap.

DELIBERATELY NOT CHANGED (out of scope, would be a REGRESSION)
------------------------------------------------------------
* `web/livetrading/execution_service.py` still passes the FORM value to the gate child: that is
  LV1's `nav_form` cross-check input (`nav_deviation_pct`), and feeding it MTM would zero the
  deviation signal the web path exists to report. The web Stage-2 path already threads MTM.
* `dd_trough` stays diagnostic-only; it is never promoted into a gate/tier input (R1/R5).

100% OFFLINE: synthetic provider, every input under `tmp_path`, `subprocess.run` always
monkeypatched, 5m frames hand-built. No network, no real `data/` tree, no archived run dir.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _load_script(module_name, relative_path):
    """Load a standalone script (not an importable package module) under a PRIVATE name.

    Same approach as `test_stage1_nav_prestaging.py`: importing it as `scripts.…` would make
    the module identity shared with every other suite that touches Stage 1.
    """
    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load_script("check_live_portfolio_health_lv12", os.path.join("scripts", "check_live_portfolio_health.py"))

#: A past US session, so `--as-of-date` is accepted in replay mode without freezing the clock.
AS_OF = "2026-04-10"

#: The archived fact pair (LV1 / LV12): the form value was frozen while the book marked away.
FORM_NAV = 199935.46
MTM_NAV = 191630.06

#: Explicit HWM BELOW the marked NAV: `inspect_portfolio_drawdown` then promotes the peak to
#: `current_nav`, so the intraday peak is `nav_base + 1000` and BOTH `hwm_intra` and
#: `dd_trough` are pure functions of the NAV base -- exactly what this suite needs to see.
PEAK_NAV = 190000.0
#: A realistic peak (the archived 203049.96) for the "daily gate is untouched" case.
REAL_PEAK_NAV = 203049.96

#: Holdings that exercise BOTH base-scaled branches of the 5m NAV loop:
#:   * `AAA`: 100 SHARES -> `shares * Close` (price path, no base),
#:   * `BBB`: 0.5 WEIGHT -> `weight * nav_base` (+ the `1 - 0.5` residual, also base-scaled).
#: So `bar_nav = 100 * close(AAA) + nav_base` on every bar.
AAA_SHARES = 100
AAA_CLOSES = (10.0, 9.0)
BBB_WEIGHT = 0.5
BAR_TIMES = ("09:35", "13:55")

_BAR_OFFSETS = (AAA_SHARES * AAA_CLOSES[0], AAA_SHARES * AAA_CLOSES[1])


class _MockProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------
def _write_inputs(tmp_path, tag, nav_effective, form_nav=FORM_NAV):
    """Throwaway universe / strategy / holdings / `nav_breakdown.json` under `tmp_path`."""
    run = tmp_path / tag
    run.mkdir(parents=True, exist_ok=True)

    universe = run / "universe.txt"
    universe.write_text("AAA\nBBB\n", encoding="utf-8")

    strategy = run / "strategy.json"
    strategy.write_text(json.dumps({"template_name": "lv12_unit_test", "params": {}}), encoding="utf-8")

    holdings = run / "holdings.json"
    holdings.write_text(
        json.dumps({"holdings": {"AAA": AAA_SHARES, "BBB": BBB_WEIGHT}}), encoding="utf-8"
    )

    breakdown = run / "nav_breakdown.json"
    breakdown.write_text(
        json.dumps({"as_of": AS_OF, "nav_effective": nav_effective}), encoding="utf-8"
    )

    return {
        "run": run,
        "out": str(run / "out"),
        "universe": str(universe),
        "strategy": str(strategy),
        "holdings": str(holdings),
        "breakdown": str(breakdown),
    }


def _five_minute_frames(symbols, start=None, end=None, data_provider=None, cutoff_time="14:00"):
    """Hand-built 5m frames indexed at `AS_OF`, INSIDE the 14:00 cutoff.

    `end` is the resolved `as_of_date` Stage 1 passes in, so the fake cannot drift from the
    run's own session.
    """
    assert end == AS_OF, f"stage 1 resolved an unexpected as_of_date: {end!r}"
    idx = pd.DatetimeIndex([pd.Timestamp(f"{AS_OF} {t}") for t in BAR_TIMES])
    frames = {}
    for sym in symbols:
        closes = list(AAA_CLOSES) if sym == "AAA" else [5.0, 5.0]
        frames[sym] = pd.DataFrame(
            {
                "Open": closes,
                "High": closes,
                "Low": closes,
                "Close": closes,
                "Volume": 1_000.0,
            },
            index=idx,
        )
    return frames


def _enrich(universe, universe_5m, config=None, synthesize_intraday_bar=False):
    """Attach MEASURED 5m features so the distribution gate is EVALUATED, not SKIPPED.

    The values deliberately trip the afternoon-distribution alert (breadth 0.0 < 25%,
    breakdown 100%), which makes `distribution_alert` / `intraday_buy_scale` a NON-TRIVIAL
    pair for the "independent of the NAV base" assertion (an alert, not a pass).
    """
    enriched = dict(universe)
    for sym in ("AAA", "BBB"):
        frame = enriched[sym].copy()
        frame["VWAP_Prem_5m"] = -0.01
        frame["UpVol_Ratio_5m"] = 0.30
        frame["Afternoon_Ret_5m"] = -0.02
        enriched[sym] = frame
    return enriched


def _patch_5m(monkeypatch, loader=None, enricher=None):
    """Patch the 5m loaders INSIDE `common.intraday_features`.

    Stage 1 imports them lazily inside `main()`, so patching the module attributes is the only
    hook that does not require a real 5m provider.

    (D7 follow-up) The 5m block stamps `intraday_5m_metrics.generated_at` with the wall clock,
    so two runs of IDENTICAL inputs differed by ~0.5 s and the "bit-identical" contract below
    could never hold. The stamp exists for the web layer's freshness check, not for the run's
    content, so the clock is PINNED here through the module's documented override hook: two
    runs then differ by nothing at all, and the assertion keeps its original meaning
    ("`form == MTM` => the 5m block is byte-identical") instead of being weakened to
    "identical except the timestamp".
    """
    import datetime as _dt

    import common.intraday_features as intraday_features

    monkeypatch.setattr(
        intraday_features, "load_intraday_5m_universe", loader or _five_minute_frames
    )
    monkeypatch.setattr(
        intraday_features,
        "enrich_universe_with_intraday_features",
        enricher or _enrich,
    )
    # The 5m block asks for a provider instance; nothing below it may touch a real cache.
    monkeypatch.setattr(_mod, "get_data_provider", lambda *a, **k: object())
    # Deterministic self-report stamp AND deterministic 5m publication state (see the
    # docstring above). 2026-04-11T00:05Z == 2026-04-10 20:05 ET: AFTER the 5m publication
    # hour (20:00), so a cache that stops on 04-09 is a genuinely STALE cache, not
    # "today's bars are not published yet" -- which is the state this fixture is about.
    monkeypatch.setattr(
        _mod,
        "INTRADAY_5M_NOW_OVERRIDE",
        _dt.datetime(2026, 4, 11, 0, 5, 0, tzinfo=_dt.timezone.utc),
    )


def _run(
    tmp_path,
    monkeypatch,
    tag,
    *,
    nav_effective,
    form_nav=FORM_NAV,
    peak_nav=PEAK_NAV,
    enable_5m=True,
    auto_stage2=False,
    loader=None,
    enricher=None,
):
    """One full Stage-1 `main()` invocation, hermetic, with the child launch captured."""
    paths = _write_inputs(tmp_path, tag, nav_effective, form_nav=form_nav)
    if enable_5m:
        _patch_5m(monkeypatch, loader=loader, enricher=enricher)

    captured = []

    def _run_child(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _MockProc()

    monkeypatch.setattr(subprocess, "run", _run_child)

    argv = [
        "--strategy-file", paths["strategy"],
        "--universe-file", paths["universe"],
        "--as-of-date", AS_OF,
        "--start-date", "2025-09-01",
        "--data-provider", "synthetic",
        "--output-dir", paths["out"],
        "--portfolio-value", repr(form_nav),
        "--peak-nav", repr(peak_nav),
        "--nav-breakdown-file", paths["breakdown"],
        "--current-holdings-file", paths["holdings"],
        "--no-save-state",
        # The throwaway strategy key is unregistered, so the weighting-mode pin cannot be
        # verified; the override is the documented exploration escape (irrelevant here -- no
        # blend parameter is read by this suite).
        "--allow-weighting-mode-override",
    ]
    if enable_5m:
        argv.append("--enable-intraday-5m")
    if auto_stage2:
        argv.append("--auto-run-stage2")

    result = _mod.main(argv)
    report = json.loads(
        (open(os.path.join(paths["out"], _mod.STAGE1_HEALTH_REPORT_JSON), encoding="utf-8")).read()
    )
    return {
        "result": result,
        "report": report,
        "captured": captured,
        "intraday": result["intraday_5m_info"],
        "reported_intraday": report.get("intraday_5m_metrics"),
        "dd": result["dd_info"],
        "out_dir": paths["out"],
    }


def _flag(cmd, flag):
    assert flag in cmd, f"{flag} missing from argv: {cmd}"
    return cmd[cmd.index(flag) + 1]


def _old_formula_trough(base):
    """The PRE-FIX arithmetic, re-derived independently of the production code.

    Operand ORDER is preserved on purpose: the `form == MTM` contract is *bit-identical*, and
    `(1000 + 0.5b) + 0.5b` is not bit-identical to `b + 1000` for every `b`. Cheap honesty,
    not pedantry -- a reordered expectation would make the no-op test flaky at the ulp level.
    """
    bars = []
    for close in AAA_CLOSES:
        bar_nav = 0.0
        total_weight = 0.0
        bar_nav += AAA_SHARES * close  # `shares` branch: price path, base-free
        if BBB_WEIGHT > 0:
            bar_nav += BBB_WEIGHT * base  # `weight` branch
            total_weight += BBB_WEIGHT
        if 0.0 < total_weight < 1.0:
            bar_nav += (1.0 - total_weight) * base  # unallocated residual, base-scaled
        bars.append(bar_nav)
    intra_peak, intra_min = max(bars), min(bars)
    # PEAK_NAV sits BELOW the marked NAV, so `dd_info["peak_nav"]` is promoted to it and
    # `hwm_intra = max(peak_nav, intra_peak)` collapses to the intraday peak.
    hwm = max(base, intra_peak)
    return hwm, (intra_min - hwm) / hwm


# ---------------------------------------------------------------------------
# 1. The threaded MTM NAV reaches Stage 2 -- at the builder AND at the call site
# ---------------------------------------------------------------------------
def test_build_stage2_command_uses_the_threaded_mtm_nav(tmp_path):
    """DISCRIMINATING: pre-LV12 the child always got `str(args.portfolio_value)` (form)."""
    args = _mod.parse_args(
        ["--output-dir", str(tmp_path / "out"), "--portfolio-value", repr(FORM_NAV)]
    )

    threaded = _mod.build_stage2_command(args, portfolio_value=MTM_NAV)
    assert _flag(threaded, "--portfolio-value") == repr(MTM_NAV)
    assert _flag(threaded, "--portfolio-value") != repr(FORM_NAV)

    # The default is the historical form value, byte-for-byte: no other caller changes.
    assert _flag(_mod.build_stage2_command(args), "--portfolio-value") == repr(FORM_NAV)


def test_auto_run_stage2_launch_carries_the_gate_nav(tmp_path, monkeypatch):
    """The CALL SITE must thread `gate_nav`; a builder-only fix would leave the launch on form."""
    run = _run(
        tmp_path,
        monkeypatch,
        "autostage2",
        nav_effective=MTM_NAV,
        enable_5m=False,
        auto_stage2=True,
    )

    assert len(run["captured"]) == 1, run["captured"]
    child = run["captured"][0]
    assert os.path.basename(child[1]) == "run_live_strategy.py"
    assert _flag(child, "--portfolio-value") == repr(MTM_NAV)
    assert _flag(child, "--portfolio-value") != repr(FORM_NAV)
    # ... and it is the SAME NAV the gate itself was evaluated on.
    assert run["dd"]["current_nav"] == pytest.approx(MTM_NAV)
    assert float(_flag(child, "--portfolio-value")) == pytest.approx(run["dd"]["current_nav"])


# ---------------------------------------------------------------------------
# 2. The 5m intraday trough derives from `nav_effective`, not from the form value
# ---------------------------------------------------------------------------
def test_five_minute_trough_is_measured_on_the_mtm_nav(tmp_path, monkeypatch):
    """DISCRIMINATING: with the base on `args.portfolio_value` this is 200935.46 / -0.0004973."""
    run = _run(tmp_path, monkeypatch, "mtm5m", nav_effective=MTM_NAV)
    info = run["intraday"]

    expected_hwm, expected_dd = _old_formula_trough(MTM_NAV)
    form_hwm, _form_dd = _old_formula_trough(FORM_NAV)

    assert info["hwm_intra"] == pytest.approx(expected_hwm, abs=1e-6)
    assert info["dd_trough"] == pytest.approx(expected_dd, abs=1e-12)
    # The two bases are far apart, so this is not a rounding-level distinction.
    assert info["hwm_intra"] != pytest.approx(form_hwm, abs=1e-6)
    assert info["hwm_intra"] == pytest.approx(MTM_NAV + _BAR_OFFSETS[0], abs=1e-6)
    # The daily gate is on the SAME NAV (LV1), which is the point of the parity.
    assert run["dd"]["current_nav"] == pytest.approx(MTM_NAV)


# ---------------------------------------------------------------------------
# 3. `form == MTM` -> the 5m block is a NO-OP (bit-identical to the pre-fix arithmetic)
# ---------------------------------------------------------------------------
def test_equal_navs_leave_the_five_minute_block_bit_identical(tmp_path, monkeypatch):
    """Exact `==` on purpose: "bit-identical" is the contract when the base swap is a no-op."""
    equal = _run(tmp_path, monkeypatch, "equal", nav_effective=FORM_NAV, form_nav=FORM_NAV)
    info = equal["intraday"]
    expected_hwm, expected_dd = _old_formula_trough(FORM_NAV)

    assert info["hwm_intra"] == expected_hwm
    assert info["dd_trough"] == expected_dd
    assert "nav_base" not in info

    # A DEGRADED run falls back to the SAME form base, so its 5m block must be identical too --
    # the pre-fix numbers survive every path that does not have an MTM NAV to use.
    degraded = _run(tmp_path, monkeypatch, "equal_degraded", nav_effective=-1.0, form_nav=FORM_NAV)
    assert degraded["intraday"] == info


# ---------------------------------------------------------------------------
# 4. Degraded: no usable `nav_effective` -> explicit form fallback, no crash
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_effective", [None, 0.0, -1.0, "not-a-number"])
def test_degraded_run_falls_back_to_the_form_value_without_raising(
    tmp_path, monkeypatch, bad_effective
):
    """LV1's placeholder fallback is PRESERVED, not "fixed" into an exception."""
    run = _run(
        tmp_path,
        monkeypatch,
        f"degraded_{str(bad_effective).replace('-', 'neg').replace('.', 'p')}",
        nav_effective=bad_effective,
        form_nav=FORM_NAV,
    )

    assert run["dd"]["nav_source"] == _mod.NAV_SOURCE_FORM_FALLBACK_REFUSED
    assert run["dd"]["nav_mtm"] is None
    assert run["dd"]["tier_status"] == "UNKNOWN"
    expected_hwm, expected_dd = _old_formula_trough(FORM_NAV)
    assert run["intraday"]["hwm_intra"] == expected_hwm
    assert run["intraday"]["dd_trough"] == expected_dd
    # An admitted absence is not a silent difference: nothing to disclose as a "swap".
    assert "nav_base" not in run["intraday"]


# ---------------------------------------------------------------------------
# 5. The difference is RECORDED when it exceeds tolerance, ABSENT when it does not
# ---------------------------------------------------------------------------
def test_the_form_vs_mtm_difference_is_recorded_not_absorbed(tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch, "recorded", nav_effective=MTM_NAV)
    record = run["intraday"]["nav_base"]

    assert record["basis"] == "mark_to_market"
    assert record["form_value"] == pytest.approx(FORM_NAV)
    assert record["mtm_value"] == pytest.approx(MTM_NAV)
    assert record["delta"] == pytest.approx(MTM_NAV - FORM_NAV)
    assert record["delta"] < 0  # the form overstates NAV -- the archived direction
    assert record["base_used"] == pytest.approx(MTM_NAV)
    assert record["deviation_pct"] == pytest.approx((MTM_NAV - FORM_NAV) / abs(FORM_NAV))
    assert record["threshold_pct"] == pytest.approx(_mod.NAV_DEVIATION_WARN_PCT)

    # Emitted in the JSON report too, not only in the in-memory dict.
    reported = run["reported_intraday"]["nav_base"]
    assert reported["basis"] == "mark_to_market"
    for key in ("form_value", "mtm_value", "delta", "deviation_pct", "threshold_pct", "base_used"):
        assert reported[key] == pytest.approx(record[key]), key


def test_an_equal_pair_emits_no_difference_record(tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch, "not_recorded", nav_effective=FORM_NAV, form_nav=FORM_NAV)
    assert "nav_base" not in run["intraday"]
    assert "nav_base" not in run["reported_intraday"]


def test_a_sub_tolerance_difference_is_not_raised_as_an_event(tmp_path, monkeypatch):
    """Below the frozen 1% tolerance the swap is not an EVENT -- but it still uses MTM."""
    near = FORM_NAV * (1.0 - _mod.NAV_DEVIATION_WARN_PCT / 2.0)
    run = _run(tmp_path, monkeypatch, "sub_tolerance", nav_effective=near)

    assert "nav_base" not in run["intraday"]
    assert run["dd"]["nav_deviation_exceeded"] is False
    assert run["intraday"]["hwm_intra"] == pytest.approx(near + _BAR_OFFSETS[0], abs=1e-6)


# ---------------------------------------------------------------------------
# 6. Distribution gate is NAV-base independent
# ---------------------------------------------------------------------------
def test_distribution_fields_are_unchanged_by_the_base_swap(tmp_path, monkeypatch):
    mtm = _run(tmp_path, monkeypatch, "dist_mtm", nav_effective=MTM_NAV)
    equal = _run(tmp_path, monkeypatch, "dist_equal", nav_effective=FORM_NAV, form_nav=FORM_NAV)

    for key in (
        "distribution_alert",
        "distribution_gate_status",
        "intraday_buy_scale",
        "intraday_vwap_breadth",
        "afternoon_return_mean",
        "afternoon_vwap_breakdown_ratio",
        "symbols_measured",
        "symbols_vwap_measured",
    ):
        assert mtm["intraday"][key] == equal["intraday"][key], key

    # Non-trivial values, so the equality above is not two identical `None`s / passes.
    assert mtm["intraday"]["distribution_alert"] is True
    assert mtm["intraday"]["intraday_buy_scale"] == pytest.approx(0.50)
    # ... while the base REALLY did move under them.
    assert mtm["intraday"]["hwm_intra"] != pytest.approx(equal["intraday"]["hwm_intra"])


# ---------------------------------------------------------------------------
# 7. Daily gate fields are untouched by the base swap
# ---------------------------------------------------------------------------
def test_daily_gate_fields_are_untouched_by_the_5m_base_swap(tmp_path, monkeypatch):
    """Archived numbers: MTM 191630.06 against the 203049.96 peak == -5.62%.

    That is BELOW the 10% damping line, so the tier is NORMAL -- the archived fact this case
    pins is that the gate is evaluated on MTM at all (-5.62% vs the form's -1.53%), and that the
    5m block, which runs AFTER the gate and now consumes the same NAV, cannot move any gate field.
    """
    with_5m = _run(
        tmp_path, monkeypatch, "gate_5m", nav_effective=MTM_NAV, peak_nav=REAL_PEAK_NAV
    )
    without_5m = _run(
        tmp_path,
        monkeypatch,
        "gate_no5m",
        nav_effective=MTM_NAV,
        peak_nav=REAL_PEAK_NAV,
        enable_5m=False,
    )
    assert without_5m["intraday"] is None

    for key in (
        "current_nav",
        "peak_nav",
        "drawdown_pct",
        "tier",
        "nav_mtm",
        "nav_source",
        "tier1_counter",
        "linear_equity_scale",
    ):
        assert with_5m["dd"][key] == without_5m["dd"][key], key

    dd = with_5m["dd"]
    assert dd["nav_mtm"] == pytest.approx(MTM_NAV)
    assert dd["nav_source"] == _mod.NAV_SOURCE_MTM
    assert dd["peak_nav"] == pytest.approx(REAL_PEAK_NAV)
    assert dd["drawdown_pct"] == pytest.approx((MTM_NAV - REAL_PEAK_NAV) / REAL_PEAK_NAV)
    assert dd["drawdown_pct"] == pytest.approx(-0.0562, abs=5e-4)
    assert dd["tier"] == "NORMAL"

    # `dd_trough` stays DIAGNOSTIC: the gate/tier numbers are never rewritten from it.
    assert with_5m["intraday"]["dd_trough"] != pytest.approx(dd["drawdown_pct"], abs=1e-6)
    assert dd["tier"] == without_5m["dd"]["tier"]


# ---------------------------------------------------------------------------
# 8. (D7 follow-up) Provider provenance + session freshness reach the report
# ---------------------------------------------------------------------------
def _stale_5m_with_fallback(symbols, start=None, end=None, data_provider=None, cutoff_time="14:00"):
    """5m frames whose last bar belongs to the PREVIOUS session, served via a fallback.

    Mirrors the measured 2026-11-10/11-11 shape: the configured provider has no 5m rows for
    these symbols, the cache falls back to BaoStock, and that cache stops on an earlier
    session. Attrs are exactly what `load_intraday_5m_universe` stamps.
    """
    assert end == AS_OF, f"stage 1 resolved an unexpected as_of_date: {end!r}"
    prev_session = pd.Timestamp(AS_OF) - pd.Timedelta(days=1)
    idx = pd.DatetimeIndex([prev_session + pd.Timedelta(hours=14, minutes=55)])
    frames = {}
    for sym in symbols:
        frame = pd.DataFrame(
            {"Open": 10.0, "High": 10.1, "Low": 9.9, "Close": 10.0, "Volume": 1_000.0},
            index=idx,
        )
        frame.attrs.update(
            {
                "intraday_5m_available": True,
                "intraday_5m_source": "duckdb_cache",
                "intraday_5m_provider": "BaoStockDataProvider",
                "intraday_5m_latest_ts": idx[0].isoformat(),
                "fallback_from": "FuyaoDataProvider",
                "fallback_to": "BaoStockDataProvider",
                "fallback_reason": "provider_unsupported_5m",
                "fallback_reason_detail": "FuyaoDataProvider 在 cache.duckdb 中无 5m 行",
            }
        )
        frames[sym] = frame
    return frames


def _no_features(universe, universe_5m, config=None, synthesize_intraday_bar=False):
    """No measured microstructure columns -> the distribution gate cannot be evaluated."""
    return dict(universe)


def test_provider_fallback_and_session_staleness_reach_the_report(tmp_path, monkeypatch):
    """DISCRIMINATING: pre-D7 the block said only "provider does not support 5m".

    It must now record WHO served the bars (BaoStock via a fuyao fallback), the fallback
    reason, and the session lag measured by `assess_intraday_5m_freshness` -- and it must
    NOT reduce the diagnosis to the provider-level token in `degraded_reason_code`.
    """
    run = _run(
        tmp_path,
        monkeypatch,
        "stale_fallback",
        nav_effective=FORM_NAV,
        form_nav=FORM_NAV,
        loader=_stale_5m_with_fallback,
        enricher=_no_features,
    )
    block = run["reported_intraday"]
    prev_iso = f"{pd.Timestamp(AS_OF) - pd.Timedelta(days=1):%Y-%m-%d}T14:55:00"

    # --- provenance (who actually served it)
    assert block["provider"] == "BaoStockDataProvider"
    assert block["source"] == "duckdb_cache"
    assert block["latest_ts"] == prev_iso
    assert block["fallback_from"] == "FuyaoDataProvider"
    assert block["fallback_to"] == "BaoStockDataProvider"
    assert block["fallback_reason"] == "provider_unsupported_5m"
    assert block["n_fallback_symbols"] == 2
    assert set(block["provider_by_symbol"]) == {"AAA", "BBB"}

    # --- session freshness (worst symbol, from the shared owner)
    assert block["stale"] is True
    assert block["lag_calendar_days"] == 1
    assert block["freshness_reason"] == "stale_last_bar"
    assert block["expected_last_session"] == AS_OF
    assert block["rows_5m"] == 0  # nothing on the as-of session

    # --- the diagnosis, not a blunt "provider unsupported"
    assert block["status"] == "degraded_stale_intraday_cache"
    assert block["degraded"] is True
    assert block["degraded_reason_code"] == "intraday_5m_stale_window"
    assert block["degraded_reason_code"] != "provider_unsupported_5m"
    assert block["enabled"] is False  # requested, but zero as-of rows
    assert block["requested"] is True
    assert block["generated_at"] == "2026-04-11T00:05:00+00:00"  # pinned clock

    # --- the Markdown banner names the lag AND the fallback chain
    md = open(os.path.join(run["out_dir"], "stage1_health_report.md"), encoding="utf-8").read()
    assert "FuyaoDataProvider→BaoStockDataProvider (provider_unsupported_5m)" in md
    assert "滞后 `1` 天" in md
