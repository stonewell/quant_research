"""Guard behaviour of the `adjust` / `adjustflag` declaration (offline, no network).

1. An illegal basis token is rejected BEFORE any side effect: a typo in `adjust=` must never
   degrade into "no local database" / a differently-adjusted series, so it has to raise ahead of
   `duckdb.connect`, HTTP, or a BaoStock login.
2. `FuyaoDataProvider` must not swallow that rejection through its `except Exception` around the
   local `MarketDBDataProvider(...)` construction (a typo would become a silent "no local DB").
3. The FRAME's own `df.attrs["adjust_basis"]` beats the provider-level knob when the cache records
   what was stored (M3.9): a provider declaring `qfq` while serving unadjusted bars must be cached
   as `raw`, never under a false `qfq` label.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pandas as pd
import pytest

from common.data import BaseDataProvider, CachedDataProvider, _provider_native_basis
from common.financial_api import (
    BaoStockDataProvider, EastMoneyDataProvider,
    FuyaoDataProvider, MarketDBDataProvider,
)


def _raw_frame() -> pd.DataFrame:
    """Deterministic 5-row OHLCV frame that declares itself unadjusted."""
    close = [10.0, 10.2, 10.1, 10.4, 10.3]
    df = pd.DataFrame(
        {"Open": close, "High": [c + 0.2 for c in close], "Low": [c - 0.2 for c in close],
         "Close": close, "Volume": [1_000.0] * 5},
        index=pd.date_range("2024-01-02", periods=5, freq="D"),
    )
    df.attrs["adjust_basis"] = "raw"
    return df


def _forbid(*_args, **_kwargs):
    pytest.fail("unexpected SQL/network call: a rejected adjust token must not reach any I/O")


def test_zero_side_effects_on_rejection(monkeypatch):
    import duckdb
    import requests

    monkeypatch.setattr(duckdb, "connect", _forbid)
    monkeypatch.setattr(requests, "get", _forbid)
    monkeypatch.setattr(requests, "post", _forbid)
    monkeypatch.setattr(requests.Session, "get", _forbid)
    monkeypatch.setattr(requests.Session, "post", _forbid)
    try:
        import baostock
    except ImportError:  # baostock is an optional dependency
        baostock = None
    if baostock is not None:
        monkeypatch.setattr(baostock, "login", _forbid, raising=False)
        monkeypatch.setattr(baostock, "query_history_k_data_plus", _forbid, raising=False)

    with pytest.raises(ValueError):
        MarketDBDataProvider(adjust="nonsense")  # no `con=` -> would resolve/connect a file
    with pytest.raises(ValueError):
        FuyaoDataProvider(adjust="nonsense")
    with pytest.raises(ValueError):
        BaoStockDataProvider(adjustflag="nonsense")
    with pytest.raises(ValueError):
        EastMoneyDataProvider(adjust="nonsense")


def test_fuyao_does_not_swallow_invalid_adjust(monkeypatch):
    """The local-provider fallback is `except Exception` -- a ValueError must not land there."""
    constructed: list = []

    def _record_and_fail(self, *_args, **_kwargs):
        constructed.append(self)
        raise RuntimeError("local provider must not be built after a rejected adjust token")

    monkeypatch.setattr(MarketDBDataProvider, "__init__", _record_and_fail)
    with pytest.raises(ValueError):
        FuyaoDataProvider(adjust="nonsense", prefer_local=True)
    assert constructed == []


class _StubForwardProvider(BaseDataProvider):
    """Non-synthetic provider declaring `qfq` while actually serving unadjusted bars."""

    native_adjust_basis = "qfq"

    def fetch_ohlcv(self, symbol, start, end, interval="1d"):
        return _raw_frame()


def test_frame_basis_beats_provider_default(tmp_path):
    frame = _raw_frame()
    stub = _StubForwardProvider()
    assert (_provider_native_basis(stub, frame), _provider_native_basis(stub)) == ("raw", "qfq")

    # Real `common/data.py` write path: CachedDataProvider -> DuckDBCache.store_bars.
    cached = CachedDataProvider(stub, cache_dir=str(tmp_path))
    cached.fetch_ohlcv("AAPL", "2024-01-02", "2024-01-06", adjust="raw")
    meta = cached.duckdb_cache.get_asset_metadata(type(stub).__name__, "AAPL")
    assert meta is not None and meta["adjustflag"] == "raw"  # not the provider-level "qfq"
