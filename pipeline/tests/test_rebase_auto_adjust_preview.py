"""Offline regression tests for the dry-run preview in `scripts/rebase_auto_adjust_cache.py`.

Why this file exists: the preview used to compare `Close` only, while the authoritative
history-rewrite gate in `common/duckdb_cache.py::store_bars` compares open/high/low/close/
volume under `_HISTORY_REWRITE_RTOL` (rtol 1e-9, absolute floor 0.0). A volume-only (or
OHLC-only) basis shift therefore previewed as "no differences" and the operator learned the
truth only when the real write was refused/flagged. These tests pin the replica:

* all five columns are compared (the volume-only case is the regression);
* the tolerance object is *the gate's own constant*, not a re-typed literal;
* NULL/NaN semantics match the SQL CASE (both NULL == equal, exactly one NULL == different);
* the tolerance denominator is GREATEST(|stored|, |incoming|), like the gate.

100% offline: synthetic frames, a stubbed cache and a stubbed provider -- no duckdb file is
opened, no provider is constructed, no network.
"""

import os
import sys

import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common import duckdb_cache
from scripts import rebase_auto_adjust_cache as rebase


def _frame(close=100.0, volume=1000.0, open_=99.0, high=101.0, low=98.0, periods=3):
    dates = pd.bdate_range("2020-01-01", periods=periods)
    return pd.DataFrame(
        {
            "Open": [open_] * periods,
            "High": [high] * periods,
            "Low": [low] * periods,
            "Close": [close] * periods,
            "Volume": [volume] * periods,
        },
        index=dates,
    )


def test_volume_only_drift_is_reported():
    """The regression: Close identical, Volume materially different -> a difference."""
    stored = _frame()
    incoming = _frame(volume=2000.0)   # 100% volume drift, prices untouched
    mask = rebase.history_rewrite_diff_mask(stored, incoming)
    assert mask.all()


def test_close_only_drift_is_reported():
    stored = _frame()
    incoming = _frame(close=50.0)
    assert rebase.history_rewrite_diff_mask(stored, incoming).all()


def test_each_price_column_is_checked():
    for column, value in (("Open", 1.0), ("High", 500.0), ("Low", 1.0)):
        incoming = _frame()
        incoming[column] = value
        assert rebase.history_rewrite_diff_mask(_frame(), incoming).all(), column


def test_identical_and_ulp_level_frames_are_equal():
    """An idempotent re-derivation must preview as clean, like the gate treats it."""
    stored = _frame()
    assert not rebase.history_rewrite_diff_mask(stored, stored.copy()).any()
    ulp = _frame()
    ulp["Close"] = stored["Close"] * (1 + 1e-12)
    assert not rebase.history_rewrite_diff_mask(stored, ulp).any()


def test_tolerance_is_the_gate_constant_not_a_literal():
    """`# why:` identity, not equality: a re-typed 1e-9 would be free to drift."""
    assert rebase._HISTORY_REWRITE_RTOL is duckdb_cache._HISTORY_REWRITE_RTOL
    assert rebase._HISTORY_REWRITE_RTOL == 1e-9


def test_null_semantics_match_the_sql_case():
    stored = _frame(periods=2)
    # exactly one NULL -> material (the gate treats a vanished/added value as a rewrite)
    one_null = _frame(periods=2)
    one_null.loc[one_null.index[0], "Close"] = float("nan")
    mask = rebase.history_rewrite_diff_mask(stored, one_null)
    assert bool(mask.iloc[0]) is True
    assert bool(mask.iloc[1]) is False
    # both NaN -> equal (DuckDB's total order makes `NaN <= NaN` TRUE, and a pandas NaN
    # arrives as SQL NULL, whose both-NULL branch is "equal")
    both_null = _frame(periods=2)
    both_null["Close"] = float("nan")
    assert not rebase.history_rewrite_diff_mask(both_null, both_null.copy()).any()


def test_tolerance_denominator_is_the_larger_magnitude():
    """GREATEST(|stored|, |incoming|): a 2e-9 relative shift is material at any scale."""
    stored = _frame(close=1.0)
    incoming = _frame(close=1.0 * (1 + 2e-9))
    assert rebase.history_rewrite_diff_mask(stored, incoming).all()
    # a zero baseline must not blow up (no division; the absolute floor is 0.0)
    assert not rebase.history_rewrite_diff_mask(_frame(close=0.0), _frame(close=0.0)).any()


def _run_dry_run(monkeypatch, tmp_path, capsys, stored, fresh):
    """Drive `main(--dry-run)` with a stubbed cache + provider (no duckdb, no network)."""
    db_file = tmp_path / "fake.duckdb"
    db_file.write_bytes(b"")   # only `os.path.exists` is consulted before the stub is used

    class _StubCache:
        def __init__(self, *a, **k):
            pass

        def query_bars(self, *a, **k):
            return stored.copy()

    class _StubProvider:
        def fetch_ohlcv(self, symbol, start, end, interval="1d"):
            return fresh.copy()

    monkeypatch.setattr(rebase, "DuckDBCache", _StubCache)
    monkeypatch.setattr(rebase, "get_data_provider", lambda name: _StubProvider())
    rc = rebase.main([
        "--provider", "stub",
        "--symbol", "AAPL",
        "--start", "2020-01-01",
        "--end", "2020-01-10",
        "--reason", "Dry-run preview regression test",
        "--db", str(db_file),
        "--dry-run",
    ])
    return rc, capsys.readouterr().out


def test_dry_run_preview_reports_volume_only_drift(monkeypatch, tmp_path, capsys):
    rc, out = _run_dry_run(
        monkeypatch, tmp_path, capsys, _frame(), _frame(volume=2000.0)
    )
    assert rc == 0
    assert "Material differences detected" in out
    assert "Volume=" in out
    # Close agrees, so the price-ratio line must not be the (only) explanation printed
    assert "drift is confined to Open/High/Low/Volume" in out
    assert "Would call rebase_corporate_action" in out


def test_dry_run_preview_says_clean_when_all_five_columns_agree(monkeypatch, tmp_path, capsys):
    rc, out = _run_dry_run(monkeypatch, tmp_path, capsys, _frame(), _frame())
    assert rc == 0
    assert "No differences detected" in out
    assert "open/high/low/close/volume" in out
