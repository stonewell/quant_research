"""Offline regression suite for Defect D7 -- "stale risk input / silent degradation".

Two archived facts (read-only audit of `chan_four_state_blend`, universe
`custom_astock_202609_20_new`, 2026-11-09/10/11 rounds) motivate this file:

1. `stage1_health_report.json::volatility_targeting.realized_vol_21d` was
   ``0.3240854096084951`` -- identical to 16 decimal places -- on all three rounds,
   with `vol_scaling_factor` frozen at ``0.370273``, while `breadth` (0.6/0.5/0.55)
   and `thrust` (0.5/0.4/0.35) changed every day and every round passed the daily
   freshness gate (20/20 bars on the as-of session).

   Root cause (frozen): the 5m downside-semivariance branch in
   `scripts/check_live_portfolio_health.py::evaluate_realized_volatility` reads the
   `RS_Down_5m` column joined onto the DAILY frame and then does
   ``.dropna().iloc[-21:]``. `dropna()` makes the window end at the last NON-NaN
   date, not at `as_of`. The 5m cache stopped on 2026-11-09 (`intraday_ohlcv_bars`
   max ts = 2026-11-09 15:00, 48 rows x 20 symbols), so the 11-10 and 11-11 windows
   re-used exactly the same 21 observations ending 11-09 and OVERWROTE the correctly
   recomputed as-of Garman-Klass value. The window endpoint was not in the report,
   so the only visible symptom was a repeated float.

2. `observability.intraday_5m` reported `symbols_measured=0` with
   "常见原因: provider does not support 5m" next to `enabled=true` and
   `symbols_5m_available=20`. That is a stale 5m cache, not a provider capability
   gap (the 2026-11-09 round measured all 20 symbols from the same cache).

What this suite pins, 100% offline (synthetic OHLC/5m bars, no network):

* the as-of window END is measured and reported (`vol_window_start/end`, `vol_n_obs`,
  `vol_input_bars_last_date`, `vol_input_lag_days`);
* different `as_of` values with different window ends produce DIFFERENT
  `realized_vol_21d` (the defect's discriminating assertion);
* a 5m window that does not reach `as_of` is REJECTED, never applied
  (`vol_intraday_input_stale` / `vol_intraday_rejected_reason`), and two `as_of`
  values called alternately cannot leak one window into the other (there is no
  cross-call cache, and the report proves it by echoing each call's own window);
* fewer than 21 observations degrades EXPLICITLY: `realized_vol_21d is None`,
  `vol_scaling_fallback="insufficient_history"`, neutral `1.0x` scaling and a
  machine-readable reason code -- never `target_vol` dressed up as a measurement;
* the 5m coverage classifier distinguishes `provider_unsupported` from
  `degraded_stale_intraday_cache`, and `enabled` follows the web layer's contract
  (real rows on the as-of session) so `requested=true` cannot read as "measured".
"""

import importlib.util
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _load_script(module_name, relative_path):
    """Loads a standalone script (not an importable package module) via importlib."""
    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load_script(
    "check_live_portfolio_health_d7", os.path.join("scripts", "check_live_portfolio_health.py")
)

from common.intraday_features import (  # noqa: E402  (needs repo root on sys.path first)
    IntradayFeatureConfig,
    enrich_universe_with_intraday_features,
)

_PARAMS = {"cfsb_target_vol": 0.12}


