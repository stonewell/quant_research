"""5m provider provenance + staleness detection (2026-11-11 defect, `六` item 6).

The confirmed defect: `load_intraday_5m_universe`'s Step-1.5 DuckDB fallback walked a FIXED
provider order (`BaoStockDataProvider` -> `EastMoneyDataProvider` -> `fuyao`) and ignored the
provider the caller had configured. A `fuyao` live run therefore received BaoStock's 5m bars
(last bar 2026-11-09) although the `fuyao` cache held rows through 2026-11-10. Downstream that
froze `RS_Down_5m`, which froze `realized_vol_21d` across three sessions -- and the archive
blamed "provider does not support 5m", a false diagnosis of a stale cache.

Pinned here (all offline: synthetic bars, `tmp_path` DuckDB, no network):

1. caller provider present in the cache -> that provider is used, even when an earlier entry of
   the historical fallback list also has rows;
2. caller provider absent from the cache -> fallback happens AND `fallback_from` /
   `fallback_to` / `fallback_reason` are recorded (empty result and explicit "no 5m" raise are
   distinguished);
3. last bar older than the expected session -> `stale=True` with the right
   `lag_calendar_days`;
4. fresh data -> `stale=False` and NO fallback attributes.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from common import intraday_features as intraday_mod
from common.duckdb_cache import DuckDBCache


# ---------------------------------------------------------------------------
# Offline fixtures: synthetic 5m sessions + stub providers named like cache keys
# ---------------------------------------------------------------------------


def _day_5m_bars(day: str, base: float = 100.0) -> pd.DataFrame:
    """One A-share session of 48 synthetic 5m bars (09:35..15:00, lunch gap)."""
    morning = pd.date_range(f"{day} 09:35", f"{day} 11:30", freq="5min")
    afternoon = pd.date_range(f"{day} 13:05", f"{day} 15:00", freq="5min")
    idx = morning.append(afternoon)
    n = len(idx)
    close = base + np.linspace(0.0, 1.5, n)
    open_ = np.concatenate([[base], close[:-1]])
    return pd.DataFrame(
        {
            "Open": open_,
            "High": np.maximum(open_, close) + 0.25,
            "Low": np.minimum(open_, close) - 0.25,
            "Close": close,
            "Volume": np.full(n, 1000.0),
        },
        index=idx,
    )


def _session_frame(days, base: float = 100.0) -> pd.DataFrame:
    return pd.concat([_day_5m_bars(d, base=base) for d in days]).sort_index()


def _stub_provider_class(class_name: str, *, raises: Exception | None = None):
    """A provider stub whose CLASS NAME is the DuckDB cache key the loader must query."""

    class _StubProvider:
        def fetch_ohlcv(self, symbol, start=None, end=None, interval="1d", **kwargs):
            if raises is not None:
                raise raises
            return pd.DataFrame()

    _StubProvider.__name__ = class_name
    return _StubProvider


def _patch_5m_cache(monkeypatch, db_path):
    monkeypatch.setattr(
        intraday_mod, "_intraday_5m_cache_candidates", lambda cache_dir: [str(db_path)]
    )
    return intraday_mod


def _seed(tmp_path, provider: str, symbol: str, frame: pd.DataFrame) -> str:
    db_path = tmp_path / "cache.duckdb"
    cache = DuckDBCache(str(db_path))
    cache.store_intraday_bars(provider, symbol, frame, interval="5m", adjustflag="qfq")
    return str(db_path)


# ---------------------------------------------------------------------------
# 1. The caller's provider wins over the historical fixed order
# ---------------------------------------------------------------------------


def test_caller_provider_is_used_even_when_fallback_list_has_rows_first(tmp_path, monkeypatch):
    """Both providers hold rows; a `FuyaoDataProvider` caller must get FUYAO's bars.

    This is the exact shape of the production defect: BaoStock (first in the old fixed list)
    stopped at 11-09 while the caller's fuyao rows reached 11-10. Receiving BaoStock here
    would silently hand back one session of stale microstructure.
    """
    db_path = _seed(
        tmp_path,
        "BaoStockDataProvider",
        "AAA",
        _session_frame(["2026-11-09"]),
    )
    cache = DuckDBCache(db_path)
    cache.store_intraday_bars(
        "FuyaoDataProvider", "AAA", _session_frame(["2026-11-10"], base=105.0),
        interval="5m", adjustflag="qfq",
    )
    _patch_5m_cache(monkeypatch, db_path)

    provider_cls = _stub_provider_class("FuyaoDataProvider")
    out = intraday_mod.load_intraday_5m_universe(
        ["AAA"],
        start="2026-11-09",
        end="2026-11-11",
        data_provider=provider_cls(),
        parquet_dir=str(tmp_path / "no_parquet"),
    )

    frame = out["AAA"]
    assert not frame.empty
    assert frame.index.max() == pd.Timestamp("2026-11-10 15:00")
    assert frame.attrs["intraday_5m_provider"] == "FuyaoDataProvider"
    assert frame.attrs["intraday_5m_source"] == "duckdb_cache"
    # no silent source switch -> no fallback bookkeeping
    assert "fallback_from" not in frame.attrs
    assert "fallback_to" not in frame.attrs
    assert "fallback_reason" not in frame.attrs


def test_cached_data_provider_wrapper_resolves_to_inner_provider(tmp_path, monkeypatch):
    """A `CachedDataProvider`-style wrapper must be unwrapped to its real upstream provider."""
    db_path = _seed(tmp_path, "EastMoneyDataProvider", "AAA", _session_frame(["2026-11-10"]))
    cache = DuckDBCache(db_path)
    cache.store_intraday_bars(
        "BaoStockDataProvider", "AAA", _session_frame(["2026-11-09"]),
        interval="5m", adjustflag="qfq",
    )
    _patch_5m_cache(monkeypatch, db_path)

    inner = _stub_provider_class("EastMoneyDataProvider")()
    wrapper = type("CachedDataProvider", (), {"inner_provider": inner})()

    out = intraday_mod.load_intraday_5m_universe(
        ["AAA"], data_provider=wrapper, parquet_dir=str(tmp_path / "no_parquet")
    )

    assert out["AAA"].index.max() == pd.Timestamp("2026-11-10 15:00")
    assert out["AAA"].attrs["intraday_5m_provider"] == "EastMoneyDataProvider"


# ---------------------------------------------------------------------------
# 2. Fallback is allowed, recorded, and never silent
# ---------------------------------------------------------------------------


def test_missing_caller_provider_falls_back_with_recorded_provenance(tmp_path, monkeypatch, caplog):
    """Preferred provider has no cached 5m rows -> fallback, with all three fields set."""
    db_path = _seed(tmp_path, "BaoStockDataProvider", "AAA", _session_frame(["2026-11-09"]))
    _patch_5m_cache(monkeypatch, db_path)

    provider_cls = _stub_provider_class("FuyaoDataProvider")  # returns empty, no raise
    with caplog.at_level(logging.WARNING, logger="common.intraday_features"):
        out = intraday_mod.load_intraday_5m_universe(
            ["AAA"], data_provider=provider_cls(), parquet_dir=str(tmp_path / "no_parquet")
        )

    frame = out["AAA"]
    assert not frame.empty  # the fallback still delivers the bars it found
    assert frame.attrs["fallback_from"] == "FuyaoDataProvider"
    assert frame.attrs["fallback_to"] == "BaoStockDataProvider"
    assert frame.attrs["fallback_reason"] == intraday_mod.FALLBACK_REASON_RETURNED_EMPTY
    assert frame.attrs["intraday_5m_provider"] == "BaoStockDataProvider"
    assert "5m 数据来源回退" in caplog.text


def test_unsupported_5m_provider_is_reported_as_unsupported_not_as_empty(tmp_path, monkeypatch):
    """An explicit `interval='5m'` refusal must not be flattened into the same reason code."""
    db_path = _seed(tmp_path, "EastMoneyDataProvider", "AAA", _session_frame(["2026-11-10"]))
    _patch_5m_cache(monkeypatch, db_path)

    provider_cls = _stub_provider_class(
        "FuyaoDataProvider", raises=ValueError("provider does not support 5m")
    )
    out = intraday_mod.load_intraday_5m_universe(
        ["AAA"], data_provider=provider_cls(), parquet_dir=str(tmp_path / "no_parquet")
    )

    frame = out["AAA"]
    assert frame.attrs["fallback_from"] == "FuyaoDataProvider"
    assert frame.attrs["fallback_to"] == "EastMoneyDataProvider"
    assert frame.attrs["fallback_reason"] == intraday_mod.FALLBACK_REASON_UNSUPPORTED_5M
    assert "does not support 5m" in frame.attrs["fallback_reason_detail"]


def test_no_provider_specified_keeps_historical_order_and_records_reason(tmp_path, monkeypatch):
    """Research/backtest callers pass no provider: historical order must be unchanged."""
    db_path = _seed(tmp_path, "BaoStockDataProvider", "AAA", _session_frame(["2026-11-09"]))
    cache = DuckDBCache(db_path)
    cache.store_intraday_bars(
        "EastMoneyDataProvider", "AAA", _session_frame(["2026-11-10"]),
        interval="5m", adjustflag="qfq",
    )
    _patch_5m_cache(monkeypatch, db_path)

    out = intraday_mod.load_intraday_5m_universe(
        ["AAA"], parquet_dir=str(tmp_path / "no_parquet")
    )

    assert out["AAA"].index.max() == pd.Timestamp("2026-11-09 15:00")  # BaoStock first
    # nothing to fall back FROM, so only positive provenance is reported
    assert out["AAA"].attrs["intraday_5m_provider"] == "BaoStockDataProvider"
    assert "fallback_from" not in out["AAA"].attrs


# ---------------------------------------------------------------------------
# 3./4. Staleness judgement against the expected session
# ---------------------------------------------------------------------------


def test_stale_5m_window_reports_stale_and_lag_days():
    """Last bar 11-09 vs expected session 11-11 -> stale with a 2-calendar-day lag."""
    frame = _session_frame(["2026-11-09"])

    result = intraday_mod.assess_intraday_5m_freshness(
        {"AAA": frame}, expected_last_session="2026-11-11"
    )

    assert result["stale"] is True
    assert result["lag_calendar_days"] == 2
    assert result["reason"] == intraday_mod.INTRADAY_5M_FRESHNESS_STALE
    assert result["latest_available_ts"] == "2026-11-09T15:00:00"
    assert result["expected_last_session"] == "2026-11-11"
    # the deadline is the session's last FULL bar (repo calendar owns this, not this module)
    assert result["expected_deadline_ts"] == "2026-11-11T14:55:00"
    assert result["symbols"]["AAA"]["stale"] is True
    assert result["stale_symbols"] == ["AAA"]
    assert result["fresh_symbols"] == []


def test_staleness_is_per_symbol_not_just_aggregate():
    """A fresh symbol must not mask a stale sibling (the freeze happened per symbol)."""
    result = intraday_mod.assess_intraday_5m_freshness(
        {
            "FRESH": _session_frame(["2026-11-11"]),
            "STALE": _session_frame(["2026-11-09"]),
        },
        expected_last_session="2026-11-11",
    )

    assert result["stale"] is True  # aggregate: any stale symbol -> stale
    assert result["symbols"]["FRESH"]["stale"] is False
    assert result["symbols"]["FRESH"]["lag_calendar_days"] == 0
    assert result["symbols"]["STALE"]["lag_calendar_days"] == 2
    assert result["fresh_symbols"] == ["FRESH"]
    assert result["stale_symbols"] == ["STALE"]


def test_empty_5m_frame_is_reported_as_no_bars():
    """No bars is "unavailable", not accidentally "fresh"."""
    result = intraday_mod.assess_intraday_5m_freshness(
        {"AAA": pd.DataFrame()}, expected_last_session="2026-11-11"
    )

    assert result["stale"] is True
    assert result["reason"] == intraday_mod.INTRADAY_5M_FRESHNESS_NO_BARS
    assert result["latest_available_ts"] is None
    assert result["lag_calendar_days"] is None


def test_fresh_5m_frame_is_not_stale_and_loader_adds_no_fallback_fields(tmp_path, monkeypatch):
    """Test 4: caller's own provider is fresh -> stale=False, no fallback attributes."""
    db_path = _seed(
        tmp_path,
        "FuyaoDataProvider",
        "AAA",
        _session_frame(["2026-11-11"]),
    )
    cache = DuckDBCache(db_path)
    cache.store_intraday_bars(
        "BaoStockDataProvider", "AAA", _session_frame(["2026-11-09"]),
        interval="5m", adjustflag="qfq",
    )
    _patch_5m_cache(monkeypatch, db_path)

    provider_cls = _stub_provider_class("FuyaoDataProvider")
    out = intraday_mod.load_intraday_5m_universe(
        ["AAA"],
        data_provider=provider_cls(),
        parquet_dir=str(tmp_path / "no_parquet"),
        expected_last_session="2026-11-11",
    )

    frame = out["AAA"]
    assert frame.index.max() == pd.Timestamp("2026-11-11 15:00")
    assert frame.attrs["intraday_5m_stale"] is False
    assert frame.attrs["intraday_5m_lag_calendar_days"] == 0
    assert frame.attrs["intraday_5m_freshness_reason"] == intraday_mod.INTRADAY_5M_FRESHNESS_OK
    assert "fallback_from" not in frame.attrs
    assert "fallback_to" not in frame.attrs
    assert "fallback_reason" not in frame.attrs


