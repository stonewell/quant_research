"""2026-11-16 SCOPE + BASIS DIAGNOSTICS: the vol numbers are labelled, never re-gated.

TWO DEFECTS FROM THE 2026-11-16 ARCHIVE
---------------------------------------
1. **The vol ceiling compares two different quantities.** `realized_vol_21d` is
   `np.median(vol_estimates)` -- a CROSS-SECTIONAL MEDIAN of single-name vols -- while
   `target_vol = 0.12` is a PORTFOLIO target and the strategy's own backtest scales on
   PORTFOLIO return vol (`blend_base.py:1557-1575`). That round reported 0.3033, the
   downstream cap turned it into a 0.3957 factor and held equity at 39.6%, while the
   equal-weight 20-name basket realized 9.94% on the SAME 21 sessions (2026-10-19..
   2026-11-16) -- a ~3.2x diversification gap. Nothing is re-wired here: the fix is a
   second, correctly-scoped MEASUREMENT (`vol_portfolio_ew_21d`, `vol_scope_ratio`,
   `vol_scope_basis`, `vol_scope_warning`) plus an explicit label.

2. **"Intraday trough drawdown" is not intraday.** `intraday_5m_metrics.dd_trough` was
   byte-identical to `portfolio_health.drawdown_pct` (-0.057661231739880504) with
   `hwm_intra == peak_nav`, i.e. the DAILY HWM drawdown relabelled -- and
   `advisory.r1.dd_source = "dd_trough"` consumed it. Stage 1 has no holdings, so a real
   intraday account NAV cannot be formed; instead the number keeps its value and gains
   honest labels (`dd_trough_basis`, `hwm_intra_source`) plus an explicitly-labelled
   equal-weight 5m basket PROXY.

WHAT THIS FILE PINS
-------------------
* the equal-weight portfolio vol IS a portfolio vol (independently recomputed), the cash
  proxy is excluded, and the ratio/basis/warning agree with the arithmetic;
* the pre-existing gate numbers (`realized_vol_21d` -> `vol_scaling_factor`) are
  UNCHANGED by the diagnostic -- same formula, same value;
* a short window degrades to `None` + reason (never a fabricated number), and equal
  scopes stay silent (no warning below the 1.5x threshold);
* both renderers print the comparison and the `[WARN]` line, and the LV18 scope
  qualifier still rides the factor line;
* the 5m trough proxy is a diversified average (not the worst single name), degrades to
  `None` + reason below 2 symbols, and never reuses a previous session's bars;
* `dd_trough_basis` reaches an R1 block when one exists, and nothing is created when it
  does not.

100% offline: synthetic frames, no `main()` run, no network, no `data/runs/**`.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import scripts.check_live_portfolio_health as health_mod  # noqa: E402

CASH = "BIL"

#: Close-to-close keeps the synthetic expectations exact (no OHLC estimator involved).
_PARAMS = {
    "cfsb_target_vol": 0.12,
    "cfsb_vol_model": "close_to_close",
    "cash_proxy": CASH,
}

#: Archived numbers the disclosure cites (`2026-11-16` round).
ARCHIVED_VOL = 0.3033
ARCHIVED_PORTFOLIO_VOL = 0.0994


# --------------------------------------------------------------------------- #
# Synthetic universes
# --------------------------------------------------------------------------- #
def _universe(
    n_symbols: int = 20,
    periods: int = 90,
    market_vol: float = 0.006,
    idio_vol: float = 0.018,
    seed: int = 11,
):
    """One-factor basket: `market_vol` common + `idio_vol` idiosyncratic, daily.

    The point of the two components is that the SINGLE-NAME vol
    (`sqrt(mkt^2 + idio^2)`) and the EQUAL-WEIGHT portfolio vol
    (`sqrt(mkt^2 + idio^2 / n)`) differ by the diversification factor, which is exactly
    the 2026-11-16 defect: ~30% single-name vs ~10% portfolio.
    """
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2026-07-01", periods=periods)
    market = rng.normal(0.0, market_vol, size=periods)
    universe = {}
    for i in range(n_symbols):
        idio = rng.normal(0.0, idio_vol, size=periods)
        close = 100.0 * np.exp(np.cumsum(market + idio))
        universe[f"S{i:02d}.SH"] = pd.DataFrame({"Close": close}, index=idx)
    return universe, idx


def _ew_portfolio_vol(universe, window=health_mod.VOL_MIN_OBS):
    """Independent re-derivation of the expected equal-weight portfolio vol."""
    matrix = pd.DataFrame({s: df["Close"] for s, df in universe.items()}).sort_index()
    ew = matrix.pct_change().mean(axis=1, skipna=True).dropna()
    return float(ew.iloc[-window:].std(ddof=1) * np.sqrt(252.0))


def _five_min_frame(day: str, closes, start: str = "09:30"):
    idx = pd.date_range(f"{day} {start}", periods=len(closes), freq="5min")
    return pd.DataFrame(
        {
            "Open": list(closes),
            "High": [c * 1.001 for c in closes],
            "Low": [c * 0.999 for c in closes],
            "Close": list(closes),
            "Volume": 1000.0,
        },
        index=idx,
    )


# --------------------------------------------------------------------------- #
# 1. The new number IS a portfolio vol, and the gate numbers do not move
# --------------------------------------------------------------------------- #
def test_equal_weight_portfolio_vol_is_reported_beside_the_cross_sectional_median():
    """DISCRIMINATING: the diagnostic must be a PORTFOLIO vol, not a second median.

    PREVENTS: the two cheap non-fixes -- (a) reporting `np.median` again under a new
    key (the ratio would be ~1.0 and the defect would stay invisible), or (b) writing
    the correctly-scoped number INTO `realized_vol_21d` (which would silently change the
    Stage-2 cap this task must not touch). Both are caught here: the value must match an
    independently computed equal-weight stdev, and the gate's own formula must still
    reproduce `vol_scaling_factor` from the UNCHANGED `realized_vol_21d`.
    """
    universe, idx = _universe()
    as_of = idx[-1].date().isoformat()

    res = health_mod.evaluate_realized_volatility(universe, _PARAMS, as_of=as_of)

    assert res["realized_vol_21d"] is not None
    assert res["vol_portfolio_ew_21d"] is not None

    # Independent re-derivation: same daily matrix, row-wise mean, last 21 obs, ddof=1.
    assert res["vol_portfolio_ew_21d"] == pytest.approx(
        _ew_portfolio_vol(universe), rel=1e-12
    )
    # It is emphatically NOT the cross-sectional median re-published.
    assert res["vol_portfolio_ew_21d"] != pytest.approx(res["realized_vol_21d"])

    assert res["vol_scope_basis"] == "cross_sectional_median"
    assert res["vol_scope_basis"] == health_mod.VOL_SCOPE_BASIS_CROSS_SECTIONAL
    assert res["vol_scope_ratio"] == pytest.approx(
        res["realized_vol_21d"] / res["vol_portfolio_ew_21d"], rel=1e-12
    )
    # The synthetic basket is built to have the ARCHIVED shape: ~3x diversification gap.
    assert res["vol_scope_ratio"] > health_mod.VOL_SCOPE_RATIO_WARN_THRESHOLD
    assert res["vol_scope_warning"] is not None
    assert "CROSS-SECTIONAL MEDIAN" in res["vol_scope_warning"]
    assert "PORTFOLIO-level target" in res["vol_scope_warning"]
    assert f"{res['vol_portfolio_ew_21d']:.4f}" in res["vol_scope_warning"]
    assert f"{res['vol_scope_ratio']:.2f}x" in res["vol_scope_warning"]
    assert res["vol_scope_unavailable_reason"] is None

    # ZERO BEHAVIOUR DRIFT: the factor is still `min(1, target / realized_vol_21d)`.
    assert res["vol_scaling_factor"] == pytest.approx(
        min(1.0, res["target_vol"] / res["realized_vol_21d"])
    )
    # ... which is strictly more conservative than the portfolio-scoped factor would be.
    assert res["vol_scaling_factor"] <= min(
        1.0, res["target_vol"] / res["vol_portfolio_ew_21d"]
    )


def test_cash_proxy_is_excluded_from_the_equal_weight_basket():
    """A constant-price cash sleeve must not dilute the basket's measured vol."""
    universe, idx = _universe(n_symbols=6)
    equity_only = dict(universe)
    with_cash = dict(universe)
    with_cash[CASH] = pd.DataFrame(
        {"Close": np.full(len(idx), 100.0)}, index=idx
    )
    as_of = idx[-1].date().isoformat()

    base = health_mod.evaluate_realized_volatility(equity_only, _PARAMS, as_of=as_of)
    mixed = health_mod.evaluate_realized_volatility(with_cash, _PARAMS, as_of=as_of)

    assert mixed["vol_portfolio_ew_21d"] == pytest.approx(
        base["vol_portfolio_ew_21d"], rel=1e-12
    )
    assert mixed["vol_scope_ratio"] == pytest.approx(base["vol_scope_ratio"], rel=1e-12)


