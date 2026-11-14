"""2026-11-13: `adjust_factors` can lag `ohlcv_bars` silently -- the read-only census.

WHY THIS SUITE EXISTS
---------------------
`adjust_factors` is written by a provider path that is completely separate from `ohlcv_bars`:
the live Fuyao factor source is a pure delegation to the local MarketDB
`calc_adjust_factor_daily` table
(`common/financial_api.py::FuyaoDataProvider.fetch_adjust_factors`),
while raw bars keep arriving from the Fuyao REST feed. Nothing compared their two maxima.
Measured on the production cache 2026-11-13: factors end **2026-10-27**, bars end
**2026-11-12** -- 12 sessions -- and every helper stayed quiet, because
`get_latest_factor_hfq` is non-None for all 20 live symbols, so `MissingAdjustFactorsError`
never fired. The qfq read is `raw * factor_hfq[t] / factor_hfq[latest]`, i.e. the anchor is the
latest STORED factor: the stale tail is invisible in the output and only becomes a priced error
once a corporate action lands inside the gap.

`common.duckdb_cache.inspect_adjust_factor_freshness` makes that state nameable. It is an
OBSERVATION: it never raises, writes nothing, and feeds no gate (asserted here, because a future
edit that quietly turns it into one would change Stage-2 behaviour).

100% offline: temporary / on-disk DuckDB caches only. No providers, no network.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pandas as pd
import pytest

from common.duckdb_cache import (
    ADJUST_FACTOR_FRESHNESS_LAGGING,
    ADJUST_FACTOR_FRESHNESS_MISSING,
    ADJUST_FACTOR_FRESHNESS_OK,
    ADJUST_FACTOR_FRESHNESS_UNKNOWN,
    DuckDBCache,
    ImplausibleAdjustFactorStep,
    format_adjust_factor_freshness_warning,
    inspect_adjust_factor_freshness,
)

PROVIDER = "FuyaoDataProvider"
SYMBOL = "600028.SH"

#: The measured production shape: the dense factor series stops on 2026-10-27 ...
FACTOR_DATES = pd.to_datetime(
    [
        "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27",
    ]
)
#: ... while the bar series continues through 2026-11-12: exactly 12 sessions of qfq bars whose
#: derivation carries the 2026-10-27 factor forward as the as-of hit and the anchor.
GAP_SESSIONS = [
    "2026-10-28", "2026-10-29", "2026-10-30",
    "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05",
    "2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12",
]
BAR_DATES = pd.to_datetime([d.strftime("%Y-%m-%d") for d in FACTOR_DATES] + GAP_SESSIONS)


def _bars(dates) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [5.40] * len(dates),
            "High": [5.60] * len(dates),
            "Low": [5.30] * len(dates),
            "Close": [5.51] * len(dates),
            "Volume": [1_000_000.0] * len(dates),
        },
        index=pd.to_datetime(list(dates)),
    )


def _factors(values, dates) -> pd.DataFrame:
    return pd.DataFrame({"factor_hfq": values}, index=pd.to_datetime(list(dates)))


def _seed(
    tmp_path,
    *,
    symbols=(SYMBOL,),
    factor_dates=FACTOR_DATES,
    bar_dates=BAR_DATES,
    bar_provider=PROVIDER,
    extra_bars=None,
):
    """Build a real on-disk cache through the public write API; return its file path."""
    db_file = str(tmp_path / "cache.duckdb")
    cache = DuckDBCache(db_file)
    for symbol in symbols:
        if bar_dates is not None:
            cache.store_bars(
                bar_provider, symbol, _bars(bar_dates), adjustflag="raw"
            )
        if factor_dates is not None:
            cache.store_adjust_factors(
                symbol,
                _factors([1.897382] * len(factor_dates), factor_dates),
                source=PROVIDER,
            )
    for provider, symbol, dates in extra_bars or ():
        cache.store_bars(provider, symbol, _bars(dates), adjustflag="raw")
    return cache.db_path


def _assert_lagging_shape(block) -> None:
    """The exact measured shape -- shared with the red-check so both assert the SAME thing."""
    assert block["status"] == ADJUST_FACTOR_FRESHNESS_LAGGING
    assert block["n_lagging"] == 1
    assert block["max_lag_sessions"] == 12, block
    assert block["max_lag_days"] == 16, block
    entry = block["per_symbol"][SYMBOL]
    assert entry["factor_max_date"] == "2026-10-27"
    assert entry["bar_max_date"] == "2026-11-12"
    assert entry["lag_sessions"] == 12
    worst = block["worst_symbols"][0]
    assert (worst["symbol"], worst["lag_sessions"]) == (SYMBOL, 12)


# ---------------------------------------------------------------------------
# 1. ok -- both tables reach the same session
# ---------------------------------------------------------------------------
def test_ok_when_factor_and_bar_maxima_agree(tmp_path):
    db_file = _seed(tmp_path, factor_dates=BAR_DATES)
    block = inspect_adjust_factor_freshness(db_file)

    assert block["status"] == ADJUST_FACTOR_FRESHNESS_OK
    assert (block["n_symbols"], block["n_ok"], block["n_lagging"], block["n_missing"]) == (
        1,
        1,
        0,
        0,
    )
    assert block["max_lag_sessions"] == 0
    assert block["worst_symbols"] == []
    assert block["per_symbol"][SYMBOL]["lag_sessions"] == 0


# ---------------------------------------------------------------------------
# 2. lagging -- the exact 2026-10-27 vs 2026-11-12 production shape
# ---------------------------------------------------------------------------
def test_lagging_matches_the_measured_production_shape(tmp_path):
    db_file = _seed(tmp_path)
    block = inspect_adjust_factor_freshness(db_file)

    _assert_lagging_shape(block)
    # The block is JSON-safe as-is: dates are ISO strings, so Stage 1 can persist it verbatim.
    assert isinstance(block["per_symbol"][SYMBOL]["factor_max_date"], str)
    assert isinstance(block["worst_symbols"][0]["bar_max_date"], str)


def test_bar_provider_selects_the_namespace_that_is_compared(tmp_path):
    """A fresher OTHER namespace must not decide the lag when the caller pins the provider."""
    db_file = _seed(
        tmp_path,
        extra_bars=[("MarketDBDataProvider", SYMBOL, FACTOR_DATES)],
    )

    pinned = inspect_adjust_factor_freshness(db_file, bar_provider=PROVIDER)
    assert pinned["per_symbol"][SYMBOL]["lag_sessions"] == 12

    other = inspect_adjust_factor_freshness(db_file, bar_provider="MarketDBDataProvider")
    assert other["per_symbol"][SYMBOL]["bar_max_date"] == "2026-10-27"
    assert other["status"] == ADJUST_FACTOR_FRESHNESS_OK


# ---------------------------------------------------------------------------
# 3. missing -- bars without any factor rows (the silent-anchor precondition)
# ---------------------------------------------------------------------------
def test_missing_factor_rows_for_a_non_neutral_symbol(tmp_path):
    db_file = _seed(tmp_path, factor_dates=None)
    block = inspect_adjust_factor_freshness(db_file)

    assert block["status"] == ADJUST_FACTOR_FRESHNESS_MISSING
    assert (block["n_lagging"], block["n_missing"]) == (0, 1)
    entry = block["per_symbol"][SYMBOL]
    assert entry["reason"] == "no_factor_rows"
    assert entry["lag_sessions"] is None
    assert entry["bar_max_date"] == "2026-11-12"

    warning = format_adjust_factor_freshness_warning(block)
    assert warning and "[WARN]" in warning and SYMBOL in warning


def test_factor_neutral_symbols_are_out_of_scope(tmp_path):
    """An index has no factor rows BY DESIGN -- it must not manufacture a `missing` status."""
    db_file = _seed(
        tmp_path,
        symbols=(SYMBOL,),
        factor_dates=BAR_DATES,
        extra_bars=[(PROVIDER, "000300.SH", BAR_DATES)],
    )
    block = inspect_adjust_factor_freshness(db_file)

    assert "000300.SH" not in block["per_symbol"]
    assert block["status"] == ADJUST_FACTOR_FRESHNESS_OK


# ---------------------------------------------------------------------------
# 4. never raises, writes nothing -- the observation contract
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_path", ["/nonexistent/cache.duckdb", ":memory:", ""])
def test_unreadable_input_is_unknown_never_an_exception(bad_path):
    block = inspect_adjust_factor_freshness(bad_path)
    assert block["status"] == ADJUST_FACTOR_FRESHNESS_UNKNOWN
    assert block["error"]
    assert block["gates_orders"] is False


def test_census_does_not_write(tmp_path):
    db_file = _seed(tmp_path)
    before = os.path.getmtime(db_file), os.path.getsize(db_file)
    inspect_adjust_factor_freshness(db_file)
    assert (os.path.getmtime(db_file), os.path.getsize(db_file)) == before
    assert not os.path.exists(db_file + ".wal")


# ---------------------------------------------------------------------------
# 5. the printed line: one, grep-able, and only for a real finding
# ---------------------------------------------------------------------------
def test_warning_line_is_none_for_ok_and_unknown(tmp_path):
    ok = inspect_adjust_factor_freshness(_seed(tmp_path, factor_dates=BAR_DATES))
    assert format_adjust_factor_freshness_warning(ok) is None
    assert (
        format_adjust_factor_freshness_warning({"status": ADJUST_FACTOR_FRESHNESS_UNKNOWN}) is None
    )
    assert format_adjust_factor_freshness_warning(None) is None


def test_warning_line_names_the_numbers_and_the_no_gate_scope(tmp_path):
    block = inspect_adjust_factor_freshness(_seed(tmp_path))
    line = format_adjust_factor_freshness_warning(block)

    assert line.startswith("[WARN] adjust_factor_freshness=lagging:")
    assert "1/1" in line
    assert "12" in line
    assert "2026-10-27" in line and "2026-11-12" in line
    assert "不参与任何门禁" in line
    assert "\n" not in line


# ---------------------------------------------------------------------------
# 6. RED CHECK -- removing the comparison flips test 2
# ---------------------------------------------------------------------------
def test_red_check_removing_the_comparison_turns_the_lagging_assertions_red(tmp_path, monkeypatch):
    """Proves suite item 2 depends on the new comparison, not on the fixture.

    The pre-fix state of the world was "bars are fresh, so nothing to report": the code never
    looked at `adjust_factors` at all, which is behaviourally a census that always answers `ok`.
    Substituting exactly that answer must make `_assert_lagging_shape` fail -- otherwise the
    lagging test would keep passing with the comparison deleted and would prove nothing.
    """
    db_file = _seed(tmp_path)

    def _blind_census(*_args, **_kwargs):
        # What the code returned before the comparison existed: no factor/bar join, so no lag.
        return {
            "status": ADJUST_FACTOR_FRESHNESS_OK,
            "enforcement": "observation_only",
            "gates_orders": False,
            "n_symbols": 1,
            "n_ok": 1,
            "n_lagging": 0,
            "n_missing": 0,
            "max_lag_sessions": 0,
            "max_lag_days": 0,
            "worst_symbols": [],
            "per_symbol": {},
            "error": None,
        }

    monkeypatch.setattr(
        "common.duckdb_cache.inspect_adjust_factor_freshness", _blind_census
    )
    import common.duckdb_cache as duckdb_cache_module

    blind = duckdb_cache_module.inspect_adjust_factor_freshness(db_file)
    assert blind["status"] == ADJUST_FACTOR_FRESHNESS_OK

    with pytest.raises(AssertionError):
        _assert_lagging_shape(blind)

    # Same fixture, real comparison: green again. (Fixture is therefore not the variable.)
    monkeypatch.undo()
    _assert_lagging_shape(inspect_adjust_factor_freshness(db_file))


# ---------------------------------------------------------------------------
# 7. the `<2024` foreign-basis-prefix guidance on ImplausibleAdjustFactorStep
# ---------------------------------------------------------------------------
def test_implausible_message_keeps_fields_and_names_the_prefix_workaround():
    exc = ImplausibleAdjustFactorStep(
        "600660.SH",
        "2026-09-24",
        81.588562,
        1.3894851,
        source="BaoStockDataProvider",
        n_offending=1,
        n_factors=11,
        bar_window="1993-06-10..2026-09-30",
    )

    # Fields unchanged (audit consumers read these attributes).
    assert exc.symbol == "600660.SH"
    assert exc.value == pytest.approx(81.588562)
    assert exc.median == pytest.approx(1.3894851)
    assert exc.source == "BaoStockDataProvider"
    assert exc.n_offending == 1
    assert exc.n_factors == 11
    assert exc.bar_window == "1993-06-10..2026-09-30"

    message = str(exc)
    # Pre-existing content survives verbatim ...
    assert "Implausible adjustment factor for '600660.SH' on 2026-09-24" in message
    assert "repair_cross_source_adjust_factors.py" in message
    assert "4.0-4.0x" not in message  # band rendered as "0.25-4.0x"
    assert "0.25-4.0x" in message
    # ... and the new guidance names the cause AND the workaround.
    assert "foreign-basis PREFIX" in message
    assert "2024-01-02" in message
    assert "`raw`" in message
