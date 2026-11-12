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


def test_batch_outside_a_foreign_span_is_allowed():
    """An append past a foreign SPAN is not a conflict (the everyday incremental-sync shape).

    Without this, a symbol carrying an old BaoStock prefix would have its legitimate Fuyao
    updates refused forever, which would freeze factor updates on every legacy-mixed symbol.
    """
    cache = DuckDBCache(db_path=":memory:")
    cache.store_adjust_factors(
        "601398.SH",
        _factors([3.232896], pd.to_datetime(["2023-09-18"])),
        source=SPARSE_SOURCE,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cache.store_adjust_factors(
            "601398.SH",
            _factors([1.654876], pd.to_datetime(["2024-01-02"])),
            source=DENSE_SOURCE,
        )
    assert [w for w in caught if issubclass(w.category, CrossSourceAdjustFactorRefused)] == []
    stored = _stored(cache, "601398.SH")
    assert stored[datetime.date(2024, 1, 2)] == (pytest.approx(1.654876), DENSE_SOURCE)
    assert stored[datetime.date(2023, 9, 18)] == (pytest.approx(3.232896), SPARSE_SOURCE)


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