def test_short_window_degrades_to_null_with_a_reason_not_a_number():
    """Fewer than VOL_MIN_OBS observations is an explicit degradation, never a value."""
    universe, idx = _universe(periods=15)
    as_of = idx[-1].date().isoformat()

    res = health_mod.evaluate_realized_volatility(universe, _PARAMS, as_of=as_of)

    assert res["realized_vol_21d"] is None
    assert res["vol_portfolio_ew_21d"] is None
    assert res["vol_scope_ratio"] is None
    assert res["vol_scope_warning"] is None
    assert isinstance(res["vol_scope_unavailable_reason"], str)
    assert res["vol_scope_unavailable_reason"].strip()
    # The basis still documents what `realized_vol_21d` would have been.
    assert res["vol_scope_basis"] == "cross_sectional_median"


def test_equal_scopes_produce_no_warning():
    """A perfectly correlated basket has single-name vol == portfolio vol => silence.

    PREVENTS: a warning that always fires, which would train an operator to ignore the
    one case that matters.
    """
    universe, idx = _universe(n_symbols=5, idio_vol=0.0)
    as_of = idx[-1].date().isoformat()

    res = health_mod.evaluate_realized_volatility(universe, _PARAMS, as_of=as_of)

    assert res["vol_scope_ratio"] == pytest.approx(1.0, rel=1e-6)
    assert res["vol_scope_warning"] is None


# --------------------------------------------------------------------------- #
# 2. Rendering: the comparison + the [WARN] line, in BOTH artefacts
# --------------------------------------------------------------------------- #
def _vol_info(*, with_warning_string=True):
    ratio = ARCHIVED_VOL / ARCHIVED_PORTFOLIO_VOL
    info = {
        "realized_vol_21d": ARCHIVED_VOL,
        "target_vol": 0.12,
        "vol_scaling_factor": 0.12 / ARCHIVED_VOL,
        "vol_elevated": True,
        "vol_degraded": False,
        "vol_model": "Close-to-Close",
        "vol_window_start": "2026-10-19",
        "vol_window_end": "2026-11-16",
        "vol_n_obs": 21,
        "vol_input_bars_last_date": "2026-11-16",
        "vol_input_lag_days": 0,
        "vol_input_source": "close_to_close",
        "vol_portfolio_ew_21d": ARCHIVED_PORTFOLIO_VOL,
        "vol_scope_ratio": ratio,
        "vol_scope_basis": "cross_sectional_median",
        "vol_scope_warning": None,
    }
    if with_warning_string:
        info["vol_scope_warning"] = (
            f"SCOPE MISMATCH: realized_vol_21d={ARCHIVED_VOL:.4f} is the CROSS-SECTIONAL "
            f"MEDIAN of single-name volatilities, but it is compared against target_vol="
            f"0.1200, a PORTFOLIO-level target. The equal-weight portfolio realized "
            f"{ARCHIVED_PORTFOLIO_VOL:.4f} over the same 21 sessions (ratio {ratio:.2f}x); "
            "a portfolio-vol cap would not de-lever this far. Diagnostic only -- no order "
            "is gated on this line."
        )
    return info


def _dd_info():
    return {
        "current_nav": 100000.0,
        "peak_nav": 100000.0,
        "drawdown_pct": -0.05,
        "tier": "TIER_1_DAMPING",
        "tier_label": "TIER 1",
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
        "tracked_symbols_count": 20,
        "leaders": [],
        "breadth_active": True,
        "thrust_active": False,
    }


def _dashboard_kwargs(vol_info, dd_info=None):
    return dict(
        as_of_date="2026-11-16",
        universe_path="/tmp/univ.txt",
        strategy_path="/tmp/strat.json",
        strategy_info={"strategy_name": "x", "template_name": "chan_four_state_blend"},
        dd_info=_dd_info() if dd_info is None else dd_info,
        macro_info=_macro_info(),
        vol_info=vol_info,
        gate_code="CAUTION_DAMPING",
        gate_headline="CAUTION",
        actions=["sweep into cash"],
        timing=None,
    )


