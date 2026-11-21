"""Offline tests for MS9: 5m tri-state coverage audit, degradation trace, and
BaoStock sequential sync guardrails.

All tests run 100% offline using in-memory DuckDB and synthetic 5m bars.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.duckdb_cache import DuckDBCache
from common.intraday_coverage import (
    DUAL_MODE_DEGRADATION_POLICY,
    PURE_5M_DEGRADATION_POLICY,
    SESSION_STATE_BOUNDARY_MISMATCH,
    SESSION_STATE_COMPLETE,
    SESSION_STATE_MISSING_DAY,
    SESSION_STATE_SHORT_BARS,
    STATUS_FAIL,
    STATUS_INSUFFICIENT,
    STATUS_PASS,
    audit_universe_5m_tri_state,
    record_5m_degradation_trace,
)
from common.intraday_features import (
    IntradayFeatureConfig,
    IntradayIncompleteWarning,
    enrich_universe_with_intraday_features,
)
from scripts import sync_china_5m_and_indices as sync


def _make_session_5m(
    day: str,
    *,
    first: str = "09:35",
    last: str = "15:00",
) -> pd.DataFrame:
    morning = pd.date_range(f"{day} {first}:00", f"{day} 11:30:00", freq="5min")
    afternoon = pd.date_range(f"{day} 13:05:00", f"{day} {last}:00", freq="5min")
    idx = morning.append(afternoon)
    n = len(idx)
    return pd.DataFrame(
        {
            "Open": np.full(n, 100.0),
            "High": np.full(n, 101.0),
            "Low": np.full(n, 99.0),
            "Close": np.linspace(100.0, 100.8, n),
            "Volume": np.full(n, 1000.0),
        },
        index=idx,
    )


def test_tri_state_matrix_pass_fail_and_insufficient():
    days = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]

    sym_a = pd.concat([_make_session_5m(d) for d in days])

    # Sym B: missing Wed (11-11), Thu (11-12) truncated to 14:15 (39 bars < 45),
    # Fri (11-13) starts at 09:40 (47 bars, boundary mismatch & short)
    sym_b = pd.concat(
        [
            _make_session_5m("2026-11-09"),
            _make_session_5m("2026-11-10"),
            _make_session_5m("2026-11-12", last="14:15"),
            _make_session_5m("2026-11-13", first="09:40"),
        ]
    )

    report = audit_universe_5m_tri_state(
        {"SYM_A": sym_a, "SYM_B": sym_b, "SYM_C": pd.DataFrame(), "SYM_D": None},
        expected_sessions=days,
    )

    assert report["SYM_A"]["status"] == STATUS_PASS
    assert report["SYM_A"]["missing_days"] == []
    assert report["SYM_A"]["short_bar_days"] == {}
    assert report["SYM_A"]["boundary_mismatch_days"] == {}
    assert len(report["SYM_A"]["complete_days"]) == 5
    assert all(v == SESSION_STATE_COMPLETE for v in report["SYM_A"]["session_states"].values())

    assert report["SYM_B"]["status"] == STATUS_FAIL
    assert report["SYM_B"]["missing_days"] == ["2026-11-11"]
    assert report["SYM_B"]["short_bar_days"] == {"2026-11-12": 39, "2026-11-13": 47}
    assert "2026-11-12" in report["SYM_B"]["boundary_mismatch_days"]
    assert "2026-11-13" in report["SYM_B"]["boundary_mismatch_days"]
    assert report["SYM_B"]["boundary_mismatch_days"]["2026-11-13"]["first"] == "09:40"
    assert report["SYM_B"]["session_states"]["2026-11-11"] == SESSION_STATE_MISSING_DAY
    assert report["SYM_B"]["session_states"]["2026-11-12"] == SESSION_STATE_SHORT_BARS
    assert report["SYM_B"]["session_states"]["2026-11-13"] == SESSION_STATE_SHORT_BARS

    assert report["SYM_C"]["status"] == STATUS_INSUFFICIENT
    assert report["SYM_D"]["status"] == STATUS_INSUFFICIENT


def test_baostock_sequential_sync_contract_passes_real_script_and_fails_closed():
    class _GoodDouble:
        keep_logged_in = True
        adjustflag = "3"

    res = sync.assert_baostock_sequential_sync_contract(provider_instance=_GoodDouble())
    assert res["ok"] is True
    assert res["single_session_verified"] is True

    # Forbidden concurrency imports / usages fail closed
    for snippet in [
        "import multiprocessing\n",
        "from concurrent.futures import ThreadPoolExecutor\n",
        "import threading\nt = threading.Thread(target=lambda: None)\n",
        (
            "def sync_5m_intraday():\n"
            "    for sym in ['A']:\n"
            "        p = BaoStockDataProvider(keep_logged_in=True)\n"
        ),
    ]:
        with pytest.raises(RuntimeError, match="BaoStock sequential sync contract violated"):
            sync.assert_baostock_sequential_sync_contract(snippet)

    # keep_logged_in=False fails closed
    class _BadDouble:
        keep_logged_in = False
        adjustflag = "3"

    with pytest.raises(RuntimeError, match="keep_logged_in"):
        sync.assert_baostock_sequential_sync_contract(provider_instance=_BadDouble())


def _real_provider(**kwargs):
    """Construct the real provider without logging in.

    `BaoStockDataProvider.__init__` only stores the params (no socket/login happens until
    the first fetch), so this stays 100% offline. `baostock.login` is stubbed anyway so a
    future constructor change can never turn this into a network test.
    """
    from common.financial_api import BaoStockDataProvider

    with patch("baostock.login", side_effect=AssertionError("no network in tests")):
        return BaoStockDataProvider(cache_dir=None, **kwargs)


def test_guard_accepts_real_provider_with_keep_logged_in_true():
    provider = _real_provider(adjustflag="3", keep_logged_in=True)
    # The real class stores it privately only -- this is exactly what broke the guard.
    assert not hasattr(provider, "keep_logged_in")
    assert provider._keep_logged_in is True

    res = sync.assert_baostock_sequential_sync_contract(provider_instance=provider)
    assert res["ok"] is True


def test_guard_rejects_real_provider_with_keep_logged_in_false():
    provider = _real_provider(adjustflag="3", keep_logged_in=False)
    with pytest.raises(
        RuntimeError,
        match="BaoStock sequential sync contract violated: provider keep_logged_in must be True",
    ):
        sync.assert_baostock_sequential_sync_contract(provider_instance=provider)


def test_guard_fails_closed_when_attribute_absent():
    class _NoAttrs:
        adjustflag = "3"

    assert sync._provider_keep_logged_in(_NoAttrs()) is False
    with pytest.raises(RuntimeError, match="keep_logged_in must be True"):
        sync.assert_baostock_sequential_sync_contract(provider_instance=_NoAttrs())


def test_keep_logged_in_helper_prefers_public_attribute():
    class _BothAttrs:
        keep_logged_in = False
        _keep_logged_in = True

    assert sync._provider_keep_logged_in(_BothAttrs()) is False


def test_full_bars_lt_45_sync_to_feature_gate_alignment():
    cache = DuckDBCache(db_path=":memory:")
    # Day 1: 48 bars; Day 2: 40 bars (09:35-11:30 = 24 + 13:05-14:20 = 16 -> 40 < 45)
    day1 = _make_session_5m("2026-11-11", last="15:00")
    day2 = _make_session_5m("2026-11-12", last="14:20")
    assert len(day1) == 48 and len(day2) == 40
    bars_5m = pd.concat([day1, day2])

    class _FakeBs:
        keep_logged_in = True
        adjustflag = "3"

        def __init__(self, *args, **kwargs):
            pass

        def fetch_raw_ohlcv(self, symbol, start, end, interval="5m"):
            return bars_5m.copy()

        fetch_ohlcv = fetch_raw_ohlcv

        def fetch_adjust_factors(self, symbol, start, end):
            return pd.DataFrame(
                {"factor_hfq": [1.0, 1.0]},
                index=pd.to_datetime(["2026-11-11", "2026-11-12"]),
            )

        def logout(self):
            return None

    class _FakeEm:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_raw_ohlcv(self, symbol, start, end, interval="5m"):
            return pd.DataFrame()

        fetch_ohlcv = fetch_raw_ohlcv

    with (
        patch.object(sync, "BaoStockDataProvider", _FakeBs),
        patch.object(sync, "EastMoneyDataProvider", _FakeEm),
        pytest.warns(IntradayIncompleteWarning),
    ):
        report = sync.sync_5m_intraday(
            cache,
            stocks=["600900.SH"],
            indices=[],
            start="2026-11-11",
            end="2026-11-12",
        )

    assert report.short_session_symbols["600900.SH"] == {"2026-11-12": 40}
    assert report.to_diagnostics()["short_session_symbols"]["600900.SH"] == {"2026-11-12": 40}

    # Verify stored raw bars were NOT padded
    stored = cache.query_intraday_bars(
        "BaoStockDataProvider", "600900.SH", interval="5m", adjustflag="raw"
    )
    assert len(stored) == 88

    # Enrich daily universe with same 5m bars -> Day 2 skipped with IntradayIncompleteWarning & NaN
    daily_df = pd.DataFrame(
        {
            "Open": [100.0, 100.5],
            "High": [101.0, 101.5],
            "Low": [99.0, 99.5],
            "Close": [100.8, 101.0],
            "Volume": [48000.0, 40000.0],
        },
        index=pd.to_datetime(["2026-11-11", "2026-11-12"]),
    )
    skip_report: dict = {}
    with pytest.warns(IntradayIncompleteWarning):
        enriched = enrich_universe_with_intraday_features(
            {"600900.SH": daily_df},
            {"600900.SH": bars_5m},
            config=IntradayFeatureConfig(enable_chan_5m=False),
            report=skip_report,
        )

    assert any(
        d["symbol"] == "600900.SH" and d["date"] == "2026-11-12" and d["bars"] == 40
        for d in skip_report["symbol_days"]
    )
    out_df = enriched["600900.SH"]
    feature_cols = [c for c in out_df.columns if c not in daily_df.columns]
    assert len(feature_cols) >= 1
    assert out_df.loc[pd.Timestamp("2026-11-12"), feature_cols].isna().all()


def test_record_5m_degradation_trace_dual_mode_vs_pure_5m():
    sink: dict = {}
    record_5m_degradation_trace(
        sink,
        symbol="600900.SH",
        operator="intraday_vwap_dev",
        reason="short_session_40_bars",
        fallback_used=None,
    )
    record_5m_degradation_trace(
        sink,
        symbol="600900.SH",
        operator="amihud_dual_mode",
        reason="short_session_40_bars",
        fallback_used="daily_amihud",
    )

    assert sink["count"] == 2
    assert sink["degradations"][0]["policy"] == PURE_5M_DEGRADATION_POLICY
    assert sink["degradations"][0]["fallback_used"] is None
    assert sink["degradations"][1]["policy"] == DUAL_MODE_DEGRADATION_POLICY
    assert sink["degradations"][1]["fallback_used"] == "daily_amihud"