def _ohlc_frame(
    periods: int,
    *,
    start: str = "2026-01-01",
    seed: int = 7,
    drift: float = 0.0,
    vol: float = 0.012,
) -> pd.DataFrame:
    """Synthetic OHLC bars with a non-degenerate high/low so Garman-Klass applies."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=periods)
    rets = rng.normal(drift, vol, size=periods)
    close = 100.0 * np.exp(np.cumsum(rets))
    # A wide-but-deterministic intraday range keeps the estimator away from the
    # `high == low` degenerate branch (which would fall back to close-to-close).
    spread = np.abs(rng.normal(0.006, 0.001, size=periods)) + 0.002
    high = close * (1.0 + spread)
    low = close * (1.0 - spread)
    open_ = close * (1.0 + rng.normal(0.0, 0.002, size=periods))
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close},
        index=idx,
    )


def _intraday_features_block(rs_down: pd.Series) -> pd.DataFrame:
    """Daily-indexed frame carrying the 5m feature columns the vol code consumes."""
    return pd.DataFrame(
        {
            "RS_Down_5m": rs_down,
            "Signed_Jump_5m": pd.Series(0.01, index=rs_down.index),
        },
        index=rs_down.index,
    )


# ---------------------------------------------------------------------------
# 1. Same universe, different as_of -> different realized vol + explicit window
# ---------------------------------------------------------------------------


def test_realized_vol_is_recomputed_per_as_of_and_reports_its_window():
    """DISCRIMINATING: a frozen/constant estimate cannot pass this test.

    Two as_of values five sessions apart must yield different 21-day windows and
    therefore different realized volatility, and each result must name the window it
    used (end == as_of, lag 0, 21 observations).
    """
    df = _ohlc_frame(120)
    universe = {"STOCK_A": df}

    late = _mod.evaluate_realized_volatility(universe, _PARAMS, as_of=df.index[-1])
    early = _mod.evaluate_realized_volatility(universe, _PARAMS, as_of=df.index[-6])

    assert late["realized_vol_21d"] is not None
    assert early["realized_vol_21d"] is not None
    assert late["realized_vol_21d"] != early["realized_vol_21d"], (
        "same universe, different as_of -> the 21-day window ends differ, so the "
        "estimate must differ (this is exactly what the archived 11-09/10/11 rounds "
        "failed: 0.3240854096084951 on all three)"
    )

    for res, as_of in ((late, df.index[-1]), (early, df.index[-6])):
        assert res["vol_as_of"] == as_of.date().isoformat()
        assert res["vol_window_end"] == as_of.date().isoformat()
        assert res["vol_input_bars_last_date"] == as_of.date().isoformat()
        assert res["vol_input_lag_days"] == 0
        assert res["vol_input_stale"] is False
        assert res["vol_n_obs"] == _mod.VOL_MIN_OBS == 21
        assert res["vol_input_source"] == "garman_klass_ohlc"
        assert res["vol_degraded"] is False
        assert res["vol_scaling_fallback"] is None
        # Window start is exactly 21 bars before the as_of bar.
        start_pos = df.index.get_loc(pd.Timestamp(as_of)) - 20
        assert res["vol_window_start"] == df.index[start_pos].date().isoformat()


# ---------------------------------------------------------------------------
# 2. Insufficient history -> explicit degradation, neutral scaling, no fabrication
# ---------------------------------------------------------------------------


def test_insufficient_history_degrades_explicitly_instead_of_fabricating_a_number():
    """< 21 bars must NOT yield a number (the old code fell back to `target_vol`)."""
    df = _ohlc_frame(12)
    res = _mod.evaluate_realized_volatility({"STOCK_A": df}, _PARAMS, as_of=df.index[-1])

    assert res["realized_vol_21d"] is None
    assert res["vol_degraded"] is True
    assert res["vol_degraded_reason_code"] == "insufficient_history"
    assert res["vol_scaling_fallback"] == "insufficient_history"
    assert res["vol_scaling_factor"] == 1.0  # neutral stand-in, labelled as such
    assert res["vol_elevated"] is False
    assert res["vol_model"] == "Unavailable (insufficient history)"
    assert res["vol_input_source"] == "unavailable"
    assert res["vol_n_obs"] is None
    assert "21" in (res["vol_degraded_reason"] or "")
    assert "NULL" in (res["vol_degraded_reason"] or "")
    assert res["vol_symbols_used"] == 0
    assert res["vol_symbols_skipped"] == ["STOCK_A"]


def test_close_to_close_also_requires_21_returns():
    """A Close-only frame needs 22 prices (21 returns); 21 prices is not enough."""
    idx = pd.bdate_range("2026-01-01", periods=21)
    df = pd.DataFrame({"Close": 100.0 * (1.001 ** np.arange(21))}, index=idx)
    res = _mod.evaluate_realized_volatility({"STOCK_A": df}, _PARAMS, as_of=idx[-1])
    assert res["realized_vol_21d"] is None
    assert res["vol_degraded_reason_code"] == "insufficient_history"

    idx22 = pd.bdate_range("2026-01-01", periods=22)
    df22 = pd.DataFrame({"Close": 100.0 * (1.001 ** np.arange(22))}, index=idx22)
    res22 = _mod.evaluate_realized_volatility({"STOCK_A": df22}, _PARAMS, as_of=idx22[-1])
    assert res22["realized_vol_21d"] is not None
    assert res22["vol_n_obs"] == 21
    assert res22["vol_input_source"] == "close_to_close"


# ---------------------------------------------------------------------------
# 3. A stale 5m window is rejected -- and cannot leak across as_of calls
# ---------------------------------------------------------------------------


def _stale_intraday_case():
    """Daily bars through D, 5m features whose last non-NaN row is D-3 sessions."""
    df = _ohlc_frame(80)
    idx = df.index
    rs = pd.Series(0.0004, index=idx)
    # The intraday cache stopped three sessions before the last daily bar: THIS is
    # the 2026-11-09/11-10/11-11 archive shape (NaN rows hide a frozen window).
    rs.iloc[-3:] = np.nan
    features = _intraday_features_block(rs)
    universe = {"STOCK_A": pd.concat([df, features], axis=1)}
    return universe, idx


def test_stale_intraday_window_is_rejected_not_applied():
    """THE D7 DEFECT: a 5m window ending before as_of must never override the daily value."""
    universe, idx = _stale_intraday_case()
    as_of = idx[-1]
    res = _mod.evaluate_realized_volatility(universe, _PARAMS, as_of=as_of)

    assert res["vol_intraday_input_stale"] is True
    assert res["vol_intraday_used"] is False
    assert res["vol_intraday_window_end"] == idx[-4].date().isoformat()
    assert res["vol_intraday_lag_days"] == int((as_of - idx[-4]).days)
    assert res["vol_intraday_rejected_reason"], "rejection must be recorded, not silent"
    # The estimate comes from the as-of DAILY window, not from the frozen 5m one.
    assert res["vol_input_source"] == "garman_klass_ohlc"
    assert res["vol_window_end"] == as_of.date().isoformat()
    assert res["vol_degraded"] is False

    # The rejected value must differ from what the stale 5m window would have produced.
    stale_down = float(np.sqrt(252.0 * 2.0 * pd.Series(0.0004, index=idx).iloc[-24:-3].tail(21).mean()))
    assert res["realized_vol_21d"] != pytest.approx(stale_down)


def test_alternating_as_of_calls_do_not_leak_each_others_window():
    """No cross-call cache: each call re-derives its own window (cache key would need as_of)."""
    universe, idx = _stale_intraday_case()

    first = _mod.evaluate_realized_volatility(universe, _PARAMS, as_of=idx[-1])
    # D-3: the 5m window now REACHES as_of, so the downside branch is legitimately used.
    second = _mod.evaluate_realized_volatility(universe, _PARAMS, as_of=idx[-4])
    third = _mod.evaluate_realized_volatility(universe, _PARAMS, as_of=idx[-1])

    assert first["vol_intraday_input_stale"] is True
    assert second["vol_intraday_input_stale"] is False
    assert second["vol_intraday_used"] is True
    assert second["vol_input_source"] == "downside_semivariance_5m"
    assert second["vol_intraday_window_end"] == idx[-4].date().isoformat()
    # The reported window describes the input that produced the number: the 5m one.
    assert second["vol_window_end"] == idx[-4].date().isoformat()
    assert second["vol_input_bars_last_date"] == idx[-4].date().isoformat()
    assert second["vol_n_obs"] == 21
    assert first["realized_vol_21d"] != second["realized_vol_21d"]
    # Repeating the first call reproduces it exactly -- nothing was memoized under the
    # wrong key, and the second call did not contaminate it.
    assert third["realized_vol_21d"] == first["realized_vol_21d"]
    assert third["vol_intraday_window_end"] == first["vol_intraday_window_end"]
    assert third["vol_window_end"] == idx[-1].date().isoformat()


def test_short_intraday_history_is_not_labelled_a_21_day_estimate():
    """Only 16 usable 5m sessions must NOT be reported as a 21-day semivariance.

    This is the second archived finding: the BaoStock 5m cache starts 2026-10-19, so the
    2026-11-09 round's 0.3240854096084951 was a 16-observation semivariance labelled
    "21d". Eligibility now requires VOL_MIN_OBS even though the window reaches as_of.
    """
    df = _ohlc_frame(40)
    idx = df.index
    rs = pd.Series(np.nan, index=idx)
    rs.iloc[-16:] = 0.0004  # only 16 sessions of 5m features exist
    universe = {"STOCK_A": pd.concat([df, _intraday_features_block(rs)], axis=1)}

    res = _mod.evaluate_realized_volatility(universe, _PARAMS, as_of=idx[-1])

    assert res["vol_intraday_eligible"] is False
    assert res["vol_intraday_used"] is False
    assert res["vol_intraday_available_obs"] == 16
    assert res["vol_intraday_n_obs"] is None
    assert res["vol_intraday_window_end"] == idx[-1].date().isoformat()
    assert res["vol_intraday_input_stale"] is False
    assert "16" in (res["vol_intraday_rejected_reason"] or "")
    assert res["vol_input_source"] == "garman_klass_ohlc"
    assert res["vol_n_obs"] == 21
    assert res["vol_window_end"] == idx[-1].date().isoformat()


def test_real_enriched_universe_keeps_the_intraday_branch_when_fresh():
    """End-to-end through `enrich_universe_with_intraday_features` (the live join path)."""
    df = _ohlc_frame(60)
    idx = df.index
    # 5m bars for every session, 36 bars/day at <= 14:00, so features exist for as_of.
    # A per-bar random walk (not a flat bar) keeps `RS_Down_5m` non-zero -- a constant
    # frame yields rs_down == 0 and the estimator would (correctly) skip it.
    rng = np.random.default_rng(11)
    rows = []
    for d in idx:
        price = 100.0
        for bar in range(36):
            ts = d + pd.Timedelta(hours=9, minutes=35) + pd.Timedelta(minutes=5 * bar)
            open_ = price
            close = price * float(np.exp(rng.normal(0.0, 0.002)))
            rows.append(
                (ts, open_, max(open_, close) * 1.001, min(open_, close) * 0.999, close, 1_000.0)
            )
            price = close
    u5 = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"]).set_index("ts")
    universe_5m = {"STOCK_A": u5}

    enriched = enrich_universe_with_intraday_features(
        {"STOCK_A": df},
        universe_5m,
        config=IntradayFeatureConfig(cutoff_time="14:00"),
        synthesize_intraday_bar=True,
    )
    assert "RS_Down_5m" in enriched["STOCK_A"].columns

    res = _mod.evaluate_realized_volatility(enriched, _PARAMS, as_of=idx[-1])
    assert res["vol_intraday_input_stale"] is False
    assert res["vol_intraday_used"] is True
    assert res["vol_intraday_window_end"] == idx[-1].date().isoformat()
    assert res["vol_intraday_n_obs"] == 21
    assert res["vol_window_end"] == idx[-1].date().isoformat()


# ---------------------------------------------------------------------------
# 4. 5m coverage status: "provider unsupported" vs "stale cache" are NOT the same
# ---------------------------------------------------------------------------


def _bars_index(days):
    return pd.DatetimeIndex(
        [pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=35) for d in days]
    )


def test_intraday_5m_status_provider_unsupported_when_no_frames():
    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    universe_5m = {"A": empty, "B": empty}
    coverage = _mod.probe_intraday_5m_cache_coverage(universe_5m, "2026-11-11")

    assert coverage["rows_5m"] == 0
    assert coverage["intraday_data_last_bar_date"] is None
    assert coverage["symbols_with_rows"] == []

    status = _mod.classify_intraday_5m_status(
        coverage, n_5m_symbols_nonempty=0, features_measured=0
    )
    assert status["status"] == "provider_unsupported"
    assert status["degraded"] is True
    assert status["degraded_reason_code"] == "intraday_5m_unavailable"
    # `enabled` follows the web contract: real rows on the as-of session only.
    assert status["enabled"] is False
    assert status["requested"] is True
    assert status["generated_at"]


def test_intraday_5m_status_stale_cache_is_not_provider_unsupported():
    """THE FALSE DIAGNOSIS: 20 non-empty but STALE frames != "provider does not support 5m"."""
    frame = pd.DataFrame(
        {"Open": 1.0, "High": 1.1, "Low": 0.9, "Close": 1.0, "Volume": 100.0},
        index=_bars_index(["2026-11-06", "2026-11-09"]),
    )
    universe_5m = {s: frame.copy() for s in ("A", "B", "C")}
    coverage = _mod.probe_intraday_5m_cache_coverage(universe_5m, "2026-11-11")

    assert coverage["rows_5m"] == 0  # no rows ON 2026-11-11
    assert coverage["intraday_data_last_bar_date"] == "2026-11-09"
    assert coverage["intraday_lag_calendar_days"] == 2
    assert coverage["intraday_data_stale"] is True
    assert sorted(coverage["symbols_unavailable"]) == ["A", "B", "C"]

    status = _mod.classify_intraday_5m_status(
        coverage, n_5m_symbols_nonempty=3, features_measured=0
    )
    assert status["status"] == "degraded_stale_intraday_cache"
    assert status["degraded_reason_code"] == "intraday_5m_stale_window"
    assert status["degraded"] is True
    assert status["enabled"] is False


def test_intraday_5m_status_evaluated_when_features_were_measured():
    frame = pd.DataFrame(
        {"Open": 1.0, "High": 1.1, "Low": 0.9, "Close": 1.0, "Volume": 100.0},
        index=_bars_index(["2026-11-10", "2026-11-11"]),
    )
    universe_5m = {"A": frame}
    coverage = _mod.probe_intraday_5m_cache_coverage(universe_5m, "2026-11-11")
    assert coverage["rows_5m"] == 1
    assert coverage["intraday_data_stale"] is False

    status = _mod.classify_intraday_5m_status(
        coverage, n_5m_symbols_nonempty=1, features_measured=1
    )
    assert status["status"] == "evaluated"
    assert status["degraded"] is False
    assert status["degraded_reason_code"] is None
    assert status["enabled"] is True


# ---------------------------------------------------------------------------
# 5. Downstream stays alive: a degraded factor must not crash the gate
# ---------------------------------------------------------------------------


def _gate_inputs():
    dd_info = {
        "tier": "NORMAL",
        "tier_label": "NORMAL",
        "fast_recovery_nav": False,
        "freeze_remaining_bars": 0,
        "linear_equity_scale": 1.0,
        "current_nav": 100_000.0,
        "peak_nav": 100_000.0,
        "drawdown_pct": 0.0,
    }
    macro_info = {
        "thrust_active": False,
        "thrust": 0.3,
        "thrust_thresh": 0.6,
        "target_exposure": 0.55,
        "breadth_active": True,
        "breadth": 0.6,
        "breadth_thresh": 0.3,
        "leaders": [],
    }
    return dd_info, macro_info


def test_gate_directive_survives_a_degraded_volatility_factor():
    dd_info, macro_info = _gate_inputs()
    degraded = _mod.evaluate_realized_volatility(
        {"STOCK_A": _ohlc_frame(8)}, _PARAMS, as_of="2026-02-01"
    )
    code, headline, actions = _mod.resolve_gate_directive(dd_info, macro_info, degraded)

    assert code == "GO"
    assert any("Volatility Targeting UNAVAILABLE" in a for a in actions), actions
    assert any("insufficient_history" in a for a in actions)


def test_gate_directive_reports_a_stale_intraday_window_but_keeps_the_verdict():
    dd_info, macro_info = _gate_inputs()
    universe, idx = _stale_intraday_case()
    vol_info = _mod.evaluate_realized_volatility(universe, _PARAMS, as_of=idx[-1])
    code, _headline, actions = _mod.resolve_gate_directive(dd_info, macro_info, vol_info)

    assert code == "GO"
    assert any("5m 下行半方差窗口未采用" in a for a in actions), actions


# ---------------------------------------------------------------------------
# 6. (D7 follow-up) Provider provenance aggregation + session-freshness status
# ---------------------------------------------------------------------------
def _frame_with_attrs(attrs, ts="2026-11-09T15:00:00"):
    frame = pd.DataFrame(
        {"Open": 1.0, "High": 1.1, "Low": 0.9, "Close": 1.0, "Volume": 100.0},
        index=pd.DatetimeIndex([pd.Timestamp(ts)]),
    )
    frame.attrs.update(attrs)
    return frame


def _freshness(stale, lag, reason="stale_last_bar", session="2026-11-11", latest=None):
    return {
        "expected_last_session": session,
        "expected_deadline_ts": f"{session}T14:55:00",
        "latest_available_ts": latest,
        "stale": stale,
        "lag_calendar_days": lag,
        "reason": reason,
        "symbols": {},
        "stale_symbols": [],
        "fresh_symbols": [],
        "n_symbols": 0,
    }


def test_provenance_aggregates_the_fallback_chain_and_collapses_when_uniform():
    """A fuyao run served by BaoStock's cache must SAY so -- chain, reason and blast radius."""
    base = {
        "intraday_5m_available": True,
        "intraday_5m_source": "duckdb_cache",
        "intraday_5m_provider": "BaoStockDataProvider",
        "fallback_from": "FuyaoDataProvider",
        "fallback_to": "BaoStockDataProvider",
        "fallback_reason": "provider_unsupported_5m",
        "fallback_reason_detail": "fuyao has no 5m rows for this symbol",
    }
    universe_5m = {
        s: _frame_with_attrs(dict(base, **{"intraday_5m_latest_ts": f"2026-11-09T15:00:00"}))
        for s in ("A", "B", "C")
    }
    prov = _mod.summarize_intraday_5m_provenance(universe_5m)

    assert prov["provider"] == "BaoStockDataProvider"
    assert prov["source"] == "duckdb_cache"
    assert prov["latest_ts"] == "2026-11-09T15:00:00"
    assert prov["fallback_from"] == "FuyaoDataProvider"
    assert prov["fallback_to"] == "BaoStockDataProvider"
    assert prov["fallback_reason"] == "provider_unsupported_5m"
    assert prov["n_fallback_symbols"] == 3
    assert set(prov["provider_by_symbol"]) == {"A", "B", "C"}

    # No fallback at all -> every chain field is null (never an empty string).
    plain = _mod.summarize_intraday_5m_provenance(
        {
            "A": _frame_with_attrs(
                {
                    "intraday_5m_available": True,
                    "intraday_5m_source": "data_provider",
                    "intraday_5m_provider": "FuyaoDataProvider",
                    "intraday_5m_latest_ts": "2026-11-11T14:55:00",
                }
            )
        }
    )
    assert plain["provider"] == "FuyaoDataProvider"
    assert plain["fallback_from"] is None
    assert plain["fallback_to"] is None
    assert plain["fallback_reason"] is None
    assert plain["n_fallback_symbols"] == 0

    # Mixed providers -> the scalar is the majority, the disagreement stays visible.
    mixed = dict(universe_5m)
    mixed["D"] = _frame_with_attrs(
        {"intraday_5m_available": True, "intraday_5m_provider": "EastMoneyDataProvider"}
    )
    prov_mixed = _mod.summarize_intraday_5m_provenance(mixed)
    assert prov_mixed["provider"] == "BaoStockDataProvider"  # 3 of 4
    assert prov_mixed["provider_by_symbol"]["D"] == "EastMoneyDataProvider"