def test_dashboard_prints_the_scope_comparison_and_the_warn_line(capsys):
    health_mod.print_dashboard(**_dashboard_kwargs(_vol_info()))
    out = capsys.readouterr().out

    scope = [ln for ln in out.splitlines() if "Scope check" in ln]
    assert scope, out
    line = scope[0]
    assert "single-name median 30.3%" in line
    assert "equal-weight portfolio 9.9%" in line
    assert "3.05x" in line
    assert "单位口径不一致" in line

    warn = [ln for ln in out.splitlines() if "[WARN]" in ln and "CROSS-SECTIONAL" in ln]
    assert warn, out

    # The LV18 qualifier still rides the factor line (nothing here displaced it).
    factor = [ln for ln in out.splitlines() if "Volatility Scaling" in ln]
    assert factor and health_mod.VOL_CAP_SCOPE_QUALIFIER in factor[0]


def test_markdown_report_prints_the_scope_comparison_and_the_warn_line(tmp_path):
    kwargs = _dashboard_kwargs(_vol_info())
    kwargs.pop("universe_path")
    kwargs.pop("strategy_path")
    _json_path, md_path = health_mod.generate_reports(
        output_dir=str(tmp_path),
        **kwargs,
    )
    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()

    bullets = [ln for ln in md.splitlines() if ln.startswith("- **Scope check**")]
    assert bullets, md
    assert "single-name median 30.3%" in bullets[0]
    assert "equal-weight portfolio 9.9%" in bullets[0]
    assert "3.05x" in bullets[0]
    assert "单位口径不一致" in bullets[0]

    warns = [ln for ln in md.splitlines() if "[WARN]" in ln and "CROSS-SECTIONAL" in ln]
    assert warns, md

    # The pre-existing vol bullets are untouched.
    assert any("Barroso Volatility Scaling" in ln for ln in md.splitlines())


def test_renderers_stay_silent_when_the_scope_reference_is_missing():
    """An older/degraded `vol_info` must not grow an invented line."""
    info = _vol_info()
    info.pop("vol_portfolio_ew_21d")
    info.pop("vol_scope_ratio")
    info["vol_scope_warning"] = None

    assert health_mod.format_vol_scope_comparison(info) is None
    assert health_mod.format_vol_scope_mismatch(info) is None


def test_warning_falls_back_to_the_two_numbers_when_the_string_is_absent():
    info = _vol_info(with_warning_string=False)
    assert health_mod.format_vol_scope_mismatch(info).startswith("[WARN]")

    info["vol_scope_ratio"] = 1.0
    assert health_mod.format_vol_scope_mismatch(info) is None


# --------------------------------------------------------------------------- #
# 3. The 5m trough PROXY: diversified, honest about absence
# --------------------------------------------------------------------------- #
def test_intraday_trough_proxy_is_a_diversified_average_not_the_worst_name():
    """DISCRIMINATING: the proxy must be the basket's drawdown, not a single name's.

    PREVENTS: the temptation to print `min(single-name drawdown)` as "the trough" --
    which is both pessimistic and exactly the labelling error this task fixes (a
    per-symbol quantity presented as a portfolio one).
    """
    day = "2026-11-16"
    frames = {
        "AAA.SH": _five_min_frame(day, [10.0, 10.5, 10.0, 10.2]),
        "BBB.SH": _five_min_frame(day, [20.0, 20.0, 20.0, 20.0]),
    }

    trough, reason, n_syms = health_mod._equal_weight_intraday_trough_proxy(
        frames, CASH, day, "14:00"
    )

    paths = {s: df["Close"] / df["Close"].iloc[0] - 1.0 for s, df in frames.items()}
    ew = pd.DataFrame(paths).mean(axis=1)
    nav = 1.0 + ew
    expected = float((nav / nav.cummax() - 1.0).min())

    assert reason is None
    assert n_syms == 2
    assert trough == pytest.approx(expected, rel=1e-12)
    assert trough < 0.0
    # The single-name worst move is much deeper than the diversified path.
    single_worst = float(
        min(
            (p + 1.0).div((p + 1.0).cummax()).sub(1.0).min()
            for p in paths.values()
        )
    )
    assert trough > single_worst


def test_intraday_trough_proxy_is_null_below_two_symbols():
    day = "2026-11-16"
    frames = {"AAA.SH": _five_min_frame(day, [10.0, 9.9, 9.8])}

    trough, reason, n_syms = health_mod._equal_weight_intraday_trough_proxy(
        frames, CASH, day, "14:00"
    )

    assert trough is None
    assert n_syms == 1
    assert isinstance(reason, str) and ">= 2" in reason


def test_intraday_trough_proxy_never_reuses_a_previous_sessions_bars():
    """A stale 5m cache is NOT this session's intraday path."""
    frames = {
        "AAA.SH": _five_min_frame("2026-11-13", [10.0, 9.0, 8.5]),
        "BBB.SH": _five_min_frame("2026-11-13", [20.0, 19.0, 18.5]),
    }

    trough, reason, n_syms = health_mod._equal_weight_intraday_trough_proxy(
        frames, CASH, "2026-11-16", "14:00"
    )

    assert trough is None
    assert n_syms == 0
    assert isinstance(reason, str) and reason.strip()


def test_intraday_trough_proxy_skips_the_cash_proxy_universe_member():
    day = "2026-11-16"
    frames = {
        "AAA.SH": _five_min_frame(day, [10.0, 10.2]),
        "BBB.SH": _five_min_frame(day, [20.0, 20.1]),
        CASH: _five_min_frame(day, [100.0, 100.0]),
    }

    _trough, _reason, n_syms = health_mod._equal_weight_intraday_trough_proxy(
        frames, CASH, day, "14:00"
    )

    assert n_syms == 2


# --------------------------------------------------------------------------- #
# 4. Basis labels + R1 carry-through
# --------------------------------------------------------------------------- #
def test_basis_constants_name_the_daily_hwm_and_its_anchor():
    assert health_mod.DD_TROUGH_BASIS_DAILY_HWM == "daily_hwm_drawdown"
    assert health_mod.HWM_INTRA_SOURCE_PEAK_NAV == "portfolio_health.peak_nav"


def test_r1_block_receives_the_basis_only_when_it_exists():
    """Additive carry-through: never create an R1 block, never overwrite a key."""
    info = {"dd_trough_basis": health_mod.DD_TROUGH_BASIS_DAILY_HWM}

    # P1 shape today: no `r1` -> the advisory block must come back byte-identical.
    p1 = {"phase": "P1", "r1_effective": False}
    before = dict(p1)
    health_mod.carry_dd_trough_basis_into_r1(p1, info)
    assert p1 == before

    # No advisory at all -> no crash, nothing returned.
    assert health_mod.carry_dd_trough_basis_into_r1(None, info) is None

    # An existing R1 block gains the basis (status/multiplier untouched).
    r1 = {"status": "LIGHT", "multiplier": 0.75, "dd_source": "dd_trough"}
    block = {"r1": r1}
    health_mod.carry_dd_trough_basis_into_r1(block, info)
    assert block["r1"]["dd_trough_basis"] == health_mod.DD_TROUGH_BASIS_DAILY_HWM
    assert block["r1"]["status"] == "LIGHT"
    assert block["r1"]["multiplier"] == 0.75
    assert block["r1"]["dd_source"] == "dd_trough"

    # An existing basis is never overwritten.
    block2 = {"r1": {"dd_trough_basis": "already_there"}}
    health_mod.carry_dd_trough_basis_into_r1(block2, info)
    assert block2["r1"]["dd_trough_basis"] == "already_there"


