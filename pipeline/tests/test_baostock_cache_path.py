"""`BaoStockDataProvider` must never IMPLICITLY create a cache at a CWD-relative `data/`.

Forensic background (2026-10-27, residual sibling of the `web/livetrading/storage.py` /
`scripts/check_live_portfolio_health.py` incident covered by
`pipeline/tests/test_cache_write_gates.py`): the constructor's final branch was

    self._duckdb_cache = DuckDBCache(db_path="data") if os.path.exists("data") else None

That is an implicit, CWD-relative, READ-WRITE open of `<cwd>/data`, so:

  * any bare `BaoStockDataProvider()` -- and `common/financial_api.py` itself builds one at
    five internal call sites, e.g. `MarketDBDataProvider`'s index fallback -- recreated a
    production cache an operator had deleted (the "corpse comes back" symptom);
  * the file it touched depended on the process CWD, so two processes started in different
    directories silently wrote two different "caches".

The `DuckDBCache` production-path guard only fires under pytest, which is *why* this went
unnoticed: outside pytest there was nothing to refuse the write.

Contract asserted below (matches the implementation in `common/financial_api.py`):

  1. no `cache_dir`                      -> no cache at all, and NOTHING is created;
  2. relative `cache_dir` (e.g. "data")  -> rejected with a UserWarning, no cache, nothing
                                            created (the path's meaning is CWD-dependent);
  3. absolute `cache_dir` (tmp dir)      -> historical READ-WRITE open, i.e. the file IS
                                            created and fundamentals round-trip (existing
                                            callers pass a temp dir exactly for that write);
  4. every case leaves no stray `.duckdb` behind except the one an explicit absolute temp
     `cache_dir` was asked to create.

100% offline: no `baostock` import (it is lazy inside `BaoStockDataProvider`), no login, no
socket, and no path may resolve inside `<repo>/data` -- every cache lives under `tmp_path`.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from common.data import FundamentalMetrics
from common.financial_api import BaoStockDataProvider

#: Files a cache open would leave behind (`DuckDBCache` appends `cache.duckdb` to a directory).
_DUCKDB_SUFFIXES = (".duckdb", ".db", ".wal")


def _duckdb_files(root: os.PathLike | str) -> list[str]:
    """Every DuckDB-ish file under `root`, relative to it, sorted (the actual inventory)."""
    found: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(_DUCKDB_SUFFIXES):
                found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(found)


def test_no_cache_dir_never_creates_cwd_relative_data(tmp_path, monkeypatch):
    """Case 1: a bare `BaoStockDataProvider()` must not touch `<cwd>/data` at all."""
    monkeypatch.chdir(tmp_path)

    provider = BaoStockDataProvider()

    assert provider._duckdb_cache is None, (
        "no cache_dir was passed, so the provider must run cache-free -- the old fallback "
        "opened DuckDBCache(db_path='data') (CWD-relative, read-write) whenever `./data` "
        "happened to exist, which silently recreated a deleted production cache"
    )
    assert not os.path.exists(tmp_path / "data"), (
        "the CWD-relative `data` directory must not even be probed into existence "
        f"(found: {_duckdb_files(tmp_path)})"
    )
    assert _duckdb_files(tmp_path) == []


def test_relative_cache_dir_is_rejected_with_warning(tmp_path, monkeypatch):
    """Case 2: a relative `cache_dir` is refused (warning) instead of created in the CWD."""
    monkeypatch.chdir(tmp_path)

    with pytest.warns(UserWarning, match="ignoring relative cache_dir"):
        provider = BaoStockDataProvider(cache_dir="data")

    assert provider._duckdb_cache is None
    assert not os.path.exists(tmp_path / "data"), (
        f"relative cache_dir='data' must not create anything (found: {_duckdb_files(tmp_path)})"
    )
    assert _duckdb_files(tmp_path) == []


def test_absolute_cache_dir_still_opens_read_write(tmp_path):
    """Case 3: an explicit ABSOLUTE cache_dir keeps the historical read-write behaviour.

    Deliberately writable (not `read_only=True`): `fetch_financial_indicators` persists the
    freshly parsed metrics via `store_fundamentals` and serves the next call from
    `query_fundamentals` (see `common/tests/test_financial_api.py`), so a read-only open
    would silently disable that path. This test mirrors the real behaviour instead of
    asserting something the implementation does not do.
    """
    cache_root = tmp_path / "cache_dir"
    cache_root.mkdir()

    provider = BaoStockDataProvider(cache_dir=str(cache_root))

    cache = provider._duckdb_cache
    assert cache is not None, "an explicit absolute cache_dir must be honoured"
    assert cache.read_only is False
    # `DuckDBCache` resolves a directory to `<dir>/cache.duckdb` and creates it eagerly.
    assert os.path.exists(cache_root / "cache.duckdb")
    assert _duckdb_files(tmp_path) == [os.path.join("cache_dir", "cache.duckdb")]

    # Writability is real, not just a flag: a fundamentals row round-trips.
    cache.store_fundamentals(
        "BaoStockDataProvider",
        FundamentalMetrics(
            symbol="600519.SH",
            report_year=2023,
            stat_date="2023-12-31",
            roe=0.3412,
            net_profit=7.4734e10,
            net_profit_5yr_cagr=0.15,
            cfo=6.6e10,
            cfo_to_net_profit=0.89,
            total_assets=2.5e11,
            total_liabilities=5.0e10,
            debt_to_assets=0.2,
            debt_to_equity=0.25,
            interest_bearing_debt=float("nan"),
            dividend_yield=0.0179,
            dividend_payout_ratio=0.519,
            pe_ttm=28.5,
            pb=8.2,
            total_shares=1.256e9,
            market_cap=2.16e12,
        ),
    )
    read_back = cache.query_fundamentals("BaoStockDataProvider", "600519.SH", report_year=2023)
    assert read_back is not None
    assert read_back.symbol == "600519.SH"
    assert read_back.roe == pytest.approx(0.3412)

    # Still nothing outside the directory the caller named.
    assert _duckdb_files(tmp_path) == [os.path.join("cache_dir", "cache.duckdb")]


def test_all_cases_leave_no_stray_duckdb_under_tmp(tmp_path, monkeypatch):
    """Case 4: run cases 1-3 side by side and inventory what actually landed on disk.

    Only the explicitly-named absolute cache may exist; the `data/` directories the two
    no-explicit-path cases would have created must be absent.
    """
    monkeypatch.chdir(tmp_path)

    BaoStockDataProvider()
    with pytest.warns(UserWarning, match="ignoring relative cache_dir"):
        BaoStockDataProvider(cache_dir="data")

    explicit = tmp_path / "explicit"
    explicit.mkdir()
    BaoStockDataProvider(cache_dir=str(explicit))

    inventory = _duckdb_files(tmp_path)
    assert inventory == [os.path.join("explicit", "cache.duckdb")], (
        f"only the explicitly requested absolute cache may exist, got {inventory}"
    )
    assert not os.path.exists(tmp_path / "data")
