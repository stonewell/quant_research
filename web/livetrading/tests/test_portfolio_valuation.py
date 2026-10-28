"""Offline tests for the dividend-aware NAV engine (``web/livetrading/valuation.py``).

Hermetic by construction: every case drives either an IN-MEMORY ``DuckDBCache``
(``db_path=":memory:"``, tables seeded through the public ``store_bars`` /
``store_adjust_factors`` writers) or a hand-written duck-typed fake cache.  No
file under ``data/`` is opened, no network call is made.

The scenario numbers are tiny and hand-computable on purpose -- the point of
this module is that a corporate action must not move the mark, and that is only
verifiable against arithmetic a human can redo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common.duckdb_cache import DuckDBCache  # noqa: E402
from common.freshness import StalePriceError  # noqa: E402

from web.livetrading.valuation import (  # noqa: E402
    MissingCashError,
    compute_nav,
)

PROVIDER = "test"  # not the synthetic provider, which store_bars() skips
SYM = "AAA"


# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------
def _bars(rows: dict) -> pd.DataFrame:
    """OHLCV frame from ``{date: close}`` (O=H=L=C, so any column is verifiable)."""
    items = list(rows.items())
    idx = pd.DatetimeIndex(pd.to_datetime([d for d, _ in items]), name="date")
    close = pd.Series([float(c) for _, c in items], index=idx)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close,
            "Low": close,
            "Close": close,
            "Volume": 1_000_000.0,
        },
        index=idx,
    )


def _factors(rows: dict) -> pd.DataFrame:
    items = list(rows.items())
    idx = pd.DatetimeIndex(pd.to_datetime([d for d, _ in items]), name="date")
    return pd.DataFrame(
        {"factor_hfq": pd.Series([float(v) for _, v in items], index=idx)}, index=idx
    )


@pytest.fixture()
def cache():
    """A throwaway in-memory cache (never the production ``data/cache.duckdb``)."""
    c = DuckDBCache(":memory:")
    yield c
    try:
        c._mem_con.close()
    except Exception:  # pragma: no cover - best-effort cleanup
        pass


def _seed(cache, symbol, bars: dict, factors: dict | None):
    cache.store_bars(PROVIDER, symbol, _bars(bars), adjustflag="raw")
    if factors is not None:
        cache.store_adjust_factors(symbol, _factors(factors))


# ---------------------------------------------------------------------------
# 1. no dividend -- hand-checkable arithmetic
# ---------------------------------------------------------------------------
def test_no_dividend_simple_math(cache):
    _seed(cache, SYM, {"2026-01-02": 10.0, "2026-01-05": 10.0, "2026-01-06": 10.0},
          {"2026-01-02": 1.0, "2026-01-05": 1.0, "2026-01-06": 1.0})

    res = compute_nav(
        {SYM: 100},
        as_of="2026-01-06",
        cache=cache,
        cash=1000.0,
        anchors={SYM: "2026-01-05"},
        provider=PROVIDER,
    )

    assert res.equity == pytest.approx(1000.0)
    assert res.cash == pytest.approx(1000.0)
    assert res.nav == pytest.approx(2000.0)
    assert res.cash_source == "explicit"
    assert res.nav_source == "computed"
    assert res.as_of == "2026-01-06"
    assert res.stale_symbols == []
    assert res.dividend_alarms == []

    entry = res.per_symbol[SYM]
    assert entry["shares"] == 100
    assert entry["raw_close"] == pytest.approx(10.0)
    assert entry["factor_hfq"] == pytest.approx(1.0)
    assert entry["anchor_date"] == "2026-01-05"
    assert entry["value"] == pytest.approx(1000.0)
    assert entry["stale"] is False


# ---------------------------------------------------------------------------
# 2. a single cash dividend -- THE core invariant: no NAV gap on the ex-date
# ---------------------------------------------------------------------------
def _dividend_scenario():
    """5% cash dividend: factor_hfq 1.0 -> 1.05, raw close 10 -> 10/1.05.

    BaoStock's ``hfq = raw x back`` means total return is continuous exactly when
    ``raw[ex] = raw[prev] x back[prev] / back[ex]``.  A 5% dividend therefore
    prints raw 10.00 -> 9.523809..., i.e. the price drops by the dividend.
    """
    bars = {"2026-01-05": 10.0, "2026-01-06": 10.0 / 1.05}
    factors = {"2026-01-05": 1.0, "2026-01-06": 1.05}
    return bars, factors


def test_single_cash_dividend_has_no_nav_gap(cache):
    bars, factors = _dividend_scenario()
    _seed(cache, SYM, bars, factors)

    before = compute_nav(
        {SYM: 100}, as_of="2026-01-05", cache=cache, cash=0.0,
        anchors={SYM: "2026-01-05"}, provider=PROVIDER,
    )
    after = compute_nav(
        {SYM: 100}, as_of="2026-01-06", cache=cache, cash=0.0,
        anchors={SYM: "2026-01-05"}, provider=PROVIDER,
    )

    # Raw price fell 4.76%, but the total-return mark is unchanged.
    assert after.per_symbol[SYM]["raw_close"] == pytest.approx(10.0 / 1.05)
    assert after.per_symbol[SYM]["raw_close"] < before.per_symbol[SYM]["raw_close"]
    assert after.nav == pytest.approx(before.nav, abs=1e-9)
    assert after.nav == pytest.approx(1000.0, abs=1e-9)

    # ...and the dividend is still observable, as an ALARM only.
    assert len(after.dividend_alarms) == 1
    alarm = after.dividend_alarms[0]
    assert alarm["symbol"] == SYM
    assert alarm["ex_date"] == "2026-01-06"
    assert alarm["raw_prev_close"] == pytest.approx(10.0)


def test_raw_mode_drops_the_dividend_that_total_return_keeps(cache):
    """Documented magnitude of the mode difference: exactly the dividend."""
    bars, factors = _dividend_scenario()
    _seed(cache, SYM, bars, factors)

    tr = compute_nav(
        {SYM: 100}, as_of="2026-01-06", cache=cache, cash=0.0,
        anchors={SYM: "2026-01-05"}, provider=PROVIDER,
    )
    raw = compute_nav(
        {SYM: 100}, as_of="2026-01-06", cache=cache, cash=0.0,
        anchors={SYM: "2026-01-05"}, provider=PROVIDER,
        dividend_mode="raw",
    )

    assert raw.nav == pytest.approx(100.0 * 10.0 / 1.05)
    assert tr.nav - raw.nav == pytest.approx(100.0 * (10.0 - 10.0 / 1.05))
    assert raw.per_symbol[SYM]["factor_hfq"] is None
    assert raw.nav < tr.nav


# ---------------------------------------------------------------------------
# 3. bonus issue / split -- factor moves, shares must NOT
# ---------------------------------------------------------------------------
def test_split_never_double_counts(cache):
    """2:1 split: raw 10 -> 5 with factor 1.0 -> 2.0. Shares stay 100.

    The whole point: the split is ALREADY in ``factor_hfq``.  Adjusting the share
    count as well would credit it twice and print 2000 instead of 1000.
    """
    _seed(cache, SYM, {"2026-01-05": 10.0, "2026-01-06": 5.0},
          {"2026-01-05": 1.0, "2026-01-06": 2.0})

    before = compute_nav(
        {SYM: 100}, as_of="2026-01-05", cache=cache, cash=0.0,
        anchors={SYM: "2026-01-05"}, provider=PROVIDER,
    )
    after = compute_nav(
        {SYM: 100}, as_of="2026-01-06", cache=cache, cash=0.0,
        anchors={SYM: "2026-01-05"}, provider=PROVIDER,
    )

    assert before.nav == pytest.approx(1000.0)
    assert after.nav == pytest.approx(1000.0)

    entry = after.per_symbol[SYM]
    assert entry["shares"] == 100, "shares must never be adjusted for a corporate action"
    assert entry["raw_close"] == pytest.approx(5.0)
    assert entry["factor_hfq"] == pytest.approx(2.0)
    assert entry["factor_anchor_hfq"] == pytest.approx(1.0)
    assert entry["value"] == pytest.approx(1000.0)
    assert entry["value"] != pytest.approx(2000.0), "double-counting guard"

    # A 2:1 split is indistinguishable from a 50% dividend in the factor table,
    # which is exactly why the alarms are ADVISORY and never touch cash/shares.
    assert len(after.dividend_alarms) == 1
    assert after.cash == 0.0
    assert entry["shares"] == 100


# ---------------------------------------------------------------------------
# 4. suspension / missing bar
# ---------------------------------------------------------------------------
def test_missing_bar_fails_closed(cache):
    _seed(cache, SYM, {"2026-01-05": 10.0}, {"2026-01-05": 1.0})

    with pytest.raises(StalePriceError) as excinfo:
        compute_nav(
            {SYM: 100}, as_of="2026-01-06", cache=cache, cash=1000.0,
            anchors={SYM: "2026-01-05"}, provider=PROVIDER,
        )
    assert isinstance(excinfo.value, ValueError)  # subclass, catchable generically
    assert "2026-01-05" in str(excinfo.value)


def test_missing_bar_can_be_marked_stale(cache):
    _seed(cache, SYM, {"2026-01-05": 10.0}, {"2026-01-05": 1.0})

    res = compute_nav(
        {SYM: 100}, as_of="2026-01-06", cache=cache, cash=1000.0,
        anchors={SYM: "2026-01-05"}, provider=PROVIDER, fail_closed=False,
    )

    assert res.stale_symbols == [SYM]
    assert res.per_symbol[SYM]["stale"] is True
    assert res.per_symbol[SYM]["raw_close"] == pytest.approx(10.0)  # last available
    assert res.per_symbol[SYM]["mark_date"] == "2026-01-05"
    assert res.nav == pytest.approx(2000.0)
    assert any(w.startswith("stale_price") for w in res.warnings)


def test_symbol_with_no_price_at_all(cache):
    with pytest.raises(StalePriceError):
        compute_nav({SYM: 100}, as_of="2026-01-06", cache=cache, cash=1.0,
                    provider=PROVIDER)

    res = compute_nav(
        {SYM: 100}, as_of="2026-01-06", cache=cache, cash=1.0,
        provider=PROVIDER, fail_closed=False,
    )
    assert res.stale_symbols == [SYM]
    assert res.per_symbol[SYM]["value"] == 0.0
    assert any(w.startswith("no_price_available") for w in res.warnings)


# ---------------------------------------------------------------------------
# 5. the three cash sources
# ---------------------------------------------------------------------------
def test_cash_explicit(cache):
    _seed(cache, SYM, {"2026-01-06": 10.0}, {"2026-01-06": 1.0})
    res = compute_nav({SYM: 100}, as_of="2026-01-06", cache=cache, cash=1000.0,
                      provider=PROVIDER)
    assert res.cash_source == "explicit"
    assert res.cash == pytest.approx(1000.0)


def test_cash_falls_back_to_parked_cash_holding(cache):
    _seed(cache, SYM, {"2026-01-06": 10.0}, {"2026-01-06": 1.0})
    _seed(cache, "BIL", {"2026-01-06": 100.0}, {"2026-01-06": 1.0})

    res = compute_nav({SYM: 100, "BIL": 10}, as_of="2026-01-06", cache=cache,
                      cash=None, provider=PROVIDER)

    assert res.cash_source == "bil_fallback"
    assert res.cash == pytest.approx(10 * 100.0)
    # BIL was moved OUT of equity: no double counting between equity and cash.
    assert res.equity == pytest.approx(1000.0)
    assert res.per_symbol["BIL"]["counted_as"] == "cash"
    assert res.per_symbol[SYM]["counted_as"] == "equity"
    assert res.nav == pytest.approx(2000.0)
    assert any(w.startswith("cash_fallback") for w in res.warnings)


def test_cash_second_fallback_symbol_is_honoured(cache):
    _seed(cache, SYM, {"2026-01-06": 10.0}, {"2026-01-06": 1.0})
    _seed(cache, "511880", {"2026-01-06": 50.0}, {"2026-01-06": 1.0})

    res = compute_nav({SYM: 100, "511880": 4}, as_of="2026-01-06", cache=cache,
                      cash=None, provider=PROVIDER)
    assert res.cash_source == "bil_fallback"
    assert res.cash == pytest.approx(200.0)


def test_cash_missing_fails_closed(cache):
    _seed(cache, SYM, {"2026-01-06": 10.0}, {"2026-01-06": 1.0})

    with pytest.raises(MissingCashError) as excinfo:
        compute_nav({SYM: 100}, as_of="2026-01-06", cache=cache, cash=None,
                    provider=PROVIDER)
    assert isinstance(excinfo.value, ValueError)

    res = compute_nav({SYM: 100}, as_of="2026-01-06", cache=cache, cash=None,
                      provider=PROVIDER, fail_closed=False)
    assert res.cash_source == "missing"
    assert res.cash == 0.0
    assert res.nav == pytest.approx(1000.0)
    assert any(w.startswith("cash_missing") for w in res.warnings)


# ---------------------------------------------------------------------------
# 6. nav_override
# ---------------------------------------------------------------------------
def test_nav_override_keeps_components_computed(cache):
    _seed(cache, SYM, {"2026-01-06": 10.0}, {"2026-01-06": 1.0})

    res = compute_nav({SYM: 100}, as_of="2026-01-06", cache=cache, cash=1000.0,
                      nav_override=1234.5, provider=PROVIDER)

    assert res.nav == pytest.approx(1234.5)
    assert res.nav_source == "override"
    assert res.equity == pytest.approx(1000.0)
    assert res.cash == pytest.approx(1000.0)
    assert res.per_symbol[SYM]["value"] == pytest.approx(1000.0)
    assert any(w.startswith("nav_override_applied") for w in res.warnings)


# ---------------------------------------------------------------------------
# 7. anchor semantics (P0, 2026-10-27)
# ---------------------------------------------------------------------------
# `factor_hfq` is monotone increasing, so anchoring on the START of the factor history
# (instead of the position-open date) capitalises every PRE-ENTRY dividend into today's
# value. NAV is the denominator of every order's size (`target_weight x NAV / price`), so
# that error over-sizes the whole ticket. The old code did exactly that whenever
# `anchors` was missing -- which, before this fix, was ALWAYS (no entry-date store).
def test_missing_anchor_marks_at_raw_never_earliest_factor_date(cache):
    """HARD REGRESSION: factor 1.0 (2016) -> 1.6 (as_of), 100 shares at 10.00, no anchor.

    The wrong answer is 1600 (+cash); the right answer is 1000 (+cash). Anything else
    means the pre-entry dividend history leaked back into the mark.
    """
    _seed(cache, SYM,
          {"2016-08-31": 6.25, "2026-01-05": 10.0, "2026-01-06": 10.0},
          {"2016-08-31": 1.0, "2026-01-05": 1.6, "2026-01-06": 1.6})

    res = compute_nav({SYM: 100}, as_of="2026-01-06", cache=cache, cash=500.0,
                      provider=PROVIDER)  # no anchors at all

    entry = res.per_symbol[SYM]
    # 100 x 10.00, NOT 100 x 10.00 x 1.6
    assert entry["value"] == pytest.approx(1000.0)
    assert res.equity == pytest.approx(1000.0)
    assert res.nav == pytest.approx(1500.0)
    assert entry["anchor_date"] is None            # nothing was anchored
    assert entry["factor_anchor_hfq"] is None      # ...and no factor was claimed
    assert entry["value_raw"] == pytest.approx(1000.0)
    assert entry["anchor_inflation_ratio"] == pytest.approx(1.0)
    assert entry["dividend_basis"] == "raw"
    # The rejected fallback is still MEASURED, so the operator can size the error.
    assert entry["full_history_ratio"] == pytest.approx(1.6)
    assert any(w.startswith("anchor_unavailable") for w in res.warnings)
    assert not any("earliest factor date 2016-08-31 (NOT the entry price)" in w
                   for w in res.warnings)


def test_supplied_anchor_measures_the_ratio_from_that_date(cache):
    """With a real entry date the total-return ratio applies, measured from THAT date."""
    _seed(cache, SYM,
          {"2016-08-31": 6.25, "2026-01-05": 10.0, "2026-01-06": 10.0},
          {"2016-08-31": 1.0, "2026-01-05": 1.6, "2026-01-06": 1.6})

    res = compute_nav({SYM: 100}, as_of="2026-01-06", cache=cache, cash=0.0,
                      anchors={SYM: "2026-01-05"}, provider=PROVIDER)

    entry = res.per_symbol[SYM]
    assert entry["anchor_date"] == "2026-01-05"
    assert entry["factor_anchor_hfq"] == pytest.approx(1.6)
    assert entry["value"] == pytest.approx(100 * 10.0 * (1.6 / 1.6))
    assert entry["anchor_inflation_ratio"] == pytest.approx(1.0)
    assert entry["dividend_basis"] == "total_return"
    assert not any(w.startswith("anchor_unavailable") for w in res.warnings)


def test_anchor_later_than_as_of_marks_at_raw(cache):
    _seed(cache, SYM, {"2026-01-05": 10.0, "2026-01-06": 10.0},
          {"2026-01-05": 1.0, "2026-01-06": 1.4})

    res = compute_nav({SYM: 100}, as_of="2026-01-06", cache=cache, cash=0.0,
                      anchors={SYM: "2026-03-01"}, provider=PROVIDER)

    assert any(w.startswith("anchor_after_as_of") for w in res.warnings)
    # NOT anchored on the earliest factor row (2026-01-05): that would ship the 1.4x.
    assert res.per_symbol[SYM]["anchor_date"] is None
    assert res.per_symbol[SYM]["value"] == pytest.approx(1000.0)
    assert res.nav == pytest.approx(1000.0)


def test_anchor_before_factor_history_marks_at_raw(cache):
    """The anchor predates the stored factors -> raw + warning, never the earliest row."""
    _seed(cache, SYM, {"2026-01-05": 10.0, "2026-01-06": 10.0},
          {"2026-01-05": 1.0, "2026-01-06": 1.4})

    res = compute_nav({SYM: 100}, as_of="2026-01-06", cache=cache, cash=0.0,
                      anchors={SYM: "2016-08-31"}, provider=PROVIDER)

    entry = res.per_symbol[SYM]
    assert any(w.startswith("anchor_before_factor_history") for w in res.warnings)
    assert entry["anchor_date"] is None
    assert entry["factor_anchor_hfq"] is None
    assert entry["value"] == pytest.approx(1000.0)
    assert entry["value_raw"] == pytest.approx(1000.0)
    assert entry["full_history_ratio"] == pytest.approx(1.4)


# ---------------------------------------------------------------------------
# 8. factor history gaps never silently ship dividends out
# ---------------------------------------------------------------------------
def test_factor_missing_falls_back_to_raw_with_warning(cache):
    _seed(cache, SYM, {"2026-01-06": 10.0}, None)  # prices but no factor table

    res = compute_nav({SYM: 100}, as_of="2026-01-06", cache=cache, cash=0.0,
                      provider=PROVIDER)

    assert res.per_symbol[SYM]["factor_hfq"] is None
    assert res.per_symbol[SYM]["value"] == pytest.approx(1000.0)
    assert any(w.startswith("factor_missing") for w in res.warnings)


# ---------------------------------------------------------------------------
# 9. dividend alarm arithmetic (rtol 1e-6 against hand calculation)
# ---------------------------------------------------------------------------
def test_dividend_alarm_implied_amount(cache):
    bars, factors = _dividend_scenario()
    _seed(cache, SYM, bars, factors)

    res = compute_nav({SYM: 100}, as_of="2026-01-06", cache=cache, cash=0.0,
                      anchors={SYM: "2026-01-05"}, provider=PROVIDER)

    assert len(res.dividend_alarms) == 1
    alarm = res.dividend_alarms[0]
    expected = 10.0 * (1.0 - 1.0 / 1.05)
    assert alarm["implied_dividend_per_share"] == pytest.approx(expected, rel=1e-6)
    assert alarm["ratio"] == pytest.approx(1.0 / 1.05, rel=1e-6)
    assert alarm["raw_prev_close"] == pytest.approx(10.0, rel=1e-6)


def test_dividend_scan_window_is_respected(cache):
    """A step older than ``dividend_scan_days`` is not reported."""
    _seed(cache, SYM,
          {"2026-01-05": 10.0, "2026-01-06": 10.0, "2026-03-02": 10.0},
          {"2026-01-05": 1.0, "2026-01-06": 1.05, "2026-03-02": 1.05})

    wide = compute_nav({SYM: 100}, as_of="2026-03-02", cache=cache, cash=0.0,
                       anchors={SYM: "2026-03-02"}, provider=PROVIDER,
                       dividend_scan_days=120)
    narrow = compute_nav({SYM: 100}, as_of="2026-03-02", cache=cache, cash=0.0,
                         anchors={SYM: "2026-03-02"}, provider=PROVIDER,
                         dividend_scan_days=7)
    assert len(wide.dividend_alarms) == 1
    assert narrow.dividend_alarms == []


# ---------------------------------------------------------------------------
# 10. duck-typed cache: no DuckDB (and no fastapi) needed at all
# ---------------------------------------------------------------------------
class _FakeCache:
    """Minimal ``DuckDBCache``-compatible stand-in (lowercase columns on purpose)."""

    def __init__(self, bars_by_symbol, factors_by_symbol):
        self._bars = {k: _bars(v) for k, v in bars_by_symbol.items()}
        self._factors = {k: _factors(v) for k, v in factors_by_symbol.items()}
        self.adjust_seen = []

    def query_bars(self, provider, symbol, start=None, end=None, interval="1d",
                   adjust="qfq", **kwargs):
        self.adjust_seen.append(adjust)
        frame = self._bars.get(symbol)
        if frame is None:
            return pd.DataFrame(columns=["close"])
        frame = frame.copy()
        frame.columns = [c.lower() for c in frame.columns]
        if start:
            frame = frame[frame.index >= pd.Timestamp(start)]
        if end:
            frame = frame[frame.index <= pd.Timestamp(end)]
        return frame

    def query_adjust_factors(self, symbol, start=None, end=None):
        frame = self._factors.get(symbol)
        if frame is None:
            return pd.DataFrame(columns=["factor_hfq"])
        if end:
            frame = frame[frame.index <= pd.Timestamp(end)]
        return frame.copy()

    def get_latest_factor_hfq(self, symbol):
        frame = self._factors.get(symbol)
        return float(frame["factor_hfq"].iloc[-1]) if frame is not None else None


def test_fake_cache_duck_typing_and_raw_adjust_flag():
    fake = _FakeCache(
        bars_by_symbol={SYM: {"2026-01-05": 10.0, "2026-01-06": 10.0 / 1.05}},
        factors_by_symbol={SYM: {"2026-01-05": 1.0, "2026-01-06": 1.05}},
    )

    res = compute_nav({SYM: 100}, as_of="2026-01-06", cache=fake, cash=0.0,
                      anchors={SYM: "2026-01-05"}, provider="fake")

    assert res.nav == pytest.approx(1000.0, abs=1e-9)
    # `raw` is passed EXPLICITLY -- DuckDBCache defaults to qfq, which would
    # double-apply the adjustment on top of the factor ratio.
    assert fake.adjust_seen and set(fake.adjust_seen) == {"raw"}


# ---------------------------------------------------------------------------
# 11. input guards
# ---------------------------------------------------------------------------
def test_input_guards(cache):
    _seed(cache, SYM, {"2026-01-06": 10.0}, {"2026-01-06": 1.0})

    with pytest.raises(ValueError):
        compute_nav({SYM: 100}, as_of="2026-01-06", cache=cache, cash=1.0,
                    dividend_mode="hfq", provider=PROVIDER)
    with pytest.raises(ValueError):
        compute_nav({SYM: -5}, as_of="2026-01-06", cache=cache, cash=1.0,
                    provider=PROVIDER)
    with pytest.raises(ValueError):
        compute_nav({SYM: 100}, as_of="not-a-date", cache=cache, cash=1.0,
                    provider=PROVIDER)
    with pytest.raises(ValueError):
        compute_nav({SYM: 100}, as_of="2026-01-06", cache=cache, cash=-1.0,
                    provider=PROVIDER)

    res = compute_nav({SYM: 0, "BBB": 0}, as_of="2026-01-06", cache=cache,
                      cash=1.0, provider=PROVIDER)
    assert res.equity == 0.0
    assert res.nav == pytest.approx(1.0)
    assert sum(w.startswith("zero_shares_ignored") for w in res.warnings) == 2