def test_source_labels_both_intraday_branches_and_both_render_sites():
    """STATIC: the 5m block's two branches and both artefacts carry the new labels.

    PREVENTS: labelling only the measured branch (leaving the degraded branch's
    `dd_trough: null` unexplained) or only one of the two renderers -- the repo's
    LV18 lesson is that a disclosure missing from one site is a disclosure missing.
    """
    path = os.path.join(_REPO_ROOT, "scripts", "check_live_portfolio_health.py")
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()

    assert source.count('"dd_trough_basis": DD_TROUGH_BASIS_DAILY_HWM') == 2
    assert source.count('"hwm_intra_source": HWM_INTRA_SOURCE_PEAK_NAV') == 2
    assert source.count('"intraday_trough_ew_proxy": _intraday_trough_ew_proxy') == 2
    # Both renderers consult the one wording owner.
    assert source.count("format_vol_scope_comparison(vol_info)") >= 2
    assert source.count("format_vol_scope_mismatch(vol_info)") >= 2
    # The proxy is never laundered into the gating field.
    assert "dd_trough = _intraday_trough_ew_proxy" not in source


# --------------------------------------------------------------------------- #
# 5. HWM ELIGIBILITY GUARD (2026-11-05 archive)
#
# On 2026-11-05 `current_nav` was 208830.78 = that day's equity (186218.00) + the
# PREVIOUS round's cash (22612.78), while the legs on file summed to 198810.57. Because
# `peak_nav` only ratchets up, the phantom became the permanent high-water mark and
# every later round reported a deeper drawdown than the account had (-5.77% vs the true
# -3.08% on 2026-11-16) -- i.e. it drove the real circuit breaker.
# --------------------------------------------------------------------------- #
_LEGACY_NEW_KEYS = (
    "hwm_eligible",
    "hwm_ineligibility_reason",
    "peak_nav_source",
    "hwm_guard_applied",
    # (NV13) peak PROVENANCE -- three additive, informational keys. They must never carry
    # a decision: the full-dict comparison below is what proves the ladder ignored them.
    "peak_provenance",
    "peak_provenance_detail",
    "max_leg_verified_nav",
)

#: `inspect_portfolio_drawdown`'s PRE-GUARD body, transcribed verbatim (defaults only:
#: the tests below pass `params={}` so `resolve_blend_param` yields 0.10/0.15/0.20/15/21).
#: It exists so "eligible behaviour is unchanged" is proved by a FULL-DICT comparison
#: against the old code path, not asserted in prose.
def _legacy_inspect_portfolio_drawdown(current_nav, peak_nav, state):
    dd_reduce_thresh, dd_defensive_thresh, dd_stop_thresh = 0.10, 0.15, 0.20
    tier1_cooldown_bars, stop_cooldown_bars = 15, 21

    saved_peak = state.get("peak_nav")
    if peak_nav is not None:
        effective_peak = float(peak_nav)
    elif saved_peak is not None and saved_peak > 0:
        effective_peak = max(float(saved_peak), current_nav)
    else:
        effective_peak = current_nav

    current_dd = (current_nav - effective_peak) / effective_peak if effective_peak > 0 else 0.0
    dd_mag = abs(current_dd)

    tier1_counter = int(state.get("tier1_consecutive_bars", 0))
    tier3_counter = int(state.get("tier3_consecutive_bars", 0))
    freeze_remaining = int(state.get("freeze_remaining_bars", 0))
    auto_healed = False
    heal_reason = ""

    if dd_mag >= dd_stop_thresh:
        tier3_counter += 1
        tier1_counter = 0
        freeze_remaining = stop_cooldown_bars if freeze_remaining == 0 else max(0, freeze_remaining - 1)
        if tier3_counter >= stop_cooldown_bars:
            effective_peak = current_nav
            tier3_counter = 0
            freeze_remaining = 0
            current_dd = 0.0
            dd_mag = 0.0
            auto_healed = True
            heal_reason = f"Tier 3 Full Stop sustained {stop_cooldown_bars} bars: Auto-healed HWM reset to current NAV."
    elif dd_mag >= dd_reduce_thresh:
        tier1_counter += 1
        tier3_counter = 0
        freeze_remaining = max(0, freeze_remaining - 1) if freeze_remaining > 0 else 0
        if tier1_counter >= tier1_cooldown_bars:
            effective_peak = current_nav
            tier1_counter = 0
            current_dd = 0.0
            dd_mag = 0.0
            auto_healed = True
            heal_reason = f"Tier 1/2 Risk Damping sustained {tier1_cooldown_bars} bars: Auto-healed HWM reset to current NAV."
    else:
        tier1_counter = 0
        tier3_counter = 0
        freeze_remaining = max(0, freeze_remaining - 1) if freeze_remaining > 0 else 0
        if current_nav > effective_peak:
            effective_peak = current_nav

    nav_hist = state.get("nav_history_10d", []) or []
    nav_hist_len = len(nav_hist)
    nav_10d_distinct_len = health_mod.count_distinct_nav_points(nav_hist)
    base_nav = None
    if nav_10d_distinct_len >= health_mod.NAV_10D_RECOVERY_WINDOW:
        try:
            base_nav = float(nav_hist[-health_mod.NAV_10D_RECOVERY_WINDOW])
        except (TypeError, ValueError):
            base_nav = None
    if base_nav is not None and base_nav > 0.0:
        r_10d = current_nav / base_nav - 1.0
        nav_10d_status = health_mod.NAV_10D_STATUS_OK
        nav_10d_unavailable_reason = None
    else:
        r_10d = None
        nav_10d_status = health_mod.NAV_10D_STATUS_INSUFFICIENT_HISTORY
        if nav_10d_distinct_len < health_mod.NAV_10D_RECOVERY_WINDOW:
            if nav_hist_len < health_mod.NAV_10D_RECOVERY_WINDOW:
                nav_10d_unavailable_reason = (
                    f"nav_history_10d 仅有 {nav_hist_len}/{health_mod.NAV_10D_RECOVERY_WINDOW} 个交易日记录"
                    f"（去重后仅 {nav_10d_distinct_len} 个不同值），"
                    "不足以测量 10 日 NAV 恢复幅度"
                )
            else:
                nav_10d_unavailable_reason = (
                    f"nav_history_10d 有 {nav_hist_len} 条记录，但去重后仅 "
                    f"{nav_10d_distinct_len}/{health_mod.NAV_10D_RECOVERY_WINDOW} 个不同交易日"
                    "（相邻重复点不重复计数），不足以测量 10 日 NAV 恢复幅度"
                )
        else:
            nav_10d_unavailable_reason = (
                f"nav_history_10d 起点 NAV 非正数或不可解析 ({nav_hist[0]!r})，10 日恢复幅度不可测"
            )

    fast_recovery_nav = bool(r_10d is not None and r_10d > 0.0)

    if dd_mag >= dd_stop_thresh or freeze_remaining > 0:
        tier, tier_label, linear_scale = (
            "TIER_3_HALT", "🔴 TIER 3 (Emergency Halt / 100% Cash)", 0.0,
        )
    elif dd_mag >= dd_defensive_thresh:
        tier, tier_label = "TIER_2_DEFENSIVE", "🟠 TIER 2 (Tactical Defense Mode / 70% Cash)"
        linear_scale = max(0.0, 1.0 - (dd_mag - dd_reduce_thresh) / max(0.01, dd_stop_thresh - dd_reduce_thresh))
    elif dd_mag >= dd_reduce_thresh:
        tier, tier_label = "TIER_1_DAMPING", "🟡 TIER 1 (Smooth Linear Damping)"
        linear_scale = max(0.0, 1.0 - (dd_mag - dd_reduce_thresh) / max(0.01, dd_stop_thresh - dd_reduce_thresh))
    else:
        tier, tier_label, linear_scale = "NORMAL", "🟢 NORMAL (Full Risk Budget)", 1.0

    return {
        "current_nav": current_nav,
        "peak_nav": effective_peak,
        "drawdown_pct": current_dd,
        "drawdown_mag": dd_mag,
        "tier": tier,
        "tier_label": tier_label,
        "linear_equity_scale": linear_scale,
        "tier1_counter": tier1_counter,
        "tier1_max": tier1_cooldown_bars,
        "tier3_counter": tier3_counter,
        "tier3_max": stop_cooldown_bars,
        "freeze_remaining_bars": freeze_remaining,
        "auto_healed": auto_healed,
        "heal_reason": heal_reason,
        "nav_10d_return": r_10d,
        "nav_10d_status": nav_10d_status,
        "nav_10d_history_len": nav_hist_len,
        "nav_10d_distinct_len": nav_10d_distinct_len,
        "nav_10d_required_len": health_mod.NAV_10D_RECOVERY_WINDOW,
        "nav_10d_unavailable_reason": nav_10d_unavailable_reason,
        "fast_recovery_nav": fast_recovery_nav,
    }


