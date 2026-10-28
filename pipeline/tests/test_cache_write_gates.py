"""Read-only / display paths must never CREATE or WRITE the production cache (2026-10-27).

Forensic background (confirmed before this file was written):

  * `web/livetrading/storage.py::get_quotes()` -- the function behind "open the web page,
    pull a quote" -- called `load_universe(..., use_cache=True, cache_dir=<repo>/data)`, which
    builds a WRITABLE `CachedDataProvider` -> `DuckDBCache(db_path=...)`. That constructor
    `makedirs`es and `_init_tables`s, so simply loading the page RECREATED a
    `data/cache.duckdb` an operator had deleted. The corpse was identifiable: exactly the 4
    held symbols, exactly the panel's `as_of - 35d` window, and a `last_sync_time` earlier
    than the Stage-1 child process that supposedly wrote it.
  * `scripts/check_live_portfolio_health.py::refresh_stage1_data()` opened
    `DuckDBCache(eff_cache_dir)` writable with no existence check, so a deleted cache was
    silently rebuilt as an EMPTY one -- and Stage 2 then traded against it. Stage-1's job is
    to REFRESH the cache, never to rebuild it.

The production cache guard (`common/duckdb_cache.py::production_cache_guard_active`) only
fires under pytest, by design; it is NOT changed here. Instead both paths stop needing write
permission on their own:

  * `CachedDataProvider(cache_read_only=True)` -> `DuckDBCache(read_only=True)`, which raises
    `FileNotFoundError` when the file is absent (that is the contract, not a bug to paper over);
  * `refresh_stage1_data(allow_create_cache=False)` (default) -> `{"status": "refused",
    "reason": "cache_missing_needs_allow_create"}`, and NOTHING is created.

100% offline: every provider here is a stub (no network), every database lives under
`tmp_path`, and no test may resolve a path inside `<repo>/data`.
"""

import logging
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import duckdb
import pandas as pd
import pytest

import common.data as common_data
import scripts.check_live_portfolio_health as health_mod
from common.data import BaseDataProvider, CachedDataProvider
from common.duckdb_cache import DuckDBCache


# ---------------------------------------------------------------------------
# Offline primitives
# ---------------------------------------------------------------------------


class _StubProvider(BaseDataProvider):
    """Deterministic OHLCV source; the only network-shaped thing here is the method name."""

    def __init__(self):
        self.calls = 0

    def fetch_ohlcv(self, symbol, start, end, interval="1d"):
        self.calls += 1
        dates = pd.bdate_range(start, end)
        return pd.DataFrame(
            {
                "Open": [10.0] * len(dates),
                "High": [12.0] * len(dates),
                "Low": [9.0] * len(dates),
                "Close": [11.0] * len(dates),
                "Volume": [100.0] * len(dates),
            },
            index=dates,
        )

    def fetch_metadata(self, symbol):
        return {"symbol": symbol}