def test_loader_stamps_stale_attrs_when_expected_session_is_ahead(tmp_path, monkeypatch):
    """The loader itself must surface a frozen cache, not only the standalone helper."""
    db_path = _seed(tmp_path, "FuyaoDataProvider", "AAA", _session_frame(["2026-11-09"]))
    _patch_5m_cache(monkeypatch, db_path)

    provider_cls = _stub_provider_class("FuyaoDataProvider")
    out = intraday_mod.load_intraday_5m_universe(
        ["AAA"],
        data_provider=provider_cls(),
        parquet_dir=str(tmp_path / "no_parquet"),
        expected_last_session="2026-11-11",
    )

    frame = out["AAA"]
    assert frame.attrs["intraday_5m_stale"] is True
    assert frame.attrs["intraday_5m_lag_calendar_days"] == 2
    assert frame.attrs["intraday_5m_freshness_reason"] == intraday_mod.INTRADAY_5M_FRESHNESS_STALE


def test_expected_session_defaults_to_repo_calendar():
    """Omitted `expected_last_session` must come from `common.market_clock`, not a local calendar."""
    from datetime import datetime, timezone

    from common.market_clock import expected_last_session

    now = datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc)  # 10:00 Asia/Shanghai, post-open
    result = intraday_mod.assess_intraday_5m_freshness({"AAA": pd.DataFrame()}, now=now)

    assert result["expected_last_session"] == expected_last_session(now=now).isoformat()


@pytest.mark.parametrize("bad", [None, {}, {"AAA": None}])
def test_no_5m_data_at_all_is_stale(bad):
    """Defensive: a missing/None universe can never read as "fresh"."""
    result = intraday_mod.assess_intraday_5m_freshness(bad, expected_last_session="2026-11-11")

    assert result["stale"] is True
    assert result["reason"] == intraday_mod.INTRADAY_5M_FRESHNESS_NO_BARS