def _ten_distinct_history(top=100000.0):
    """Ten ADJACENTLY DISTINCT NAV points -> the measured 10-day branch (no reason text)."""
    return [top - i * 100.0 for i in range(9, -1, -1)]


#: (label, current_nav, peak_nav, state) -- the four shapes the guard can meet.
_ELIGIBLE_SCENARIOS = {
    # the branch that turned the phantom into the permanent HWM
    "peak_advance": (
        110000.0,
        None,
        {"peak_nav": 100000.0, "nav_history_10d": _ten_distinct_history()},
    ),
    # Tier-1 auto-heal: the HWM is rewritten DOWN to the current NAV
    "tier1_heal": (
        87000.0,
        None,
        {
            "peak_nav": 100000.0,
            "tier1_consecutive_bars": 14,
            "nav_history_10d": _ten_distinct_history(),
        },
    ),
    # Tier-3 auto-heal: same downward rewrite, from the halt branch
    "tier3_heal": (
        70000.0,
        None,
        {
            "peak_nav": 100000.0,
            "tier3_consecutive_bars": 20,
            "freeze_remaining_bars": 3,
            "nav_history_10d": _ten_distinct_history(),
        },
    ),
    # no peak anywhere: this NAV initialises the mark
    "initialise": (100000.0, None, {"nav_history_10d": _ten_distinct_history()}),
    # `--peak-nav` wins over state
    "cli_peak": (95000.0, 120000.0, {"peak_nav": 100000.0}),
}


@pytest.mark.parametrize("label", sorted(_ELIGIBLE_SCENARIOS))
@pytest.mark.parametrize("nav_eligible", [None, True])
def test_eligible_or_unspecified_nav_is_bit_identical_to_the_pre_change_path(
    label, nav_eligible
):
    """The guard must be INERT for every existing caller and every eligible NAV.

    PREVENTS: a refactor that "tightens" the peak resolution for everybody -- e.g.
    dropping the `max(saved_peak, current_nav)` seed, or re-deriving the drawdown after
    a heal. Proved as a FULL-DICT comparison against the transcribed pre-guard body
    (minus the four new keys), for a peak-advance case, both heal cases, the
    initialisation case and the `--peak-nav` case.
    """
    current_nav, peak_nav, state = _ELIGIBLE_SCENARIOS[label]
    state = dict(state)

    new = health_mod.inspect_portfolio_drawdown(
        current_nav=current_nav,
        peak_nav=peak_nav,
        state=dict(state),
        params={},
        nav_eligible=nav_eligible,
    )
    legacy = _legacy_inspect_portfolio_drawdown(current_nav, peak_nav, dict(state))

    stripped = {k: v for k, v in new.items() if k not in _LEGACY_NEW_KEYS}
    assert stripped == legacy, (
        f"{label}: eligible behaviour drifted from the pre-guard code\n"
        f"new   ={stripped}\nlegacy={legacy}"
    )
    assert new["hwm_guard_applied"] is False
    assert new["hwm_eligible"] is nav_eligible


