"""The NAV engine must read the provider key the CACHE actually stores.

FORENSIC CONTEXT (2026-10-27, real live-deployment blocker)
-----------------------------------------------------------
The two halves of one read/write pair disagreed on a string:

* WRITE side -- ``common/data.py`` keys every bar row by
  ``type(self.inner_provider).__name__``, i.e. ``FuyaoDataProvider``.
* READ side -- the live station passes ``storage.default_data_provider`` /
  ``--data-provider``, i.e. the SHORT name ``fuyao`` (``settings.local.json``).

``DuckDBCache.query_bars`` builds ``WHERE provider = ?`` from the read key, gets zero
rows, and returns an EMPTY frame *before* ``_resolve_read_basis`` runs -- so a key
mismatch produces neither rows nor an exception. ``valuation.compute_nav`` then reports
``StalePriceError: no price at all for 000999.SZ ... provider=fuyao``, which
``ExecutionService`` classifies as ``stale_price``: "行情陈旧". The operator was sent to
look at market data while the cache held 680 clean bars per symbol under
``FuyaoDataProvider``, and the deploy aborted.

WHAT IS PINNED HERE
-------------------
1. ``DuckDBCache.resolve_provider_key``: exact key wins; otherwise a UNIQUE
   case-insensitive / ``DataProvider``-suffix-insensitive match is used; zero matches and
   AMBIGUOUS multi-matches return the caller's value unchanged (with a WARNING) -- the
   resolver must never guess between two upstreams that hold different bytes.
2. The hard regression: ``compute_nav(provider="fuyao")`` over a cache holding only
   ``FuyaoDataProvider`` rows MUST price the book (no ``StalePriceError``) and MUST record
   ``provider_key_resolved: fuyao -> FuyaoDataProvider``.
3. Old behaviour is unchanged when the key is already correct (no resolution warning).
4. A genuinely EMPTY cache still raises -- no price is ever fabricated.

100% offline and hermetic: in-memory ``DuckDBCache(':memory:')`` seeded through the public
``store_bars`` / ``store_adjust_factors`` writers. Nothing under ``data/`` is opened and no
network call is made.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common.duckdb_cache import (  # noqa: E402
    DuckDBCache,
    canonicalize_provider_key,
)
from common.freshness import StalePriceError  # noqa: E402

from web.livetrading.valuation import compute_nav  # noqa: E402

#: The key the production cache actually stores (``type(FuyaoDataProvider()).__name__``).
STORED_KEY = "FuyaoDataProvider"
#: What ``settings.local.json`` / ``--data-provider`` say for the SAME upstream.
SHORT_KEY = "fuyao"

SYM = "AAA"
FIRST_BAR = "2026-01-05"
LAST_BAR = "2026-01-07"


# ---------------------------------------------------------------------------
# seeding helpers (in-memory only)
# ---------------------------------------------------------------------------
def _bars(rows: dict) -> pd.DataFrame:
    items = list(rows.items())
    idx = pd.DatetimeIndex(pd.to_datetime([d for d, _ in items]), name="date")
    close = pd.Series([float(c) for _, c in items], index=idx)
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1_000_000.0},
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
    """A throwaway in-memory cache -- never the production ``data/cache.duckdb``."""
    c = DuckDBCache(":memory:")
    yield c
    try:
        c._mem_con.close()
    except Exception:  # pragma: no cover - best-effort cleanup
        pass


def _seed(cache, provider, symbol, bars: dict, factors: dict | None = None):
    cache.store_bars(provider, symbol, _bars(bars), adjustflag="raw")
    if factors is not None:
        cache.store_adjust_factors(symbol, _factors(factors))


def _three_flat_bars():
    """Three identical 10.00 closes, factor 1.0 throughout -> a hand-checkable mark."""
    dates = {FIRST_BAR: 10.0, "2026-01-06": 10.0, LAST_BAR: 10.0}
    return dates, {d: 1.0 for d in dates}


def _warning_text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 1. canonical form -- the comparison key only, never a value to read with
# ---------------------------------------------------------------------------
def test_canonical_form_strips_the_dataprovider_suffix():
    assert canonicalize_provider_key(SHORT_KEY) == "fuyao"
    assert canonicalize_provider_key("Fuyao") == "fuyao"
    assert canonicalize_provider_key(STORED_KEY) == "fuyao"
    # Distinct upstreams must never collapse into each other.
    assert canonicalize_provider_key("MarketDBDataProvider") == "marketdb"
    assert canonicalize_provider_key("BaoStockDataProvider") == "baostock"
    assert canonicalize_provider_key("MarketDBDataProvider") != canonicalize_provider_key(
        STORED_KEY
    )


# ---------------------------------------------------------------------------
# 2. resolution: the three outcomes, and the ambiguity refusal
# ---------------------------------------------------------------------------
def test_resolve_provider_key_maps_short_name_onto_the_stored_class_name(cache, caplog):
    bars, _ = _three_flat_bars()
    _seed(cache, STORED_KEY, SYM, bars)

    with caplog.at_level("WARNING", logger="common.duckdb_cache"):
        resolved = cache.resolve_provider_key(SHORT_KEY)

    assert resolved == STORED_KEY
    text = _warning_text(caplog)
    assert "requested -> resolved" in text
    assert SHORT_KEY in text and STORED_KEY in text


def test_resolve_provider_key_returns_an_exact_key_untouched(cache, caplog):
    bars, _ = _three_flat_bars()
    _seed(cache, STORED_KEY, SYM, bars)

    with caplog.at_level("WARNING", logger="common.duckdb_cache"):
        resolved = cache.resolve_provider_key(STORED_KEY)

    assert resolved == STORED_KEY
    # Nothing was rewritten, so no reconciliation warning is emitted.
    assert "requested -> resolved" not in _warning_text(caplog)


def test_resolve_provider_key_keeps_an_unknown_name_and_warns(cache, caplog):
    bars, _ = _three_flat_bars()
    _seed(cache, STORED_KEY, SYM, bars)

    with caplog.at_level("WARNING", logger="common.duckdb_cache"):
        resolved = cache.resolve_provider_key("fuyoa")  # a TYPO, not a short name

    # A typo must NOT be laundered into a working read.
    assert resolved == "fuyoa"
    assert "matches NO stored key" in _warning_text(caplog)


def test_resolve_provider_key_refuses_to_guess_between_two_canonical_matches(cache, caplog):
    """``fuyao`` AND ``FuyaoDataProvider`` both stored -> the two hold DIFFERENT bytes."""
    bars, _ = _three_flat_bars()
    _seed(cache, "fuyao", SYM, bars)
    _seed(cache, STORED_KEY, SYM, bars)

    with caplog.at_level("WARNING", logger="common.duckdb_cache"):
        # The caller named the short key exactly: it is used, and the collision is reported.
        assert cache.resolve_provider_key(SHORT_KEY) == SHORT_KEY
        assert "share its canonical form" in _warning_text(caplog)

    caplog.clear()
    with caplog.at_level("WARNING", logger="common.duckdb_cache"):
        # No exact match, two canonical matches: keep the value, never pick one.
        assert cache.resolve_provider_key("Fuyao") == "Fuyao"
        assert "AMBIGUOUS" in _warning_text(caplog)


def test_resolve_provider_key_short_circuits_synthetic(cache, caplog):
    """Synthetic bars are generated, never cached, so there is nothing to reconcile."""
    with caplog.at_level("WARNING", logger="common.duckdb_cache"):
        assert cache.resolve_provider_key("synthetic") == "synthetic"
    assert _warning_text(caplog) == ""


# ---------------------------------------------------------------------------
# 3. THE hard regression: a short-name read prices a class-name cache
# ---------------------------------------------------------------------------
def test_compute_nav_prices_a_short_name_read_against_a_class_name_cache(cache):
    """The exact live failure: 3 bars under ``FuyaoDataProvider``, read as ``fuyao``."""
    bars, factors = _three_flat_bars()
    _seed(cache, STORED_KEY, SYM, bars, factors)

    res = compute_nav(
        {SYM: 100},
        as_of=LAST_BAR,
        cache=cache,
        cash=0.0,
        anchors={SYM: FIRST_BAR},
        provider=SHORT_KEY,  # <- what `settings.local.json` says
    )

    # 100 shares x 10.00, never a StalePriceError.
    assert res.nav == pytest.approx(1000.0)
    assert res.equity == pytest.approx(1000.0)
    assert res.stale_symbols == []
    assert res.per_symbol[SYM]["raw_close"] == pytest.approx(10.0)

    # The reconciliation is visible to the operator, and per-symbol auditable.
    assert f"provider_key_resolved: {SHORT_KEY} -> {STORED_KEY}" in res.warnings
    assert res.per_symbol[SYM]["provider_key"] == STORED_KEY


def test_compute_nav_does_not_warn_when_the_key_is_already_correct(cache):
    bars, factors = _three_flat_bars()
    _seed(cache, STORED_KEY, SYM, bars, factors)

    res = compute_nav(
        {SYM: 100},
        as_of=LAST_BAR,
        cache=cache,
        cash=0.0,
        anchors={SYM: FIRST_BAR},
        provider=STORED_KEY,
    )

    assert res.nav == pytest.approx(1000.0)
    assert not [w for w in res.warnings if w.startswith("provider_key")]
    assert res.per_symbol[SYM]["provider_key"] == STORED_KEY


def test_compute_nav_maps_a_short_name_onto_a_marketdb_cache(cache):
    """Second real key from the same cache: ``marketdb`` -> ``MarketDBDataProvider``."""
    bars, factors = _three_flat_bars()
    _seed(cache, "MarketDBDataProvider", SYM, bars, factors)
    _seed(cache, STORED_KEY, "OTHER", bars, factors)  # a decoy upstream must not confuse it

    res = compute_nav(
        {SYM: 100},
        as_of=LAST_BAR,
        cache=cache,
        cash=0.0,
        anchors={SYM: FIRST_BAR},
        provider="marketdb",
    )

    assert res.nav == pytest.approx(1000.0)
    assert "provider_key_resolved: marketdb -> MarketDBDataProvider" in res.warnings


# ---------------------------------------------------------------------------
# 4. a genuinely empty cache still refuses -- no fabricated price
# ---------------------------------------------------------------------------
def test_empty_cache_still_raises_and_never_fabricates_a_price(cache):
    """Zero rows anywhere: the cure is a Stage-1 refresh, and NO mark is invented."""
    assert cache.distinct_provider_keys() == []

    with pytest.raises(StalePriceError) as excinfo:
        compute_nav(
            {SYM: 100},
            as_of=LAST_BAR,
            cache=cache,
            cash=0.0,
            anchors={SYM: FIRST_BAR},
            provider=SHORT_KEY,
        )

    assert "no price at all" in str(excinfo.value)
    assert SYM in str(excinfo.value)
    assert cache.distinct_provider_keys() == []


def test_resolution_never_writes(cache):
    """The resolver is a read: no bars appear, no metadata is created by looking."""
    bars, _ = _three_flat_bars()
    _seed(cache, STORED_KEY, SYM, bars)
    before = (
        len(cache.distinct_provider_keys()),
        len(cache.query_bars(STORED_KEY, SYM, adjust="raw")),
    )

    cache.resolve_provider_key(SHORT_KEY)

    after = (
        len(cache.distinct_provider_keys()),
        len(cache.query_bars(STORED_KEY, SYM, adjust="raw")),
    )
    assert after == before
