"""5m loading is CACHE-FIRST, bounded, and interruptible (2026-11-15 incident).

The confirmed defect (live `py-spy` dumps, `scripts/check_live_portfolio_health.py` Stage 1):
`load_intraday_5m_universe` queried the PROVIDER first and only fell back to the local DuckDB
cache when that call returned empty or raised. A live Stage-1 run therefore re-downloaded two
years x 20 symbols of BaoStock 5m bars (`send_msg` <- `_result_rows_to_df` <-
`_fetch_ohlcv_internal` <- `fetch_ohlcv` <- this loader) for 30+ minutes, while
`data/cache.duckdb` already held every one of those bars through the previous session's 15:00
close. There was also no per-symbol progress output, so a slow load was indistinguishable from
a hang.

Pinned here (100% offline: synthetic 5m sessions, `tmp_path` DuckDB, stub providers -- no
network, no real market data):

1. a warm cache means the provider is NEVER called, and the returned frame is the cached
   slice (same values, order, columns, dtypes, index name);
2. a cache miss asks the provider exactly once per symbol, and only for the BOUNDED window
   (`max_lookback_days`, 90 calendar days) -- never the caller's raw two-year start;
3. the cache-hit path keeps every provenance attr the callers consume;
4. `max_wall_seconds` raises `TimeoutError` naming the symbol instead of grinding;
5. `verbose=True` prints one `[INTRADAY 5M] i/N <symbol> <- <origin>` line per symbol.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from common import intraday_features as intraday_mod
from common.duckdb_cache import DuckDBCache


# ---------------------------------------------------------------------------
# Offline fixtures: synthetic A-share 5m sessions + call-recording provider stubs
# ---------------------------------------------------------------------------


def _day_5m_bars(day: str, base: float = 100.0) -> pd.DataFrame:
    """One synthetic A-share session: 48 five-minute bars (09:35..11:30, 13:05..15:00)."""
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


class _RecordingProvider:
    """Provider stub whose CLASS NAME is the DuckDB cache key the loader must query.

    Records every `fetch_ohlcv` call so a test can assert the network was skipped entirely
    (`calls == []`) or asked for the bounded window only. `payload(symbol)` decides what a
    call returns; the default is an empty frame, i.e. "nothing upstream".
    """

    __name__ = "BaoStockDataProvider"

    def __init__(self, payload=None, raises: Exception | None = None):
        self.calls: list[tuple[str, str | None, str | None, str]] = []
        self._payload = payload
        self._raises = raises

    def fetch_ohlcv(self, symbol, start=None, end=None, interval="1d", **kwargs):
        self.calls.append((symbol, start, end, interval))
        if self._raises is not None:
            raise self._raises
        if self._payload is None:
            return pd.DataFrame()
        return self._payload(symbol, start, end)


def _provider(payload=None, raises=None) -> _RecordingProvider:
    provider = _RecordingProvider(payload, raises)
    type(provider).__name__ = "BaoStockDataProvider"
    return provider


def _seed_cache(tmp_path, provider: str, symbol: str, frame: pd.DataFrame) -> str:
    """Write synthetic 5m bars into a `tmp_path` DuckDB; returns the cache DIRECTORY."""
    db_path = tmp_path / "cache.duckdb"
    cache = DuckDBCache(str(db_path))
    cache.store_intraday_bars(provider, symbol, frame, interval="5m", adjustflag="qfq")
    return str(tmp_path)


def _load(symbols, **kwargs):
    """Call the loader with the parquet layer disabled (no repo-relative side effects)."""
    kwargs.setdefault("parquet_dir", "no_such_parquet_dir")
    return intraday_mod.load_intraday_5m_universe(symbols, **kwargs)


# ---------------------------------------------------------------------------
# 1. Warm cache -> the provider is never touched and the frame is the cached slice
# ---------------------------------------------------------------------------


def test_warm_cache_never_calls_the_provider_and_returns_the_cached_rows(tmp_path):
    days = ["2026-11-02", "2026-11-03", "2026-11-13"]
    cache_dir = _seed_cache(tmp_path, "BaoStockDataProvider", "AAA", _session_frame(days))
    provider = _provider(payload=lambda *a: pytest.fail("provider must not be called"))

    out = _load(
        ["AAA"],
        start="2024-01-01",  # the live run's raw two-year start -- must not reach the network
        end="2026-11-13",
        data_provider=provider,
        cache_dir=cache_dir,
    )

    assert provider.calls == [], (
        f"a warm cache must not send a single provider request, got {provider.calls}"
    )

    frame = out["AAA"]
    expected = _session_frame(days)
    pd.testing.assert_frame_equal(frame, expected, check_freq=False, check_names=False)
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert frame.index.name == "Date"
    assert frame.index.equals(expected.index)
    assert frame.index.is_monotonic_increasing
    assert frame.dtypes.to_dict() == expected.dtypes.to_dict()


def test_warm_cache_returns_exactly_what_the_cache_query_returns(tmp_path):
    """The returned frame is the `DuckDBCache.query_intraday_bars` result, bit for bit."""
    cache_dir = _seed_cache(
        tmp_path, "BaoStockDataProvider", "AAA", _session_frame(["2026-11-12", "2026-11-13"])
    )
    out = _load(
        ["AAA"],
        start="2024-01-01",
        end="2026-11-13",
        data_provider=_provider(),
        cache_dir=cache_dir,
    )

    reader = DuckDBCache(str(tmp_path / "cache.duckdb"), read_only=True)
    direct = reader.query_intraday_bars(
        "BaoStockDataProvider", "AAA", interval="5m", start="2024-01-01", end="2026-11-13"
    )
    pd.testing.assert_frame_equal(out["AAA"], direct, check_freq=False)


# ---------------------------------------------------------------------------
# 2. Cache miss -> one provider call per symbol, over the BOUNDED window only
# ---------------------------------------------------------------------------


def test_cache_miss_fetches_only_the_bounded_window_not_the_two_year_span(tmp_path):
    # Schema only: opens read-only, holds zero 5m rows -> a real cache miss for every symbol.
    DuckDBCache(str(tmp_path / "cache.duckdb"))
    provider = _provider(payload=lambda symbol, start, end: _session_frame(["2026-11-13"]))

    out = _load(
        ["AAA", "BBB"],
        start="2024-01-01",
        end="2026-11-13",
        data_provider=provider,
        cache_dir=str(tmp_path),
    )

    # One request per symbol -- not a request per candidate cache path, not a retry loop.
    assert [call[0] for call in provider.calls] == ["AAA", "BBB"]
    assert all(call[3] == "5m" for call in provider.calls)

    # The clamp: `end - INTRADAY_5M_MAX_LOOKBACK_DAYS` (90 calendar days), NOT the caller's
    # 2024-01-01. This is the exact number that turned a 30-minute download into a few bars.
    expected_start = (
        pd.Timestamp("2026-11-13") - pd.Timedelta(days=intraday_mod.INTRADAY_5M_MAX_LOOKBACK_DAYS)
    ).date().isoformat()
    assert expected_start == "2026-08-15"
    for symbol, start, end, _interval in provider.calls:
        assert start == expected_start, f"{symbol}: provider asked for {start}"
        assert start != "2024-01-01"
        assert end == "2026-11-13"
        assert pd.Timestamp(start) > pd.Timestamp("2024-01-01")

    # A pure provider delivery keeps the historical provenance labels.
    frame = out["AAA"]
    assert not frame.empty
    assert frame.attrs["intraday_5m_source"] == "data_provider"
    assert frame.attrs["intraday_5m_provider"] == "BaoStockDataProvider"
    assert "intraday_5m_gap_filled_from" not in frame.attrs


def test_cache_miss_can_be_given_an_unbounded_window_explicitly(tmp_path):
    """`max_lookback_days=None` is the documented escape hatch -- nothing silently crippled."""
    DuckDBCache(str(tmp_path / "cache.duckdb"))
    provider = _provider(payload=lambda symbol, start, end: _session_frame(["2026-11-13"]))

    _load(
        ["AAA"],
        start="2024-01-01",
        end="2026-11-13",
        data_provider=provider,
        cache_dir=str(tmp_path),
        max_lookback_days=None,
    )

    assert provider.calls[0][1] == "2024-01-01"


def test_cache_warm_up_to_the_previous_session_fetches_only_the_new_session(tmp_path):
    """A gap fill asks for the uncovered tail only, and discloses where those rows came from."""
    cache_dir = _seed_cache(
        tmp_path, "BaoStockDataProvider", "AAA", _session_frame(["2026-11-12", "2026-11-13"])
    )
    # No 7-day tolerance here: an explicit `expected_last_session` is a session by contract.
    provider = _provider(payload=lambda symbol, start, end: _session_frame(["2026-11-16"]))

    out = _load(
        ["AAA"],
        start="2026-10-01",
        end="2026-11-16",
        data_provider=provider,
        cache_dir=cache_dir,
        expected_last_session="2026-11-16",
    )

    assert len(provider.calls) == 1
    _symbol, start, end, _interval = provider.calls[0]
    assert start == "2026-11-14"  # the day AFTER the last cached bar (2026-11-13)
    assert end == "2026-11-16"

    frame = out["AAA"]
    assert frame.attrs["intraday_5m_source"] == "duckdb_cache+data_provider"
    assert frame.attrs["intraday_5m_gap_filled_from"] == "BaoStockDataProvider"
    assert frame.attrs["intraday_5m_provider"] == "BaoStockDataProvider"
    # Cache rows are preserved, the fetched session is appended, order is ascending.
    assert frame.index.min() == pd.Timestamp("2026-11-12 09:35")
    assert frame.index.max() == pd.Timestamp("2026-11-16 15:00")
    assert frame.index.is_monotonic_increasing
    assert len(frame) == 96 + 48


# ---------------------------------------------------------------------------
# 3. Provenance is preserved on the cache-hit path
# ---------------------------------------------------------------------------


def test_cache_hit_keeps_provenance_attrs(tmp_path):
    cache_dir = _seed_cache(
        tmp_path, "BaoStockDataProvider", "AAA", _session_frame(["2026-11-12", "2026-11-13"])
    )

    out = _load(
        ["AAA"],
        start="2026-11-01",
        end="2026-11-13",
        data_provider=_provider(),
        cache_dir=cache_dir,
        expected_last_session="2026-11-13",
    )

    attrs = out["AAA"].attrs
    assert attrs["intraday_5m_available"] is True
    assert attrs["intraday_5m_source"] == "duckdb_cache"
    assert attrs["intraday_5m_provider"] == "BaoStockDataProvider"
    assert attrs["intraday_5m_latest_ts"] == "2026-11-13T15:00:00"
    # Same session as the expectation -> fresh, and no fallback bookkeeping at all.
    assert attrs["intraday_5m_stale"] is False
    assert attrs["intraday_5m_lag_calendar_days"] == 0
    assert "fallback_from" not in attrs
    assert "fallback_to" not in attrs
    assert "fallback_reason" not in attrs
    assert "intraday_5m_gap_filled_from" not in attrs


# ---------------------------------------------------------------------------
# 4. `max_wall_seconds` -> TimeoutError, not an indefinite grind
# ---------------------------------------------------------------------------


class _FakeClock:
    """Deterministic `time.monotonic` stand-in (10s per read) for the budget test."""

    def __init__(self, step: float = 10.0):
        self._t = 0.0
        self._step = step

    def monotonic(self) -> float:
        self._t += self._step
        return self._t


def test_max_wall_seconds_raises_timeout_error_naming_the_symbol(tmp_path, monkeypatch):
    DuckDBCache(str(tmp_path / "cache.duckdb"))  # empty -> the gap would be fetched
    provider = _provider(payload=lambda *a: pytest.fail("budget must bite before any fetch"))
    monkeypatch.setattr(intraday_mod, "time", _FakeClock())

    with pytest.raises(TimeoutError) as excinfo:
        _load(
            ["AAA", "BBB"],
            start="2026-09-01",
            end="2026-11-13",
            data_provider=provider,
            cache_dir=str(tmp_path),
            max_wall_seconds=1.0,
        )

    message = str(excinfo.value)
    assert "[INTRADAY 5M]" in message
    assert "AAA" in message
    assert "elapsed" in message
    assert "max_wall_seconds=1.0" in message
    assert provider.calls == []


def test_max_wall_seconds_is_off_by_default(tmp_path, monkeypatch):
    """No budget -> no timeout, so every existing caller keeps its behaviour."""
    cache_dir = _seed_cache(tmp_path, "BaoStockDataProvider", "AAA", _session_frame(["2026-11-13"]))
    monkeypatch.setattr(intraday_mod, "time", _FakeClock(step=1000.0))

    out = _load(
        ["AAA"], start="2026-11-01", end="2026-11-13", cache_dir=cache_dir
    )

    assert not out["AAA"].empty


# ---------------------------------------------------------------------------
# 5. `verbose=True` -> one progress line per symbol
# ---------------------------------------------------------------------------


def test_verbose_prints_one_line_per_symbol(tmp_path, capsys):
    days = ["2026-11-12", "2026-11-13"]
    cache = DuckDBCache(str(tmp_path / "cache.duckdb"))
    for symbol in ("AAA", "BBB"):
        cache.store_intraday_bars(
            "BaoStockDataProvider", symbol, _session_frame(days), interval="5m", adjustflag="qfq"
        )

    _load(
        ["AAA", "BBB"],
        start="2026-11-01",
        end="2026-11-13",
        data_provider=_provider(),
        cache_dir=str(tmp_path),
        verbose=True,
    )

    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("[INTRADAY 5M]")
    ]
    assert lines == ["[INTRADAY 5M] 1/2 AAA <- cache", "[INTRADAY 5M] 2/2 BBB <- cache"]


def test_quiet_by_default(tmp_path, capsys):
    cache_dir = _seed_cache(tmp_path, "BaoStockDataProvider", "AAA", _session_frame(["2026-11-13"]))

    _load(["AAA"], start="2026-11-01", end="2026-11-13", cache_dir=cache_dir)

    assert "[INTRADAY 5M]" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 6. `cache_dir` is HONOURED (no CWD-relative resolution) and `verbose` progress
# ---------------------------------------------------------------------------


def test_cache_dir_is_honoured_from_an_unrelated_working_directory(tmp_path, monkeypatch):
    """A run started from another directory must still find the warm cache.

    The defect: `cache_dir` was accepted and then ignored, so the candidates resolved relative to
    the process CWD (`data/cache.duckdb`). A live run launched from a service directory therefore
    missed the repo cache entirely and went to the network for every symbol.
    """
    cache_root = tmp_path / "shared"
    cache_root.mkdir()
    _seed_cache(cache_root, "BaoStockDataProvider", "AAA", _session_frame(["2026-11-13"]))
    elsewhere = tmp_path / "service_cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    provider = _provider(payload=lambda *a: pytest.fail("cache_dir was not honoured"))
    out = _load(
        ["AAA"],
        start="2026-11-01",
        end="2026-11-13",
        data_provider=provider,
        cache_dir=str(cache_root),
    )

    assert provider.calls == []
    assert out["AAA"].attrs["intraday_5m_source"] == "duckdb_cache"
    assert not (elsewhere / "data").exists()  # nothing was created CWD-relative


def test_cache_dir_may_name_the_duckdb_file_itself(tmp_path):
    """Same rule as `resolve_cache_db_file`: a `.duckdb` path is used verbatim.

    Otherwise `cache_dir` would get a second `cache.duckdb` appended (`.../cache.duckdb/cache.duckdb`).
    """
    _seed_cache(tmp_path, "BaoStockDataProvider", "AAA", _session_frame(["2026-11-13"]))
    provider = _provider(payload=lambda *a: pytest.fail("the .duckdb file path must be used"))

    out = _load(
        ["AAA"],
        start="2026-11-01",
        end="2026-11-13",
        data_provider=provider,
        cache_dir=str(tmp_path / "cache.duckdb"),
    )

    assert provider.calls == []
    assert not out["AAA"].empty


def test_twenty_symbol_warm_cache_sends_zero_provider_requests(tmp_path):
    """The measured acceptance case: 20 symbols over a warm cache -> 0 network calls."""
    symbols = [f"{i:06d}.SZ" for i in range(1, 21)]
    cache = DuckDBCache(str(tmp_path / "cache.duckdb"))
    frame = _session_frame(["2026-11-12", "2026-11-13"])
    for symbol in symbols:
        cache.store_intraday_bars(
            "BaoStockDataProvider", symbol, frame, interval="5m", adjustflag="qfq"
        )
    provider = _provider(payload=lambda *a: pytest.fail("warm cache must not touch the network"))

    out = _load(
        symbols,
        start="2024-01-01",  # Stage 1's raw `--start-date`
        end="2026-11-13",
        data_provider=provider,
        cache_dir=str(tmp_path),
        verbose=True,
    )

    assert provider.calls == []
    assert len(out) == 20
    assert all(not f.empty for f in out.values())


# ---------------------------------------------------------------------------
# 7. The PUBLIC clamp `intraday_5m_bounded_start` (what Stage 1 now passes in)
# ---------------------------------------------------------------------------


def test_bounded_start_clamps_a_two_year_window_to_the_lookback():
    assert (
        intraday_mod.intraday_5m_bounded_start("2024-01-01", "2026-11-13") == "2026-08-15"
    ), "90 calendar days back from the window's end"


def test_bounded_start_keeps_a_window_that_is_already_short():
    assert intraday_mod.intraday_5m_bounded_start("2026-10-01", "2026-11-13") == "2026-10-01"


def test_bounded_start_clamps_a_missing_start_to_the_floor():
    """`start=None` means "everything" for the provider -> the floor is the only bound left."""
    assert intraday_mod.intraday_5m_bounded_start(None, "2026-11-13") == "2026-08-15"


def test_bounded_start_none_disables_the_bound():
    assert intraday_mod.intraday_5m_bounded_start("2024-01-01", "2026-11-13", max_lookback_days=None) == "2024-01-01"


def test_bounded_start_matches_the_loader_network_floor():
    """The public clamp and the loader's own bound are the same arithmetic by construction."""
    for start, end in (
        ("2024-01-01", "2026-11-13"),
        ("2026-10-01", "2026-11-13"),
        (None, "2026-11-13"),
    ):
        loader_floor = intraday_mod._intraday_5m_network_window(start, end)[0]
        assert intraday_mod.intraday_5m_bounded_start(start, end) == loader_floor