@pytest.mark.parametrize("label", ["tier1_heal", "tier3_heal"])
def test_ineligible_nav_cannot_move_the_hwm_in_either_direction(label):
    """DISCRIMINATING: the guard blocks the ADVANCE and the HEAL reset.

    PREVENTS: both halves of the 2026-11-05 defect -- (a) the phantom 208830.78 becoming
    the permanent mark (advance), and (b) the tempting "fix" of letting the auto-heal
    overwrite the mark downward with another unverified NAV. Under `nav_eligible=False`
    the peak must be EXACTLY the prior state peak in both cases, while the tier math
    (counters, heal flag, `dd_mag`, tier) keeps running.
    """
    current_nav, peak_nav, state = _ELIGIBLE_SCENARIOS[label]
    legacy = _legacy_inspect_portfolio_drawdown(current_nav, peak_nav, dict(state))

    result = health_mod.inspect_portfolio_drawdown(
        current_nav=current_nav,
        peak_nav=peak_nav,
        state=dict(state),
        params={},
        nav_eligible=False,
        ineligibility_reason="cash_as_of=2026-11-04 != as_of=2026-11-05",
    )

    assert result["hwm_eligible"] is False
    assert result["hwm_guard_applied"] is True
    assert result["hwm_ineligibility_reason"].startswith("cash_as_of=2026-11-04")
    # The legacy path moved the mark in BOTH scenarios; the guard must not.
    assert legacy["peak_nav"] != float(state["peak_nav"]), (
        f"{label}: scenario no longer exercises a peak move; pick another"
    )
    assert result["peak_nav"] == pytest.approx(float(state["peak_nav"]))
    assert result["peak_nav_source"] == "prior_state"
    # The tier ladder is NOT re-scoped by this guard.
    assert result["tier"] == legacy["tier"]
    assert result["linear_equity_scale"] == pytest.approx(legacy["linear_equity_scale"])
    assert result["tier1_counter"] == legacy["tier1_counter"]
    assert result["tier3_counter"] == legacy["tier3_counter"]
    assert result["freeze_remaining_bars"] == legacy["freeze_remaining_bars"]
    assert result["drawdown_mag"] == pytest.approx(legacy["drawdown_mag"])
    if legacy["auto_healed"]:
        # The heal still fires (tier semantics untouched) but says what it did NOT do.
        assert result["auto_healed"] is True
        assert "Auto-healed HWM reset to current NAV" in result["heal_reason"]
        assert "HWM GUARD: reset BLOCKED" in result["heal_reason"]


def test_ineligible_nav_cannot_advance_the_peak_either():
    """The advance branch (`current_nav > effective_peak`) is refused, not clamped.

    Under the guard a barred NAV ABOVE the frozen peak is "no drawdown" for the ladder
    (review finding): `drawdown_mag` clamps to 0.0 so a phantom gap cannot trip a tier,
    while the raw gap stays visible in `hwm_barred_gap_pct` and `drawdown_pct`. With the
    old `abs(current_dd)` the 10% gap would have read as TIER_1 damping against a book
    that is actually 10% above its mark.
    """
    current_nav, peak_nav, state = _ELIGIBLE_SCENARIOS["peak_advance"]
    legacy = _legacy_inspect_portfolio_drawdown(current_nav, peak_nav, dict(state))
    assert legacy["peak_nav"] == pytest.approx(current_nav)  # the defect the guard blocks

    result = health_mod.inspect_portfolio_drawdown(
        current_nav=current_nav,
        peak_nav=peak_nav,
        state=dict(state),
        params={},
        nav_eligible=False,
        ineligibility_reason="cash_as_of=2026-11-04 != as_of=2026-11-05",
    )

    assert result["peak_nav"] == pytest.approx(100000.0)
    assert result["hwm_guard_applied"] is True
    # The raw signed gap is still reported ...
    assert result["drawdown_pct"] == pytest.approx(0.10)
    assert result["hwm_barred_gap_pct"] == pytest.approx(0.10)
    # ... but it is NOT a drawdown for the ladder.
    assert result["drawdown_mag"] == pytest.approx(0.0)
    assert legacy["tier"] == "NORMAL"
    assert result["tier"] == "NORMAL"
    assert result["linear_equity_scale"] == pytest.approx(1.0)


def test_ineligible_nav_above_the_peak_reports_a_positive_drawdown_not_a_new_mark():
    """The frozen peak makes an above-peak NAV read as a POSITIVE gap, never a raise.

    PREVENTS: silently clamping the *reported* `drawdown_pct` to 0.0 so the report looks
    tidy -- the raw gap must stay visible (`drawdown_pct` + `hwm_barred_gap_pct`), it is
    only the tier-ladder magnitude (`drawdown_mag`) that is clamped.
    """
    result = health_mod.inspect_portfolio_drawdown(
        current_nav=110000.0,
        peak_nav=None,
        state={"peak_nav": 100000.0},
        params={},
        nav_eligible=False,
        ineligibility_reason="legs span two instants",
    )

    assert result["peak_nav"] == pytest.approx(100000.0)
    assert result["drawdown_pct"] == pytest.approx(0.10)
    assert result["hwm_barred_gap_pct"] == pytest.approx(0.10)
    assert result["drawdown_mag"] == pytest.approx(0.0)
    assert result["hwm_guard_applied"] is True


def test_barred_phantom_well_above_the_peak_cannot_trip_the_tier_ladder():
    """THE REVIEW FINDING: a barred NAV 20% above a frozen peak is not a 20% drawdown.

    PREVENTS: `abs(current_dd)` feeding the ladder, which turned a healthy book (NAV up
    20% on a mark it may not raise) into `TIER_3_HALT` -- a 100% cash order produced by a
    phantom. The guard de-escalates the ladder and keeps the gap as a separate number.
    """
    result = health_mod.inspect_portfolio_drawdown(
        current_nav=120000.0,
        peak_nav=None,
        state={"peak_nav": 100000.0},
        params={},
        nav_eligible=False,
        ineligibility_reason="cash_as_of=2026-11-04 != as_of=2026-11-05",
    )

    assert result["drawdown_pct"] == pytest.approx(0.20)
    assert result["hwm_barred_gap_pct"] == pytest.approx(0.20)
    assert result["drawdown_mag"] == pytest.approx(0.0)
    assert result["tier"] == "NORMAL"
    assert result["linear_equity_scale"] == pytest.approx(1.0)
    assert result["freeze_remaining_bars"] == 0
    assert result["hwm_guard_applied"] is True
    # The two dead-band neighbours still degrade normally (below the peak => same as before)
    below = health_mod.inspect_portfolio_drawdown(
        current_nav=79000.0,
        peak_nav=None,
        state={"peak_nav": 100000.0},
        params={},
        nav_eligible=False,
        ineligibility_reason="legs span two instants",
    )
    assert below["drawdown_mag"] == pytest.approx(0.21)
    assert below["hwm_barred_gap_pct"] == pytest.approx(0.0)
    assert below["tier"] == "TIER_3_HALT"


