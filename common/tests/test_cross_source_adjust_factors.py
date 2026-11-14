"""D9: cross-source `adjust_factors` overwrite -- write gate, read guard, repair script.

All tests are 100% offline: in-memory / temporary DuckDB caches only, no providers, no network.

The defect (docs/development_plan.md D9): `adjust_factors` is keyed on `(symbol, date)` while it
also stores `source`, so `INSERT OR REPLACE` let a writer normalised on a DIFFERENT basis destroy
the other source's row. BaoStock's sparse event rows (cumulative from the IPO) overwrote Fuyao's
dense 2024-anchored daily rows for `600660.SH / 2026-09-24`: factor_hfq 81.588562 replaced
1.3894851, which rescaled that session's qfq close from 52.47 to 3080.96.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import warnings

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.duckdb_cache import (
    CrossSourceAdjustFactorRefused,
    DuckDBCache,
    MissingAdjustFactorsError,
    ImplausibleAdjustFactorStep,
    IMPLAUSIBLE_ADJUST_FACTOR_RATIO,
)

DENSE_SOURCE = "FuyaoDataProvider"
SPARSE_SOURCE = "BaoStockDataProvider"

#: `600660.SH`'s real Fuyao series is 1.2452-1.3895. The sparse BaoStock event row that landed on
#: 2026-09-24 carried 81.588562 -- 58.7x the median. The series deliberately continues past that
#: date: the collision REPLACES a dense row, and the dense source's covered interval (which the
#: repair census compares against) is still [first, last] of what survived.
_TRUE_FUYAO_2026 = [
    1.245211, 1.245211, 1.245211,
    1.260744, 1.260744, 1.260744, 1.260744,
    1.389485, 1.389485, 1.389485, 1.389485,
]
_DENSE_DATES = pd.to_datetime(
    [
        "2026-09-16", "2026-09-17", "2026-09-18",
        "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24",
        "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30",
    ]
)


def _factors(values, dates) -> pd.DataFrame:
    return pd.DataFrame({"factor_hfq": values}, index=pd.to_datetime(list(dates)))


def _dense_dates():
    return _DENSE_DATES


def _seed_dense(cache, symbol="600660.SH"):
    cache.store_adjust_factors(symbol, _factors(_TRUE_FUYAO_2026, _dense_dates()), source=DENSE_SOURCE)


def _stored(cache, symbol="600660.SH"):
    df = cache.query_adjust_factors(symbol)
    return {d.date(): (float(v), s) for d, v, s in zip(df.index, df["factor_hfq"], df["source"])}


# ---------------------------------------------------------------------------
# (1) same-source update: the pre-existing semantics must not change
# ---------------------------------------------------------------------------


def test_same_source_update_still_overwrites_in_place():
    """An update from the SAME source keeps `INSERT OR REPLACE` semantics, silently."""
    cache = DuckDBCache(db_path=":memory:")
    _seed_dense(cache)
    dates = _dense_dates()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cache.store_adjust_factors(
            "600660.SH",
            _factors([2.0] * len(dates), dates),
            source=DENSE_SOURCE,
        )
    assert [w for w in caught if issubclass(w.category, CrossSourceAdjustFactorRefused)] == []

    stored = _stored(cache)
    assert len(stored) == len(dates)
    assert {round(v, 6) for v, _ in stored.values()} == {2.0}
    assert {s for _, s in stored.values()} == {DENSE_SOURCE}


# ---------------------------------------------------------------------------
# (2) cross-source batch is refused by default: nothing written, warning emitted
# ---------------------------------------------------------------------------


def test_cross_source_batch_is_refused_without_touching_stored_rows():
    cache = DuckDBCache(db_path=":memory:")
    _seed_dense(cache)
    before = _stored(cache)

    # The exact D9 shape: a sparse BaoStock batch whose 2026-09-24 entry collides with a Fuyao
    # row, stamped on BaoStock's own (IPO-cumulative) basis.
    inj_dates = pd.to_datetime(["2026-09-24"])
    with pytest.warns(CrossSourceAdjustFactorRefused) as record:
        cache.store_adjust_factors(
            "600660.SH", _factors([81.588562], inj_dates), source=SPARSE_SOURCE
        )

    warning = record[0].message
    assert str(warning).startswith(CrossSourceAdjustFactorRefused.PREFIX)
    assert "600660.SH" in str(warning)
    assert DENSE_SOURCE in str(warning) and SPARSE_SOURCE in str(warning)
    assert "2026-09-24" in str(warning)
    assert "81.588562" in str(warning)
    assert warning.symbol == "600660.SH"
    assert warning.stored_source == DENSE_SOURCE
    assert warning.stored_rows == len(_TRUE_FUYAO_2026)
    assert warning.incoming_source == SPARSE_SOURCE
    assert warning.refused_dates == 1
    assert warning.examples and warning.examples[0]["date"] == "2026-09-24"

    # Nothing was written: same rows, same values, same sources.
    assert _stored(cache) == before
    # ...and the qfq read still uses the Fuyao anchor, i.e. no 58x step.
    assert cache.get_latest_factor_hfq("600660.SH") == pytest.approx(_TRUE_FUYAO_2026[-1])


def test_cross_source_batch_is_refused_whole_not_partially():
    """A batch mixing outside-span and inside-span dates is refused as a WHOLE."""
    cache = DuckDBCache(db_path=":memory:")
    _seed_dense(cache)
    before = _stored(cache)

    dates = pd.to_datetime(["1993-06-10", "2026-09-24"])
    with pytest.warns(CrossSourceAdjustFactorRefused):
        cache.store_adjust_factors(
            "600660.SH", _factors([1.0, 81.588562], dates), source=SPARSE_SOURCE
        )

    # Even the harmless 1993 row was not written: a partial batch would leave the caller
    # believing the write happened.
    assert _stored(cache) == before


def test_append_behind_a_foreign_span_is_allowed():
    """A backfill BEHIND the frontier is not a conflict (the pre-dense-span prefix shape).

    Nothing already stored gets replaced (no date overlap) and no incoming row can become the
    latest row for the symbol, so this shape must keep working -- otherwise a legacy-mixed
    symbol could never receive the BaoStock rows that are the only factors for its old bars.
    """
    cache = DuckDBCache(db_path=":memory:")
    cache.store_adjust_factors(
        "601398.SH",
        _factors([1.654876, 1.600000], pd.to_datetime(["2024-01-02", "2024-01-03"])),
        source=DENSE_SOURCE,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cache.store_adjust_factors(
            "601398.SH",
            _factors([3.232896], pd.to_datetime(["2023-09-18"])),
            source=SPARSE_SOURCE,
        )
    assert [w for w in caught if issubclass(w.category, CrossSourceAdjustFactorRefused)] == []
    stored = _stored(cache, "601398.SH")
    assert stored[datetime.date(2023, 9, 18)] == (pytest.approx(3.232896), SPARSE_SOURCE)
    assert stored[datetime.date(2024, 1, 2)] == (pytest.approx(1.654876), DENSE_SOURCE)
    # The frontier still belongs to the dense source, i.e. the anchor did not move.
    assert cache.get_latest_factor_hfq("601398.SH") == pytest.approx(1.6)


def test_source_switch_past_the_frontier_is_refused_and_named():
    """BEHAVIOUR CHANGE (§37.5 residual (c), closed): a switch past the frontier needs the hatch.

    A symbol whose only stored rows are a stale BaoStock prefix, then a first Fuyao batch dated
    AFTER them. Nothing would be overwritten, but the Fuyao rows would take the anchor away from
    BaoStock -- and by dates and values alone this is INDISTINGUISHABLE from the poison shape
    below (Fuyao dense series, then a BaoStock event row). The gate therefore fails closed and
    the audited `allow_cross_source=True` is what a real, verified source switch has to pass.
    """
    cache = DuckDBCache(db_path=":memory:")
    cache.store_adjust_factors(
        "601398.SH",
        _factors([3.232896], pd.to_datetime(["2023-09-18"])),
        source=SPARSE_SOURCE,
    )
    before = _stored(cache, "601398.SH")

    with pytest.warns(CrossSourceAdjustFactorRefused) as record:
        cache.store_adjust_factors(
            "601398.SH",
            _factors([1.654876], pd.to_datetime(["2024-01-02"])),
            source=DENSE_SOURCE,
        )
    warning = record[0].message
    assert warning.rule == CrossSourceAdjustFactorRefused.RULE_FRONTIER_APPEND
    assert _stored(cache, "601398.SH") == before

    # The audited hatch writes it, which is the documented path for a verified basis switch.
    cache.store_adjust_factors(
        "601398.SH",
        _factors([1.654876], pd.to_datetime(["2024-01-02"])),
        source=DENSE_SOURCE,
        allow_cross_source=True,
    )
    stored = _stored(cache, "601398.SH")
    assert stored[datetime.date(2024, 1, 2)] == (pytest.approx(1.654876), DENSE_SOURCE)
    assert stored[datetime.date(2023, 9, 18)] == (pytest.approx(3.232896), SPARSE_SOURCE)


# ---------------------------------------------------------------------------
# (2b) source STRINGS are compared canonicalised (2026-11-13 review fix)
# ---------------------------------------------------------------------------
#
# `_cross_source_conflict` used to compare raw strings (`r[0] != source`). Two spellings of the
# SAME provider coexist in this repo: `common/data.py` stamps the class name
# (`BaoStockDataProvider`) while `store_adjust_factors`'s own default is the short alias
# (`baostock`). A caller relying on that default therefore had its legitimate update refused as
# "cross-source" against rows it had itself written. These two tests pin BOTH halves of the
# contract: aliases must be equal, and genuinely different providers must stay different.


def test_source_alias_is_not_treated_as_a_foreign_source():
    """`baostock` (the API default) must match stored `BaoStockDataProvider` -- no false refusal.

    This is the regression: the default source never collided with a stored class name, so a
    caller that simply omitted `source=` got `[REFUSED] cross-source` on every overlapping update
    and its factors silently froze.
    """
    cache = DuckDBCache(db_path=":memory:")
    cache.store_adjust_factors(
        "600660.SH",
        _factors([1.245211, 1.389485], pd.to_datetime(["2026-09-24", "2026-09-25"])),
        source=SPARSE_SOURCE,  # class-name spelling, what the provider wrapper writes
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cache.store_adjust_factors(  # source= omitted on purpose: the documented default
            "600660.SH", _factors([2.5], pd.to_datetime(["2026-09-24"]))
        )
    assert [w for w in caught if issubclass(w.category, CrossSourceAdjustFactorRefused)] == []

    stored = _stored(cache)
    assert stored[datetime.date(2026, 9, 24)][0] == pytest.approx(2.5)
    assert stored[datetime.date(2026, 9, 25)][1] == SPARSE_SOURCE


@pytest.mark.parametrize(
    "incoming",
    ["fuyao", "Fuyao", "FuyaoDataProvider"],
)
def test_different_providers_still_conflict_after_canonicalisation(incoming):
    """Canonicalisation must NOT collapse two real providers: Fuyao vs BaoStock still refuses.

    Deliberately parameterised over every Fuyao spelling -- if the alias mapping ever widened
    into "any provider whose key is a prefix/suffix of another", this is the test that goes red,
    and it is the only property the whole D9 write gate rests on.
    """
    cache = DuckDBCache(db_path=":memory:")
    cache.store_adjust_factors(
        "600660.SH",
        _factors([73.117], pd.to_datetime(["2023-05-18"])),
        source=SPARSE_SOURCE,
    )
    before = _stored(cache)

    with pytest.warns(CrossSourceAdjustFactorRefused) as record:
        cache.store_adjust_factors(
            "600660.SH", _factors([1.245211], pd.to_datetime(["2023-05-18"])), source=incoming
        )

    warning = record[0].message
    assert warning.incoming_source == incoming
    assert warning.stored_source == SPARSE_SOURCE
    assert _stored(cache) == before


def test_canonicalize_provider_key_separates_the_two_incident_sources():
    """The comparison key itself: aliases fold together, the D9 pair does not."""
    from common.duckdb_cache import canonicalize_provider_key

    assert canonicalize_provider_key("baostock") == canonicalize_provider_key(
        "BaoStockDataProvider"
    )
    assert canonicalize_provider_key("fuyao") == canonicalize_provider_key("FuyaoDataProvider")
    assert canonicalize_provider_key(SPARSE_SOURCE) != canonicalize_provider_key(DENSE_SOURCE)
    # Unknown strings pass through (case-folded only) rather than being mutated into a wrong key.
    assert canonicalize_provider_key("MarketDBDataProvider") == canonicalize_provider_key(
        "marketdb"
    )
    assert canonicalize_provider_key("some_new_vendor") == "some_new_vendor"


# ---------------------------------------------------------------------------
# (3) allow_cross_source=True is the audited escape hatch
# ---------------------------------------------------------------------------


def test_allow_cross_source_true_writes_the_overwrite():
    cache = DuckDBCache(db_path=":memory:")
    _seed_dense(cache)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cache.store_adjust_factors(
            "600660.SH",
            _factors([81.588562], pd.to_datetime(["2026-09-24"])),
            source=SPARSE_SOURCE,
            allow_cross_source=True,
        )
    assert [w for w in caught if issubclass(w.category, CrossSourceAdjustFactorRefused)] == []

    stored = _stored(cache)
    assert stored[datetime.date(2026, 9, 24)] == (pytest.approx(81.588562), SPARSE_SOURCE)


# ---------------------------------------------------------------------------
# (4) a symbol with no stored rows accepts any source
# ---------------------------------------------------------------------------


def test_empty_symbol_accepts_any_source():
    cache = DuckDBCache(db_path=":memory:")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cache.store_adjust_factors(
            "600519.SH", _factors([1.0, 1.05], pd.to_datetime(["2024-01-02", "2024-06-03"]))
        )
        cache.store_adjust_factors(
            "000001.SZ",
            _factors([1.0], pd.to_datetime(["2024-01-02"])),
            source="MarketDBDataProvider",
        )
    assert [w for w in caught if issubclass(w.category, CrossSourceAdjustFactorRefused)] == []
    assert len(_stored(cache, "600519.SH")) == 2
    assert len(_stored(cache, "000001.SZ")) == 1


# ---------------------------------------------------------------------------
# (5)/(6) read-side plausibility band
# ---------------------------------------------------------------------------


def _bars(dates) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [50.0] * len(dates),
            "High": [53.0] * len(dates),
            "Low": [49.0] * len(dates),
            "Close": [52.0] * len(dates),
            "Volume": [1_000_000.0] * len(dates),
        },
        index=dates,
    )


def test_read_guard_raises_on_the_600660_outlier():
    """The poisoned 81.588562 inside a 1.2452-1.3895 series must fail closed on read."""
    cache = DuckDBCache(db_path=":memory:")
    dates = _dense_dates()
    _seed_dense(cache)
    # Write the pollution with the escape hatch: this is exactly the on-disk state the repair
    # script exists for, so the guard is what protects the read path until it has run.
    cache.store_adjust_factors(
        "600660.SH",
        _factors([81.588562], pd.to_datetime(["2026-09-24"])),
        source=SPARSE_SOURCE,
        allow_cross_source=True,
    )
    cache.store_bars(DENSE_SOURCE, "600660.SH", _bars(dates), adjustflag="raw")

    with pytest.raises(ImplausibleAdjustFactorStep) as excinfo:
        cache.query_bars(DENSE_SOURCE, "600660.SH", adjust="qfq")

    err = excinfo.value
    assert err.symbol == "600660.SH"
    assert err.date == "2026-09-24"
    assert err.value == pytest.approx(81.588562)
    assert err.median < 1.6 and err.median > 1.2
    assert err.value / err.median > IMPLAUSIBLE_ADJUST_FACTOR_RATIO
    message = str(err)
    assert "600660.SH" in message and "2026-09-24" in message
    assert "repair_cross_source_adjust_factors.py" in message


def test_read_guard_accepts_a_real_dividend_step():
    """A genuine +1.89% dividend step (and a 10-for-3 bonus) must NOT trip the band."""
    cache = DuckDBCache(db_path=":memory:")
    dates = _dense_dates()
    # 1.245211 -> 1.268745 is the real +1.89% step; 1.250000 -> 1.625000 is a 10-for-3 bonus.
    factors = [1.245211] * 3 + [1.268745] * 3 + [1.250000] * 3 + [1.625000] * 2
    assert len(factors) == len(dates)
    cache.store_adjust_factors(
        "600660.SH", _factors(factors, dates), source=DENSE_SOURCE
    )
    cache.store_bars(DENSE_SOURCE, "600660.SH", _bars(dates), adjustflag="raw")

    qfq = cache.query_bars(DENSE_SOURCE, "600660.SH", adjust="qfq")
    hfq = cache.query_bars(DENSE_SOURCE, "600660.SH", adjust="hfq")
    # qfq is anchored on the latest factor, so the last bar is unscaled.
    assert qfq["Close"].iloc[-1] == pytest.approx(52.0)
    # hfq applies the step itself, which is the point: a real action scales prices.
    assert hfq["Close"].iloc[-1] == pytest.approx(52.0 * 1.625)
    np.testing.assert_allclose(
        qfq["Close"].to_numpy(), 52.0 * np.array(factors) / factors[-1], rtol=1e-12
    )


# ---------------------------------------------------------------------------
# read-guard SCOPE (D9 follow-up): the band judges only the factors a read APPLIES
# ---------------------------------------------------------------------------

#: Daily bars for the "modern" window: starts at the dense series' first factor row and is long
#: enough to cross several dense steps (and, in the poisoned case, the 2026-09-24 row). A short
#: window would hit only 1-2 distinct factors, which is not a meaningful median sample.
_MODERN_BARS = pd.bdate_range("2024-01-02", "2026-09-30")
#: Dense (Fuyao, 2024-anchored) factor rows: three steps, none of them near the prefix.
_DENSE_EVENTS = pd.to_datetime(["2024-01-02", "2024-07-16", "2025-06-18", "2026-09-23"])
_DENSE_EVENT_FACTORS = [1.245211, 1.317042, 1.389485, 1.389485]
#: The foreign BaoStock prefix and the row that poisoned 2026-09-24 (the D9 incident).
_PREFIX_EVENTS = pd.to_datetime(["1993-06-10", "2023-05-18"])
_PREFIX_FACTORS = [1.0, 73.117]


def _dirty_symbol(cache, with_poison: bool = True):
    """600660-style state: dense 2024+ rows, a 1993-2023 foreign prefix, optional poison row."""
    cache.store_adjust_factors(
        "600660.SH", _factors(_DENSE_EVENT_FACTORS, _DENSE_EVENTS), source=DENSE_SOURCE
    )
    cache.store_adjust_factors(
        "600660.SH", _factors(_PREFIX_FACTORS, _PREFIX_EVENTS), source=SPARSE_SOURCE
    )
    if with_poison:
        cache.store_adjust_factors(
            "600660.SH",
            _factors([81.588562], pd.to_datetime(["2026-09-24"])),
            source=SPARSE_SOURCE,
            allow_cross_source=True,
        )


def test_read_guard_ignores_a_prefix_the_window_never_hits():
    """DISCRIMINATING (D9 follow-up): a 2024+ window must NOT fail because of a 1993-2023 prefix.

    The band used to be computed over `event_hfq` as returned by `query_adjust_factors`, which
    has no `start` filter -- so `600660.SH`'s 34 foreign-basis prefix rows (max 73.117, ~55x)
    made EVERY modern read raise, including the live breadth path. A wrong error is not an
    improvement over a wrong price; the band must judge the factors it actually applies.
    """
    cache = DuckDBCache(db_path=":memory:")
    _dirty_symbol(cache, with_poison=False)
    cache.store_bars(DENSE_SOURCE, "600660.SH", _bars(_MODERN_BARS), adjustflag="raw")

    qfq = cache.query_bars(DENSE_SOURCE, "600660.SH", "2024-01-02", "2026-09-23", adjust="qfq")
    assert len(qfq) >= 700  # the whole stored dense history, unaffected by the foreign prefix
    # Every applied multiplier comes from the dense series (1.245211 / 1.389485 = 0.8962 ...),
    # so no price may carry the 73.117 prefix basis (which would be a ~52x close).
    assert qfq["Close"].max() < 100
    np.testing.assert_allclose(
        qfq["Close"].iloc[0], 52.0 * 1.245211 / 1.389485, rtol=1e-12
    )


def test_read_guard_still_raises_when_the_window_hits_the_poisoned_row():
    """The same prefix is harmless, but a hit on 2026-09-24 must still fail closed."""
    cache = DuckDBCache(db_path=":memory:")
    _dirty_symbol(cache, with_poison=True)
    cache.store_bars(DENSE_SOURCE, "600660.SH", _bars(_MODERN_BARS), adjustflag="raw")

    with pytest.raises(ImplausibleAdjustFactorStep) as excinfo:
        cache.query_bars(DENSE_SOURCE, "600660.SH", "2024-01-02", "2026-09-30", adjust="qfq")

    err = excinfo.value
    assert err.date == "2026-09-24"
    assert err.value == pytest.approx(81.588562)
    assert err.source == SPARSE_SOURCE
    assert err.value / err.median > IMPLAUSIBLE_ADJUST_FACTOR_RATIO
    # The message names the bar range that hit it, so an operator can see which read is unsafe.
    assert err.bar_window == "2024-01-02..2026-09-30"
    assert "hit bars: 2024-01-02..2026-09-30" in str(err)


def test_read_guard_raises_when_the_window_lies_in_the_foreign_prefix():
    """A pre-2024 window DOES apply the prefix, so it must fail closed (no silent 52x scaling)."""
    cache = DuckDBCache(db_path=":memory:")
    _dirty_symbol(cache, with_poison=False)
    dates = pd.bdate_range("2023-01-03", "2023-12-29")
    cache.store_bars("MarketDBDataProvider", "600660.SH", _bars(dates), adjustflag="raw")

    with pytest.raises(ImplausibleAdjustFactorStep) as excinfo:
        cache.query_bars(
            "MarketDBDataProvider", "600660.SH", "2023-01-03", "2023-12-29", adjust="qfq"
        )

    err = excinfo.value
    assert err.date == "2023-05-18"
    assert err.value == pytest.approx(73.117)
    assert err.source == SPARSE_SOURCE
    assert err.bar_window == "2023-01-03..2023-12-29"


def test_read_guard_skips_the_judgement_when_no_factor_is_applied(monkeypatch):
    """Bars before the first factor row apply a neutral 1.0 -- nothing to band against.

    Through `query_bars` this state is unreachable: `query_adjust_factors(symbol, end=...)`
    truncates at the last bar's date, so a window lying entirely before the first factor row
    yields an empty frame and the pre-existing `MissingAdjustFactorsError` fires first. The
    guard's empty-sample branch is therefore defensive; it is pinned here by forcing that frame
    directly, so a refactor can never start banding a neutral 1.0.
    """
    cache = DuckDBCache(db_path=":memory:")
    cache.store_adjust_factors(
        "600660.SZ",
        _factors([1.0, 73.117], _PREFIX_EVENTS),
        source=SPARSE_SOURCE,
    )
    dates = pd.bdate_range("1992-01-02", periods=5)  # before 1993-06-10
    cache.store_bars(DENSE_SOURCE, "600660.SZ", _bars(dates), adjustflag="raw")

    with pytest.raises(MissingAdjustFactorsError):
        cache.query_bars(DENSE_SOURCE, "600660.SZ", adjust="hfq")

    fac = _factors([73.117], pd.to_datetime(["1992-12-31"]))
    fac["source"] = SPARSE_SOURCE
    fac["last_sync_time"] = None
    monkeypatch.setattr(cache, "query_adjust_factors", lambda *a, **k: fac)
    out = cache._apply_factors_to_df(_bars(dates), "600660.SZ", adjust_mode="hfq")
    np.testing.assert_allclose(out["Close"].to_numpy(), 52.0, rtol=1e-12)


# ---------------------------------------------------------------------------
# (6b) band BOUNDARIES: inclusive on both ends, and the known false positive
# ---------------------------------------------------------------------------
#
# The 4.0x / 0.25x decision was previously only reachable via 62x and 1.3x samples, so nothing
# pinned whether the band is `<` or `<=`. It is INCLUSIVE: `ratio > 4.0` / `ratio < 0.25` are the
# failure predicates, i.e. a factor sitting exactly on the boundary is accepted. These tests are
# the only place that decides it, so a future "tighten the band" edit has to change them openly.

#: Five business days: one factor row per day, so every value below is a real as-of hit.
_BAND_DATES = pd.bdate_range("2024-01-02", periods=5)


def _band_factors(extreme: float):
    """Five distinct factors whose MEDIAN is exactly 1.0, with `extreme` among them.

    The median has to be exact, otherwise "exactly 4.0x the median" is not expressible. With an
    odd count of DISTINCT values the median is one of them, so the list is built around 1.0:
    the two shapes below are the ones that keep 1.0 in the middle slot for both directions.
    """
    if extreme > 1.0:
        return [0.5, 0.9, 1.0, extreme, 1.1]
    return [extreme, 0.5, 1.0, 1.1, 1.5]


def _band_cache(extreme: float):
    cache = DuckDBCache(db_path=":memory:")
    factors = _band_factors(extreme)
    cache.store_adjust_factors(
        "600660.SH", _factors(factors, _BAND_DATES), source=DENSE_SOURCE
    )
    cache.store_bars(DENSE_SOURCE, "600660.SH", _bars(_BAND_DATES), adjustflag="raw")
    return cache, factors


@pytest.mark.parametrize("extreme", [4.0, 0.25], ids=["4.0x", "0.25x"])
def test_band_boundary_values_are_accepted(extreme):
    """INCLUSIVE: a factor at exactly 4.0x / 0.25x the applied median must NOT raise."""
    cache, factors = _band_cache(extreme)

    qfq = cache.query_bars(DENSE_SOURCE, "600660.SH", adjust="qfq")

    # ...and it really was APPLIED to a price (a boundary test that skipped the extreme would
    # pass for the wrong reason).
    np.testing.assert_allclose(
        qfq["Close"].to_numpy(), 52.0 * np.array(factors) / factors[-1], rtol=1e-12
    )


@pytest.mark.parametrize("extreme", [4.01, 0.24], ids=["4.01x", "0.24x"])
def test_band_boundary_values_just_outside_are_rejected(extreme):
    """EXCLUSIVE: 4.01x / 0.24x is outside and must fail closed with the offending value named."""
    cache, factors = _band_cache(extreme)

    with pytest.raises(ImplausibleAdjustFactorStep) as excinfo:
        cache.query_bars(DENSE_SOURCE, "600660.SH", adjust="qfq")

    err = excinfo.value
    assert err.value == pytest.approx(extreme)
    assert err.median == pytest.approx(1.0)
    assert err.value / err.median == pytest.approx(extreme)


#: A real 10-for-1 split: a single monotone ~10.5x step, i.e. exactly the shape a backward factor
#: is SUPPOSED to contain. The band is calibrated to THIS cache (largest observed single step
#: ~1.3x), so such a name is rejected.
_SPLIT_FACTORS = [1.0, 1.02, 1.05, 10.5, 10.71, 10.92]


def test_known_false_positive_a_real_10x_split_is_rejected():
    """CHARACTERISATION TEST -- this is the KNOWN FALSE POSITIVE, not supported behaviour.

    A genuine 10-for-1 split produces a monotone ~10.5x step, which the 4x band cannot tell apart
    from a foreign normalisation basis (both are "orders of magnitude off the median"). The read
    fails closed, which is the deliberate choice -- a wrong error beats a wrong price -- and the
    remedy is an operator decision, not a silently widened band.

    DO NOT "fix" this by widening `IMPLAUSIBLE_ADJUST_FACTOR_RATIO` in passing: widening the band
    is a deliberate future change with its own evidence (it would also stop catching a
    moderately-off foreign basis), and this test must then be rewritten ON PURPOSE. See
    `docs/version.md` §37 residual (d).
    """
    cache = DuckDBCache(db_path=":memory:")
    dates = pd.bdate_range("2024-01-02", periods=len(_SPLIT_FACTORS))
    cache.store_adjust_factors(
        "600660.SH", _factors(_SPLIT_FACTORS, dates), source=DENSE_SOURCE
    )
    cache.store_bars(DENSE_SOURCE, "600660.SH", _bars(dates), adjustflag="raw")

    with pytest.raises(ImplausibleAdjustFactorStep) as excinfo:
        cache.query_bars(DENSE_SOURCE, "600660.SH", adjust="qfq")

    err = excinfo.value
    assert err.median == pytest.approx((1.05 + 10.5) / 2)  # median of the 6 distinct values
    assert err.value == pytest.approx(1.0)  # the small side is the offender here
    assert err.value / err.median < 0.25


# ---------------------------------------------------------------------------
# (6c) FRONTIER-ANCHOR residual (c) -- REFUSED at the write gate since 2026-11-13
# ---------------------------------------------------------------------------
#
# See docs/version.md §37.5 residual (c), closed by this section. The overlap rule alone let a
# batch that continues a series OUTSIDE a foreign span through (deliberately: otherwise every
# legacy-mixed symbol's incremental sync would be frozen forever). Such a batch overwrites
# nothing, but because `get_latest_factor_hfq` picks the latest row by date ACROSS sources, the
# appended foreign-basis row becomes the qfq divisor, i.e. it silently rescales the whole qfq
# series by its own basis -- and if that ratio lands inside the read-side [0.25, 4.0]x band,
# nothing else fires at all.
#
# The second rule refuses exactly that: a batch reaching PAST the symbol's latest stored row is
# refused unless its own source owns that row. The read side is deliberately UNCHANGED, so the
# last test here forces a row in with the audited escape hatch and shows the old consequence.

#: Dense (Fuyao-basis) rows ending at D, then the foreign BaoStock row at the next session.
_FRONTIER_DENSE_DATES = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
_FRONTIER_FOREIGN_DATE = pd.to_datetime(["2024-01-05"])

#: The production shape of the 20 live symbols: a dense Fuyao series from 2024 and a BaoStock
#: prefix ending in 2023. The Fuyao rows are written FIRST here because that is the state the
#: cache is already in -- re-creating "prefix first, Fuyao second" would itself be the source
#: switch that the frontier rule (correctly) refuses.
_MIXED_PREFIX_DATES = pd.to_datetime(["1993-06-10", "2023-05-18"])
_MIXED_PREFIX_FACTORS = [1.0, 73.117]
_MIXED_DENSE_DATES = pd.bdate_range("2024-01-02", "2026-11-11")
_MIXED_FUYAO_FACTOR = 1.389485


def _legacy_mixed_symbol(cache, symbol="600660.SH"):
    """600660-style state: dense Fuyao 2024-01-02..2026-11-11 + BaoStock prefix 1993..2023."""
    cache.store_adjust_factors(
        symbol,
        _factors([_MIXED_FUYAO_FACTOR] * len(_MIXED_DENSE_DATES), _MIXED_DENSE_DATES),
        source=DENSE_SOURCE,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cache.store_adjust_factors(
            symbol, _factors(_MIXED_PREFIX_FACTORS, _MIXED_PREFIX_DATES), source=SPARSE_SOURCE
        )
    # The fixture itself must be accepted (a backfill behind the frontier), or every test below
    # would be measuring a different state than the one production has.
    assert [w for w in caught if issubclass(w.category, CrossSourceAdjustFactorRefused)] == []


def test_frontier_poison_is_refused_and_nothing_is_written():
    """The exact live hazard: a BaoStock event row past Fuyao's dense range must NOT land.

    Today's sync window (2026-11-03..11-12) lies outside Fuyao's covered interval, so the
    overlap rule accepts it -- and the row (~58x the Fuyao scale) would then be the LATEST row,
    i.e. the qfq anchor, which hard-fails every 2024+ read (or, if inside the band, silently
    poisons prices). The frontier rule refuses it instead.
    """
    cache = DuckDBCache(db_path=":memory:")
    _legacy_mixed_symbol(cache)
    before = _stored(cache)
    before_latest = cache.get_latest_factor_hfq("600660.SH")
    assert before_latest == pytest.approx(_MIXED_FUYAO_FACTOR)

    with pytest.warns(CrossSourceAdjustFactorRefused) as record:
        cache.store_adjust_factors(
            "600660.SH",
            _factors([81.588562], pd.to_datetime(["2026-11-12"])),
            source=SPARSE_SOURCE,
        )

    warning = record[0].message
    assert warning.rule == CrossSourceAdjustFactorRefused.RULE_FRONTIER_APPEND
    assert warning.symbol == "600660.SH"
    assert warning.stored_source == DENSE_SOURCE
    assert warning.stored_rows == len(_MIXED_DENSE_DATES)
    assert warning.incoming_source == SPARSE_SOURCE
    assert warning.refused_dates == 1
    assert warning.batch_dates == 1
    assert warning.examples[0]["date"] == "2026-11-12"
    assert warning.examples[0]["collision"] is False

    # Nothing was written -- not even a single row -- and the anchor did not move.
    assert _stored(cache) == before
    assert cache.get_latest_factor_hfq("600660.SH") == pytest.approx(before_latest)
    assert datetime.date(2026, 11, 12) not in _stored(cache)


def test_frontier_refusal_message_names_the_rule_the_frontier_and_both_sources():
    """The operator-facing contract: the text alone must say what was refused and why."""
    cache = DuckDBCache(db_path=":memory:")
    _legacy_mixed_symbol(cache)

    with pytest.warns(CrossSourceAdjustFactorRefused) as record:
        cache.store_adjust_factors(
            "600660.SH",
            _factors([81.588562], pd.to_datetime(["2026-11-12"])),
            source=SPARSE_SOURCE,
        )
    text = str(record[0].message)
    assert text.startswith(CrossSourceAdjustFactorRefused.PREFIX)
    assert "FRONTIER rule" in text
    assert "2026-11-11" in text  # the frontier row's date
    assert "2026-11-12" in text  # the incoming date that crossed it
    assert DENSE_SOURCE in text and SPARSE_SOURCE in text
    assert "81.588562" in text
    assert "get_latest_factor_hfq" in text
    assert "Nothing was written" in text
    assert "allow_cross_source=True" in text  # the documented escape hatch for a real switch


def test_legitimate_fuyao_frontier_append_on_a_legacy_mixed_symbol_is_allowed():
    """REGRESSION GUARD: the everyday incremental sync must keep working.

    Fuyao already owns the latest row for the symbol, so a Fuyao append one session later is a
    continuation of the anchor's own series, not an anchor takeover. If this ever refuses, every
    legacy-mixed symbol's factor updates are frozen (the very outcome the overlap rule's
    outside-interval exemption existed to prevent).
    """
    cache = DuckDBCache(db_path=":memory:")
    _legacy_mixed_symbol(cache)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cache.store_adjust_factors(
            "600660.SH",
            _factors([1.389485], pd.to_datetime(["2026-11-12"])),
            source=DENSE_SOURCE,
        )
    assert [w for w in caught if issubclass(w.category, CrossSourceAdjustFactorRefused)] == []

    stored = _stored(cache)
    assert stored[datetime.date(2026, 11, 12)] == (pytest.approx(1.389485), DENSE_SOURCE)
    assert stored[datetime.date(1993, 6, 10)][1] == SPARSE_SOURCE  # prefix untouched
    assert cache.get_latest_factor_hfq("600660.SH") == pytest.approx(1.389485)


def test_backfill_behind_the_frontier_on_a_legacy_mixed_symbol_is_allowed():
    """A BaoStock batch dated BEHIND the Fuyao frontier is a prefix backfill, not a takeover."""
    cache = DuckDBCache(db_path=":memory:")
    _legacy_mixed_symbol(cache)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cache.store_adjust_factors(
            "600660.SH",
            _factors([74.5], pd.to_datetime(["2023-12-29"])),
            source=SPARSE_SOURCE,
        )
    assert [w for w in caught if issubclass(w.category, CrossSourceAdjustFactorRefused)] == []

    stored = _stored(cache)
    assert stored[datetime.date(2023, 12, 29)] == (pytest.approx(74.5), SPARSE_SOURCE)
    assert cache.get_latest_factor_hfq("600660.SH") == pytest.approx(_MIXED_FUYAO_FACTOR)


def test_frontier_row_forced_in_with_the_hatch_still_becomes_the_qfq_anchor():
    """The READ side is unchanged: the write gate is the only defence, so the hatch must be safe.

    Same shape as the old characterisation test, but the append now has to be forced with
    `allow_cross_source=True`. It still becomes the qfq divisor, and the 1.26-vs-1.05 ratio is
    still inside the read-side band, so the qfq series still shows a +20% jump that raw prices do
    not have. Pinned so that a future anchor-aware read guard is a conscious change, and so that
    nobody "frees up" the escape hatch believing the read side would catch it.
    """
    cache = DuckDBCache(db_path=":memory:")
    cache.store_adjust_factors(
        "600660.SH",
        _factors([1.0, 1.05, 1.05], _FRONTIER_DENSE_DATES),
        source=DENSE_SOURCE,
    )

    cache.store_adjust_factors(
        "600660.SH",
        _factors([1.26], _FRONTIER_FOREIGN_DATE),
        source=SPARSE_SOURCE,
        allow_cross_source=True,
    )

    # ...and it is now the anchor the qfq derivation divides by.
    assert cache.get_latest_factor_hfq("600660.SH") == pytest.approx(1.26)

    dates = _FRONTIER_DENSE_DATES.append(_FRONTIER_FOREIGN_DATE)
    cache.store_bars(DENSE_SOURCE, "600660.SH", _bars(dates), adjustflag="raw")
    qfq = cache.query_bars(DENSE_SOURCE, "600660.SH", adjust="qfq")  # accepted: 1.26 is in band

    # Raw closes are flat at 52.0; the qfq series is NOT, because the divisor changed basis.
    np.testing.assert_allclose(
        qfq["Close"].to_numpy(), 52.0 * np.array([1.0, 1.05, 1.05, 1.26]) / 1.26, rtol=1e-12
    )
    assert qfq["Close"].iloc[-1] / qfq["Close"].iloc[-2] == pytest.approx(1.2)


# ---------------------------------------------------------------------------
# repair script: dry-run is read-only, --apply deletes with a JSON backup
# ---------------------------------------------------------------------------


def _repair_db(tmp_path):
    """A cache in the exact D9 state: dense Fuyao rows + a foreign BaoStock row on top."""
    db_path = tmp_path / "cache.duckdb"
    cache = DuckDBCache(db_path=str(db_path))
    cache.store_adjust_factors(
        "600660.SH", _factors(_TRUE_FUYAO_2026, _DENSE_DATES), source=DENSE_SOURCE
    )
    # A pre-dense-span foreign prefix: legitimate under the census rules, kept by the repair
    # (it is the only factor row for those bars), so it needs no escape hatch. Its second value
    # is the real 600660 basis (73.117 by 2023), i.e. far outside the dense series' band.
    cache.store_adjust_factors(
        "600660.SH",
        _factors([1.0, 73.117], pd.to_datetime(["1993-06-10", "2023-05-18"])),
        source=SPARSE_SOURCE,
    )
    # The poisoning row: same (symbol, date) as a Fuyao row, on BaoStock's own basis.
    cache.store_adjust_factors(
        "600660.SH",
        _factors([81.588562], pd.to_datetime(["2026-09-24"])),
        source=SPARSE_SOURCE,
        allow_cross_source=True,
    )
    return db_path, cache


def test_repair_dry_run_lists_rows_and_writes_nothing(tmp_path, capsys):
    from scripts import repair_cross_source_adjust_factors as repair

    db_path, cache = _repair_db(tmp_path)
    before_md5 = _md5(db_path)

    assert repair.run(["--db", str(db_path), "--backup-dir", str(tmp_path / "bk")]) == 1
    out = capsys.readouterr().out
    assert "600660.SH" in out
    assert "81.588562" in out
    assert "2026-09-24" in out
    assert "READ-ONLY" in out
    assert "KEEP 2 outside-row(s)" in out  # the 1993 + 2023 prefix survives
    # The kept prefix is on the foreign basis, so the report must say the read guard will still
    # fail closed for this symbol until that history is re-pulled.
    assert "[WARN]" in out and "ImplausibleAdjustFactorStep" in out
    assert _md5(db_path) == before_md5
    assert not (tmp_path / "bk").exists()  # no directory is created in dry-run

    stored = _stored(cache)
    assert stored[datetime.date(2026, 9, 24)] == (pytest.approx(81.588562), SPARSE_SOURCE)


def test_repair_apply_removes_only_inside_span_rows_and_backs_them_up(tmp_path, capsys):
    from scripts import repair_cross_source_adjust_factors as repair

    db_path, cache = _repair_db(tmp_path)
    backup_root = tmp_path / "bk"
    rc = repair.run(
        [
            "--db",
            str(db_path),
            "--apply",
            "--stamp",
            "20261111-000000",
            "--backup-dir",
            str(backup_root),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0, out

    out_dir = backup_root / "20261111-000000"
    removed = json.loads((out_dir / "adjust_factors_removed.json").read_text())
    manifest = json.loads((out_dir / "repair_manifest.json").read_text())
    assert manifest["script_version"] == repair.SCRIPT_VERSION
    assert manifest["removed_rows"] == len(removed["rows"]) == 1
    assert manifest["removed_json_md5"] and len(manifest["removed_json_md5"]) == 32
    assert os.path.isfile(manifest["db_copy"])
    assert manifest["db_md5_before"] != manifest["db_md5_after"]
    assert {r["source"] for r in removed["rows"]} == {SPARSE_SOURCE}
    assert {r["date"] for r in removed["rows"]} == {"2026-09-24"}
    # The backup keeps the removed value verbatim, which is the only surviving copy of the
    # destroyed Fuyao row's neighbours.
    assert removed["rows"][0]["factor_hfq"] == pytest.approx(81.588562)

    stored = _stored(cache)
    # The poisoned date is gone (its dense row had already been REPLACED, which is the damage),
    # so the as-of read now falls back to the last dense row instead of a 58x step.
    assert datetime.date(2026, 9, 24) not in stored
    assert datetime.date(1993, 6, 10) in stored  # prefix kept
    assert cache.get_latest_factor_hfq("600660.SH") == pytest.approx(_TRUE_FUYAO_2026[-1])
    # Post-repair the census is clean.
    assert repair.run(["--db", str(db_path)]) == 0


def test_repair_apply_refuses_the_production_cache(tmp_path):
    from scripts import repair_cross_source_adjust_factors as repair
    import scripts.repair_qfq_alignment as qfq_repair

    db_path, _ = _repair_db(tmp_path)
    # Simulate the production path: the guard is a pure path test, so patching the predicate is
    # enough and no real cache is touched.
    original = qfq_repair.is_production_cache
    qfq_repair.is_production_cache = lambda path: True
    try:
        rc = repair.run(["--db", str(db_path), "--apply", "--backup-dir", str(tmp_path / "bk")])
    finally:
        qfq_repair.is_production_cache = original
    assert rc == 1
    # Nothing was written and no backup directory was created.
    assert not (tmp_path / "bk").exists()


def _md5(path) -> str:
    import hashlib

    digest = hashlib.md5()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()