def test_stale_session_is_labelled_stale_cache_not_provider_unsupported():
    """`degraded_reason_code` must describe the LAYER (frozen cache), not the provider."""
    coverage = {
        "rows_5m": 0,
        "intraday_data_last_bar_date": "2026-11-09",
        "intraday_data_stale": True,
        "symbols_with_rows": [],
    }
    status = _mod.classify_intraday_5m_status(
        coverage,
        n_5m_symbols_nonempty=20,
        features_measured=0,
        freshness=_freshness(True, 2, latest="2026-11-09T15:00:00"),
    )
    assert status["status"] == "degraded_stale_intraday_cache"
    assert status["degraded_reason_code"] == "intraday_5m_stale_window"
    assert status["degraded_reason_code"] != "provider_unsupported_5m"
    assert status["freshness_reason"] == "stale_last_bar"
    assert status["enabled"] is False


def test_preclose_stale_bar_is_not_reported_as_a_frozen_cache():
    """`stale=True` with lag 0 == "closing bar not in yet", a normal intraday run."""
    coverage = {
        "rows_5m": 720,
        "intraday_data_last_bar_date": "2026-11-11",
        "intraday_data_stale": False,
        "symbols_with_rows": ["A"],
    }
    status = _mod.classify_intraday_5m_status(
        coverage,
        n_5m_symbols_nonempty=20,
        features_measured=0,
        freshness=_freshness(True, 0, latest="2026-11-11T13:55:00"),
    )
    assert status["status"] == "degraded_unmeasured_features"
    assert status["degraded_reason_code"] == "intraday_5m_features_unmeasured"
    assert status["enabled"] is True  # there ARE as-of rows