def test_initialisation_with_no_prior_peak_is_reported_but_not_flagged_as_blocked():
    """With no mark at all there is nothing to protect: record the source, not a block.

    `peak_nav_source="current_nav"` is what tells a reader the mark came from this
    round -- the guard cannot invent an earlier peak, and claiming `hwm_guard_applied`
    here would overstate what it did.
    """
    result = health_mod.inspect_portfolio_drawdown(
        current_nav=123456.0,
        peak_nav=None,
        state={},
        params={},
        nav_eligible=False,
        ineligibility_reason="nav_breakdown is not a mapping",
    )

    assert result["peak_nav"] == pytest.approx(123456.0)
    assert result["peak_nav_source"] == "current_nav"
    assert result["hwm_eligible"] is False
    assert result["hwm_guard_applied"] is False


def test_peak_nav_source_truth_table():
    """`cli` / `prior_state` / `current_nav` -- the ACTUAL source of the reported mark."""
    cases = [
        # (current_nav, peak_nav, state, expected source)
        (95000.0, 120000.0, {"peak_nav": 100000.0}, "cli"),
        (95000.0, None, {"peak_nav": 100000.0}, "prior_state"),
        (110000.0, None, {"peak_nav": 100000.0}, "current_nav"),
        (110000.0, None, {}, "current_nav"),
        (110000.0, None, {"peak_nav": 0}, "current_nav"),
    ]
    for current_nav, peak_nav, state, expected in cases:
        result = health_mod.inspect_portfolio_drawdown(
            current_nav=current_nav,
            peak_nav=peak_nav,
            state=dict(state),
            params={},
        )
        assert result["peak_nav_source"] == expected, (current_nav, peak_nav, state)


def test_default_call_omits_no_evidence_as_a_verdict():
    """An unspecified `nav_eligible` must stay `None` (not `True`) in the report."""
    result = health_mod.inspect_portfolio_drawdown(100000.0, None, {}, {})
    assert result["hwm_eligible"] is None
    assert result["hwm_ineligibility_reason"] is None
    assert result["hwm_guard_applied"] is False


# --------------------------------------------------------------------------- #
# 5b. NV13 -- PEAK PROVENANCE rendering (a label, never a gate)
#
# The phantom 208830.78 entered the permanent HWM with NOTHING in either artefact saying
# the mark had no leg evidence behind it. These tests pin the `[WARN]` line to the TWO
# labels that require it -- `unverified_inherited` (evidence contradicts the peak) and
# `unverifiable` (the run could NOT check the peak; review finding, HIGH) -- and to BOTH
# render sites, so "clean output" can never again be mistaken for "the engine vouched for
# the peak".
# --------------------------------------------------------------------------- #
def _provenance_dd(label, *, peak=208830.78, max_leg=202483.94):
    dd = _dd_info()
    dd.update(
        {
            "peak_nav": peak,
            "peak_nav_source": "prior_state",
            "peak_provenance": label,
            "peak_provenance_detail": (
                f"peak {peak:,.2f} vs highest leg-verified NAV {max_leg:,.2f}"
            ),
            "max_leg_verified_nav": max_leg,
        }
    )
    return dd


def test_peak_provenance_warn_renders_once_in_dashboard_and_markdown(tmp_path, capsys):
    unverified = _provenance_dd("unverified_inherited")

    health_mod.print_dashboard(**_dashboard_kwargs(_vol_info(), dd_info=unverified))
    out = capsys.readouterr().out
    warn = [ln for ln in out.splitlines() if "[WARN] 峰值无出处" in ln]
    assert len(warn) == 1, out
    assert "208,830.78" in warn[0] and "202,483.94" in warn[0]
    assert "0.50%" in warn[0]
    assert "--peak-nav" in warn[0] and "--require-provable-peak" in warn[0]
    # the line explains that the ladder did NOT move -- it is a disclosure, not a gate
    assert "默认行为不变" in warn[0]

    kwargs = _dashboard_kwargs(_vol_info(), dd_info=unverified)
    kwargs.pop("universe_path")
    kwargs.pop("strategy_path")
    _json_path, md_path = health_mod.generate_reports(output_dir=str(tmp_path), **kwargs)
    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()
    assert len([ln for ln in md.splitlines() if "[WARN] 峰值无出处" in ln]) == 1, md

    # The rendered HWM/drawdown rows are unchanged by the label (byte-identical numbers).
    assert "**High-Water Mark (HWM)** | `208,830.78`" in md
    assert "**Peak-to-Trough Drawdown** | `-5.00%`" in md


def test_unverifiable_peak_warns_that_the_run_could_not_verify_it_in_both_renderers(
    tmp_path, capsys
):
    """FINDING (HIGH): a peak nothing could check is DISCLOSED, in both artefacts.

    `unverifiable` (peak present, no leg-verified series -- non-archive `--output-dir`, a
    failed archive scan, an empty tree) used to share the `missing` label and therefore
    printed nothing at all: the one deployment that cannot verify a peak was also the one
    that said nothing about it. The wording must claim only what the evidence supports --
    the run could not check the peak, NOT that the peak is wrong -- and the numbers the
    ladder used must stay byte-identical.
    """
    unverifiable = _provenance_dd("unverifiable", max_leg=202483.94)
    unverifiable["max_leg_verified_nav"] = None
    unverifiable["peak_provenance_detail"] = (
        "peak 208,830.78 is present and in use, but no leg-verified NAV was supplied, so "
        "this run cannot verify OR refute it (unverifiable -- the run could NOT check the "
        "peak; that is not a claim that the peak is wrong)"
    )

    health_mod.print_dashboard(**_dashboard_kwargs(_vol_info(), dd_info=unverifiable))
    out = capsys.readouterr().out
    warn = [ln for ln in out.splitlines() if "[WARN] 峰值无法校验" in ln]
    assert len(warn) == 1, out
    assert "208,830.78" in warn[0]
    assert "峰值无法校验" in warn[0] and "unverifiable" in warn[0]
    assert "无法证明、也无法否证" in warn[0]      # could neither verify nor refute
    assert "这不表示峰值有误" in warn[0]          # explicitly NOT an accusation
    assert "--require-provable-peak" in warn[0]
    assert "峰值无出处" not in out                 # not the phantom-provenance claim

    kwargs = _dashboard_kwargs(_vol_info(), dd_info=unverifiable)
    kwargs.pop("universe_path")
    kwargs.pop("strategy_path")
    _json_path, md_path = health_mod.generate_reports(output_dir=str(tmp_path), **kwargs)
    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()
    assert len([ln for ln in md.splitlines() if "[WARN] 峰值无法校验" in ln]) == 1, md
    # the disclosure does not move the reported numbers
    assert "**High-Water Mark (HWM)** | `208,830.78`" in md
    assert "**Peak-to-Trough Drawdown** | `-5.00%`" in md

    # A run with NO usable peak at all (`missing`) stays silent: nothing to disclose.
    assert health_mod.format_peak_provenance_warning(_provenance_dd("missing")) is None