def _row_counts(db_path):
    """(ohlcv_bars, asset_sync_metadata) row counts, opened strictly read-only."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        bars = con.execute("SELECT COUNT(*) FROM ohlcv_bars").fetchone()[0]
        meta = con.execute("SELECT COUNT(*) FROM asset_sync_metadata").fetchone()[0]
    finally:
        con.close()
    return int(bars), int(meta)


@pytest.fixture
def stub_provider(monkeypatch):
    """Patch the provider factory so `load_universe(provider="marketdb")` stays offline.

    `common.data.get_data_provider` is the single factory every loader goes through; replacing
    it (rather than registering a provider) also skips `_ensure_financial_api_registered`,
    which would import the real marketdb/baostock stack.
    """
    provider = _StubProvider()
    monkeypatch.setattr(common_data, "get_data_provider", lambda *a, **k: provider)
    return provider


# ---------------------------------------------------------------------------
# (1) get_quotes must not create the cache it is only supposed to read
# ---------------------------------------------------------------------------


def test_get_quotes_does_not_create_a_missing_cache(tmp_path, stub_provider, caplog):
    """A deleted cache is a MISSING QUOTE, never "let me rebuild it for you"."""
    from web.livetrading.storage import StorageManager

    cache_dir = tmp_path / "duckdb_cache"
    cache_dir.mkdir()  # the DIRECTORY may exist; the DATABASE must not be created
    db_file = cache_dir / "cache.duckdb"
    assert not db_file.exists()

    storage = StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(tmp_path / "data"),
        archive_dir=str(tmp_path / "runs"),
        cache_dir=str(cache_dir),
        settings_dir=str(tmp_path / "settings"),
    )

    with caplog.at_level(logging.WARNING, logger="web.livetrading.storage"):
        res = storage.get_quotes(["AAPL"], as_of_date="2026-09-29", data_provider="marketdb")

    # THE regression: no file, no directory contents, nothing.
    assert not db_file.exists(), "get_quotes created the production-style cache file"
    assert list(cache_dir.iterdir()) == []

    # The quote is simply absent -- "not_found", price None -- not an exception, not a rebuild.
    quote = res["quotes"]["AAPL"]
    assert quote["price"] is None
    assert quote["source"] == "not_found"

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a missing cache must be logged, not swallowed silently"
    message = warnings[0].getMessage()
    assert str(cache_dir) in message
    assert "FileNotFoundError" in message


# ---------------------------------------------------------------------------
# (2) CachedDataProvider(cache_read_only=True) on a missing file
# ---------------------------------------------------------------------------


def test_read_only_provider_raises_file_not_found_when_cache_is_missing(tmp_path):
    """`read_only=True` fails at construction; it must NOT degrade into "create an empty one"."""
    db_file = tmp_path / "absent" / "cache.duckdb"

    with pytest.raises(FileNotFoundError):
        CachedDataProvider(_StubProvider(), str(db_file), cache_read_only=True)

    assert not db_file.exists()
    assert not (tmp_path / "absent").exists(), "read_only must not even create the directory"


# ---------------------------------------------------------------------------
# (3) An existing cache opened read-only is never written to
# ---------------------------------------------------------------------------


def test_read_only_provider_never_writes_an_existing_cache(tmp_path):
    db_file = tmp_path / "cache.duckdb"
    # Create the schema with a writable open (tmp_path only!), then reopen strictly read-only.
    DuckDBCache(str(db_file))
    before = _row_counts(db_file)
    assert before == (0, 0)

    provider = CachedDataProvider(_StubProvider(), str(db_file), cache_read_only=True)
    df = provider.fetch_ohlcv("AAPL", "2026-01-05", "2026-01-09")

    # A miss is served by the provider (use_cache=False semantics) instead of being stored.
    assert not df.empty
    assert "Close" in df.columns
    assert _row_counts(db_file) == before, "read-only provider wrote rows into the cache"

    # Same guarantee through the multi-symbol path.
    universe = provider.fetch_universe(["AAPL", "MSFT"], "2026-01-05", "2026-01-09")
    assert set(universe.keys()) == {"AAPL", "MSFT"}
    assert _row_counts(db_file) == (0, 0)


# ---------------------------------------------------------------------------
# (4) Defaults unchanged: no flag => still writable (bit-for-bit legacy behaviour)
# ---------------------------------------------------------------------------


def test_default_provider_is_still_writable(tmp_path):
    cache_dir = tmp_path / "legacy"
    provider = CachedDataProvider(_StubProvider(), str(cache_dir))

    df = provider.fetch_ohlcv("AAPL", "2026-01-05", "2026-01-09")
    assert not df.empty

    db_file = cache_dir / "cache.duckdb"
    assert db_file.exists()
    bars, meta = _row_counts(db_file)
    assert bars > 0
    assert meta == 1


def test_read_only_quote_path_still_serves_prices_from_an_existing_cache(tmp_path, stub_provider):
    """The flag must not turn a healthy cache into "no quotes" -- it only removes write rights."""
    from web.livetrading.storage import StorageManager

    cache_dir = tmp_path / "duckdb_cache"
    db_file = cache_dir / "cache.duckdb"

    # Warm the cache the ordinary way (writable, tmp_path only), then read it read-only.
    warmer = CachedDataProvider(stub_provider, str(cache_dir))
    warmer.fetch_ohlcv("AAPL", "2026-08-25", "2026-09-30")
    before = _row_counts(db_file)
    assert before[0] > 0

    storage = StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(tmp_path / "data"),
        archive_dir=str(tmp_path / "runs"),
        cache_dir=str(cache_dir),
        settings_dir=str(tmp_path / "settings"),
    )
    res = storage.get_quotes(["AAPL"], as_of_date="2026-09-30", data_provider="marketdb")

    quote = res["quotes"]["AAPL"]
    assert quote["price"] == pytest.approx(11.0)
    assert quote["source"] == "marketdb"
    assert quote["stale"] is False
    # Reading a quote still writes nothing, even when the cache is present and covered.
    assert _row_counts(db_file) == before


# ---------------------------------------------------------------------------
# (5)-(7) Stage-1 refresh gate
# ---------------------------------------------------------------------------


@pytest.fixture
def silent_sync(monkeypatch):
    """Patch the three china sync routines so Stage-1 never reaches the network."""
    import scripts.sync_china_5m_and_indices as sync_mod

    calls = {"indices": 0, "equity": 0, "intraday": 0}

    def _indices(cache, indices, start=None, end=None, **kw):
        calls["indices"] += 1
        return {}

    def _equity(cache, symbols, end=None, **kw):
        calls["equity"] += 1
        return {}

    def _intraday(cache, equity, indices, start=None, end=None, **kw):
        calls["intraday"] += 1
        return {}

    monkeypatch.setattr(sync_mod, "sync_indices_daily", _indices)
    monkeypatch.setattr(sync_mod, "patch_equity_daily_bars", _equity)
    monkeypatch.setattr(sync_mod, "sync_5m_intraday", _intraday)
    return calls


def test_stage1_refuses_a_missing_cache_and_creates_nothing(tmp_path, silent_sync):
    cache_dir = tmp_path / "data"
    cache_dir.mkdir()
    db_file = cache_dir / "cache.duckdb"

    summary = health_mod.refresh_stage1_data(
        symbols=["600000.SH"],
        as_of_date="2026-10-26",
        data_provider="marketdb",
        cache_dir=str(cache_dir),
        market="china",
        no_cache=False,
        # allow_create_cache defaults to False -> refused
    )

    assert summary["status"] == "refused"
    assert summary["reason"] == "cache_missing_needs_allow_create"
    assert summary["cache_created"] is False
    assert summary["cache_file"] == str(db_file)
    # The decisive assertion: refusing must not build the thing it refused to build.
    assert not db_file.exists()
    assert list(cache_dir.iterdir()) == []
    assert silent_sync == {"indices": 0, "equity": 0, "intraday": 0}


def test_stage1_creates_the_cache_only_with_the_explicit_flag(tmp_path, silent_sync):
    cache_dir = tmp_path / "data"
    db_file = cache_dir / "cache.duckdb"

    summary = health_mod.refresh_stage1_data(
        symbols=["600000.SH"],
        as_of_date="2026-10-26",
        data_provider="marketdb",
        cache_dir=str(cache_dir),
        market="china",
        no_cache=False,
        allow_create_cache=True,
    )

    assert summary["status"] == "completed"
    assert summary["cache_created"] is True
    assert db_file.exists()
    # The refresh routines ran; the file was created by this call, deliberately.
    assert silent_sync["indices"] == 1
    assert silent_sync["equity"] == 1


def test_stage1_refreshes_an_existing_cache_without_being_refused(tmp_path, silent_sync):
    cache_dir = tmp_path / "data"
    cache_dir.mkdir()
    db_file = cache_dir / "cache.duckdb"
    DuckDBCache(str(cache_dir))  # pre-existing (possibly empty) live cache
    assert db_file.exists()

    summary = health_mod.refresh_stage1_data(
        symbols=["600000.SH"],
        as_of_date="2026-10-26",
        data_provider="marketdb",
        cache_dir=str(cache_dir),
        market="china",
        no_cache=False,
    )

    assert summary["status"] == "completed"
    assert summary["cache_created"] is False
    assert silent_sync["indices"] == 1


def test_stage1_flag_is_wired_into_the_cli():
    """`--allow-create-cache` exists, is a store_true flag, and defaults to False."""
    default = health_mod.parse_args(["--strategy-file", "dummy.json"])
    assert default.allow_create_cache is False

    explicit = health_mod.parse_args(["--strategy-file", "dummy.json", "--allow-create-cache"])
    assert explicit.allow_create_cache is True


def test_stage1_main_aborts_before_loading_data_when_refresh_is_refused(monkeypatch, tmp_path):
    """A refused refresh must ABORT stage 1, not carry on and create the cache a line later.

    `load_live_universe` goes through a WRITABLE `CachedDataProvider`; if `main()` merely
    printed the refusal and kept going, the very next call would recreate the empty
    `cache.duckdb` the operator deleted -- undoing the deletion this run just protected.
    """
    universe_file = tmp_path / "universe.txt"
    universe_file.write_text("600000.SH\n601857.SH\n", encoding="utf-8")

    refused = {
        "status": "refused",
        "reason": "cache_missing_needs_allow_create",
        "cache_file": str(tmp_path / "data" / "cache.duckdb"),
        "cache_created": False,
    }
    monkeypatch.setattr(health_mod, "refresh_stage1_data", lambda **kwargs: dict(refused))

    loaded = []
    monkeypatch.setattr(health_mod, "load_live_universe", lambda *a, **k: loaded.append(1) or {})

    with pytest.raises(SystemExit) as excinfo:
        health_mod.main(
            [
                "--universe-file", str(universe_file),
                "--output-dir", str(tmp_path / "reports"),
                "--start-date", "2026-01-01",
                "--data-provider", "synthetic",
                "--no-save-state",
            ]
        )

    assert excinfo.value.code != 0
    assert loaded == [], "stage 1 continued past a refused refresh"