def test_no_5m_frames_at_all_is_unavailable_never_a_provider_verdict():
    coverage = {
        "rows_5m": 0,
        "intraday_data_last_bar_date": None,
        "intraday_data_stale": True,
        "symbols_with_rows": [],
    }
    status = _mod.classify_intraday_5m_status(
        coverage,
        n_5m_symbols_nonempty=0,
        features_measured=0,
        freshness=_freshness(True, None, reason="no_5m_bars"),
    )
    assert status["status"] == "provider_unsupported"
    assert status["degraded_reason_code"] == "intraday_5m_unavailable"
    assert status["degraded_reason_code"] != "provider_unsupported_5m"
    assert status["freshness_reason"] == "no_5m_bars"


def test_classify_is_deterministic_when_the_clock_is_injected(monkeypatch):
    """FLAKE REGRESSION: two calls with the SAME `now` must be byte-identical.

    The unpinned call used `datetime.now()`, which drifted ~0.5 s between two Stage-1 runs of
    identical inputs and broke `test_nav_source_parity`'s bit-identical contract.
    """
    coverage = {"rows_5m": 720, "intraday_data_last_bar_date": "2026-11-11"}
    fixed = datetime(2026, 11, 11, 6, 0, 0, tzinfo=timezone.utc)

    first = _mod.classify_intraday_5m_status(
        coverage, n_5m_symbols_nonempty=20, features_measured=20, now=fixed
    )
    second = _mod.classify_intraday_5m_status(
        coverage, n_5m_symbols_nonempty=20, features_measured=20, now=fixed
    )
    assert first == second
    assert first["generated_at"] == "2026-11-11T06:00:00+00:00"

    # The module override is the hook `main()` honours (a test cannot pass `now` through the
    # CLI), and an explicit `now` still wins over it.
    monkeypatch.setattr(_mod, "INTRADAY_5M_NOW_OVERRIDE", fixed)
    via_override = _mod.classify_intraday_5m_status(
        coverage, n_5m_symbols_nonempty=20, features_measured=20
    )
    assert via_override["generated_at"] == "2026-11-11T06:00:00+00:00"
    explicit = _mod.classify_intraday_5m_status(
        coverage,
        n_5m_symbols_nonempty=20,
        features_measured=20,
        now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    assert explicit["generated_at"] == "2026-01-02T03:04:05+00:00"