def test_peak_provenance_warn_is_silent_for_every_other_label(tmp_path, capsys):
    """`missing` is silent ON PURPOSE: with no usable peak, nothing is being divided by."""
    for label in ("leg_verified", "attributable_external", "missing"):
        dd = _provenance_dd(label)
        assert health_mod.format_peak_provenance_warning(dd) is None

        health_mod.print_dashboard(**_dashboard_kwargs(_vol_info(), dd_info=dd))
        out = capsys.readouterr().out
        assert "峰值无出处" not in out, label

        kwargs = _dashboard_kwargs(_vol_info(), dd_info=dd)
        kwargs.pop("universe_path")
        kwargs.pop("strategy_path")
        _json_path, md_path = health_mod.generate_reports(
            output_dir=str(tmp_path / label), **kwargs
        )
        with open(md_path, "r", encoding="utf-8") as f:
            md = f.read()
        assert "峰值无出处" not in md, label

    # No peak-evidence keys at all (an older dict) stays silent too -- never a KeyError.
    assert health_mod.format_peak_provenance_warning(_dd_info()) is None
    assert health_mod.format_peak_provenance_warning(None) is None


def test_hwm_guard_warning_renders_once_in_dashboard_and_markdown(tmp_path, capsys):
    """One `[WARN]` line in each artefact, naming the reason and the both-directions bar."""
    guarded = _dd_info()
    guarded.update(
        {
            "hwm_eligible": False,
            "hwm_ineligibility_reason": "cash_as_of=2026-11-04 != as_of=2026-11-05",
            "peak_nav_source": "prior_state",
            "hwm_guard_applied": True,
        }
    )

    health_mod.print_dashboard(**_dashboard_kwargs(_vol_info(), dd_info=guarded))
    out = capsys.readouterr().out
    warn = [ln for ln in out.splitlines() if "[WARN] HWM 守卫" in ln]
    assert len(warn) == 1, out
    assert "cash_as_of=2026-11-04" in warn[0]
    assert "既未上抬也未下修" in warn[0]

    kwargs = _dashboard_kwargs(_vol_info(), dd_info=guarded)
    kwargs.pop("universe_path")
    kwargs.pop("strategy_path")
    _json_path, md_path = health_mod.generate_reports(output_dir=str(tmp_path), **kwargs)
    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()
    md_warn = [ln for ln in md.splitlines() if "[WARN] HWM 守卫" in ln]
    assert len(md_warn) == 1, md

    # An ungated round prints NOTHING (the pre-existing reports stay byte-identical).
    assert health_mod.format_hwm_guard_warning(_dd_info()) is None
    health_mod.print_dashboard(**_dashboard_kwargs(_vol_info()))
    assert "[WARN] HWM 守卫" not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# 6. `advisory.r1` carries the trough basis (Stage-2 consumer)
# --------------------------------------------------------------------------- #
def test_r1_decision_dict_emits_the_trough_basis_and_preserves_the_verdict():
    """`dd_source` names the field; `dd_trough_basis` now names the QUANTITY.

    PREVENTS: a basis added by re-deriving the verdict -- `status` / `multiplier` /
    `drawdown` (and the advisory fence) must come through untouched.
    """
    import common.advisory_report as advisory_report
    from common.intraday_drawdown_derisk import evaluate_intraday_drawdown

    decision = evaluate_intraday_drawdown(hwm_intra=100.0, dd_trough=-0.03)
    out = advisory_report.r1_decision_to_dict(decision)

    assert out["dd_source"] == "dd_trough"
    assert out["dd_trough_basis"] == "daily_hwm_drawdown"
    assert out["dd_trough_basis"] == health_mod.DD_TROUGH_BASIS_DAILY_HWM
    assert out["advisory"] is True and out["gates_orders"] is False
    # The verdict itself is byte-identical to the decision.
    assert out["status"] == decision.status
    assert out["multiplier"] == pytest.approx(decision.multiplier)
    assert out["drawdown"] == pytest.approx(decision.drawdown)

    # Explicit keyword wins; a basis attribute on the decision is honoured next.
    assert (
        advisory_report.r1_decision_to_dict(decision, dd_trough_basis="intraday_ew_proxy")[
            "dd_trough_basis"
        ]
        == "intraday_ew_proxy"
    )

    class _WithBasis:
        status = "NONE"
        multiplier = 1.0
        drawdown = -0.01
        reason = "r"
        dd_source = "dd_trough"
        dd_trough_basis = "explicit_attr"

    assert advisory_report.r1_decision_to_dict(_WithBasis())["dd_trough_basis"] == "explicit_attr"

    class _Bare:
        status = "NONE"
        multiplier = 1.0
        drawdown = None
        reason = "r"
        dd_source = "none"

    bare = advisory_report.r1_decision_to_dict(_Bare())
    assert bare["dd_trough_basis"] == "daily_hwm_drawdown"  # default, never absent
    assert bare["drawdown"] is None  # unmeasurable stays null


def test_advisory_block_keeps_its_fence_with_the_basis_present():
    import common.advisory_report as advisory_report

    class _Decision:
        status = "MEDIUM"
        multiplier = 0.5
        drawdown = -0.035
        reason = "r"
        dd_source = "dd_trough"

    block = advisory_report.build_advisory_block(
        phase=advisory_report.ADVISORY_PHASE_P2,
        as_of="2026-11-16",
        r1=_Decision(),
        real_buy_scale=0.5,
    )
    assert block["advisory"] is True and block["gates_orders"] is False
    assert block["r1_effective"] is False
    assert block["r1"]["dd_trough_basis"] == "daily_hwm_drawdown"
    assert block["r1"]["multiplier"] == pytest.approx(0.5)
    assert block["shadow_buy_scale"] == pytest.approx(0.25)


def test_hwm_eligibility_mirror_and_wiring_are_present_in_the_source():
    """STATIC: the guard is WIRED (not merely defined) and its constants cannot drift."""
    from common.nav_hwm import hwm_eligibility

    # The eligibility verdict of the 2026-11-05 shape is False, of a clean one True.
    assert hwm_eligibility({"as_of": "2026-11-05", "cash_as_of": "2026-11-04"})[0] is False
    assert hwm_eligibility({"as_of": "2026-11-05", "cash_as_of": "2026-11-05"})[0] is True

    path = os.path.join(_REPO_ROOT, "scripts", "check_live_portfolio_health.py")
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "from common.nav_hwm import hwm_eligibility" in source
    assert "nav_eligible=nav_eligible" in source
    assert "ineligibility_reason=nav_ineligibility_reason" in source
    # The peak may not be advanced or healed without consulting the guard.
    assert source.count("hwm_guard_active") >= 5
