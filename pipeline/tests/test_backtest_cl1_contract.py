"""CL1 contract tests: CLI End-Date Truncation Guard & Cache Read-Only Default.

Validates docs/development_plan.md §2.34 (CL1):
1. Silent Truncation Refusal: when --end is omitted (defaults to 2024-12-31) and DuckDB cache contains
   bars newer than 2024-12-31, run_backtest.py refuses with DefaultEndDateTruncationError.
   When --end is explicitly supplied, backtest succeeds.
2. Read-Only DuckDB Cache Invariance: backtest defaults to cache_read_only=True, guaranteeing
   zero cache mutations and bit-identical cache MD5 checksum before and after the run.
3. Opt-in Cache Writes: passing --allow-cache-write enables cache writing, persisting newly
   fetched bars and recording cache_writes > 0 in data_load_telemetry.
4. Synthetic Provider Compatibility: omitting --end on synthetic provider emits a UserWarning but
   does not fail or block execution.
"""

import hashlib
import json
import os
import sys
import numpy as np
import pandas as pd
import pytest

# Ensure repo root and sub-project dirs are on sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
for _group in ("pipeline", "ml"):
    _group_dir = os.path.join(_PROJECT_ROOT, _group)
    if _group_dir not in sys.path:
        sys.path.insert(0, _group_dir)

from common.data import BaseDataProvider, register_provider
from common.duckdb_cache import DuckDBCache
from common.testing import make_ohlcv_from_closes as make_df
import backtester.run_backtest as rb
from backtester.run_backtest import (
    DEFAULT_BACKTEST_END,
    DefaultEndDateTruncationError,
    main,
)


class _CL1TestProvider(BaseDataProvider):
    """Deterministic offline provider for CL1 contract tests."""
    native_adjust_basis = "qfq"

    def fetch_ohlcv(self, symbol, start, end, interval="1d"):
        # Generate bars spanning 2024-01-01 to 2025-06-30
        dates = pd.bdate_range("2024-01-01", "2025-06-30")
        closes = np.linspace(100.0, 150.0, len(dates))
        return make_df(closes, start="2024-01-01")


register_provider("cl1_test_provider", _CL1TestProvider)


def _write_dummy_strategy(strategy_path):
    strategy_path.write_text(
        json.dumps({
            "template_name": "equal_weight",
            "name": "cl1_contract_strategy",
            "params": {"rebalance_freq_days": 21},
            "explanation": "CL1 contract test dummy strategy",
        }),
        encoding="utf-8",
    )


def test_cl1_default_end_refuses_silent_truncation_against_newer_cache(tmp_path, monkeypatch):
    """Rule 10 probe: omitting --end against a cache with 2025-06-30 bars raises DefaultEndDateTruncationError.

    When --end is explicitly provided (either 2025-06-30 or explicit 2024-12-31), it succeeds.
    """
    strategy_path = tmp_path / "strategy.json"
    _write_dummy_strategy(strategy_path)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(cache_dir / "cache.duckdb")

    # Populate cache with bars through 2025-06-30
    df = _CL1TestProvider().fetch_ohlcv("AAA", "2024-01-01", "2025-06-30")
    cache = DuckDBCache(db_path)
    cache.store_bars("cl1_test_provider", "AAA", df, adjustflag="qfq")
    del cache

    results_dir = tmp_path / "results_fail"

    # 1. Run without --end flag -> must raise DefaultEndDateTruncationError
    argv_default = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "AAA",
        "--data-provider", "cl1_test_provider",
        "--results-dir", str(results_dir),
        "--cache-dir", str(cache_dir),
        "--start", "2024-01-01",
    ]
    monkeypatch.setattr(sys, "argv", argv_default)

    with pytest.raises(DefaultEndDateTruncationError) as exc_info:
        main()

    err_msg = str(exc_info.value)
    assert DEFAULT_BACKTEST_END in err_msg
    assert "2025-06-30" in err_msg
    assert "prevent silent data truncation" in err_msg

    # 2. Run with explicit --end 2025-06-30 -> must succeed
    results_dir_ok = tmp_path / "results_explicit_new"
    argv_explicit_new = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "AAA",
        "--data-provider", "cl1_test_provider",
        "--results-dir", str(results_dir_ok),
        "--cache-dir", str(cache_dir),
        "--start", "2024-01-01",
        "--end", "2025-06-30",
    ]
    monkeypatch.setattr(sys, "argv", argv_explicit_new)
    main()
    assert (results_dir_ok / "backtest_equity.csv").exists()

    # 3. Run with explicit --end 2024-12-31 -> must also succeed (operator intentionally truncates)
    results_dir_ok2 = tmp_path / "results_explicit_legacy"
    argv_explicit_legacy = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "AAA",
        "--data-provider", "cl1_test_provider",
        "--results-dir", str(results_dir_ok2),
        "--cache-dir", str(cache_dir),
        "--start", "2024-01-01",
        "--end", "2024-12-31",
    ]
    monkeypatch.setattr(sys, "argv", argv_explicit_legacy)
    main()
    assert (results_dir_ok2 / "backtest_equity.csv").exists()

    # Rule 10 discriminating probe: explicit newer end vs explicit legacy end
    eq_new = pd.read_csv(results_dir_ok / "backtest_equity.csv", index_col=0)
    eq_legacy = pd.read_csv(results_dir_ok2 / "backtest_equity.csv", index_col=0)
    assert len(eq_new) > len(eq_legacy)
    assert eq_new.index[-1] > eq_legacy.index[-1]
    assert eq_legacy.index[-1] <= "2024-12-31"
    assert eq_new.index[-1] >= "2025-06-30"

    manifest_new = json.loads((results_dir_ok / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest_new["end_source"] == "explicit"
    assert manifest_new["data_load_telemetry"]["end_source"] == "explicit"
    assert manifest_new["data_load_telemetry"]["cache_writes"] == 0

    manifest_legacy = json.loads((results_dir_ok2 / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest_legacy["end_source"] == "explicit"
    assert manifest_legacy["data_load_telemetry"]["end_source"] == "explicit"


def test_cl1_backtest_default_is_cache_read_only_md5_invariant(tmp_path, monkeypatch):
    """Default backtester mode is cache read-only; cache MD5 is strictly invariant."""
    strategy_path = tmp_path / "strategy.json"
    _write_dummy_strategy(strategy_path)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    db_path = cache_dir / "cache.duckdb"

    df = _CL1TestProvider().fetch_ohlcv("AAA", "2024-01-01", "2024-12-31")
    cache = DuckDBCache(str(db_path))
    cache.store_bars("cl1_test_provider", "AAA", df, adjustflag="qfq")
    del cache

    initial_md5 = hashlib.md5(db_path.read_bytes()).hexdigest()

    results_dir = tmp_path / "results"
    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "AAA",
        "--data-provider", "cl1_test_provider",
        "--results-dir", str(results_dir),
        "--cache-dir", str(cache_dir),
        "--start", "2024-01-01",
        "--end", "2024-12-31",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    main()

    # Check manifest telemetry
    manifest_file = results_dir / "run_manifest.json"
    assert manifest_file.exists()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    telem = manifest.get("data_load_telemetry", {})
    assert telem.get("cache_read_only") is True
    assert telem.get("cache_writes") == 0
    assert telem.get("cache_written_rows") == 0

    # Assert MD5 invariance
    post_md5 = hashlib.md5(db_path.read_bytes()).hexdigest()
    assert initial_md5 == post_md5, f"Cache MD5 mutated in read-only mode! initial={initial_md5}, post={post_md5}"


def test_cl1_allow_cache_write_opt_in_persists_uncovered_symbol(tmp_path, monkeypatch):
    """Passing --allow-cache-write enables cache writes and persists uncovered symbols."""
    strategy_path = tmp_path / "strategy.json"
    _write_dummy_strategy(strategy_path)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(cache_dir / "cache.duckdb")

    # Cache has only AAA
    df_a = _CL1TestProvider().fetch_ohlcv("AAA", "2024-01-01", "2024-12-31")
    cache = DuckDBCache(db_path)
    cache.store_bars("cl1_test_provider", "AAA", df_a, adjustflag="qfq")
    del cache

    results_dir = tmp_path / "results"
    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "AAA", "BBB",
        "--data-provider", "cl1_test_provider",
        "--results-dir", str(results_dir),
        "--cache-dir", str(cache_dir),
        "--start", "2024-01-01",
        "--end", "2024-12-31",
        "--allow-cache-write",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    main()

    # Check manifest telemetry
    manifest = json.loads((results_dir / "run_manifest.json").read_text(encoding="utf-8"))
    telem = manifest.get("data_load_telemetry", {})
    assert telem.get("cache_read_only") is False
    assert telem.get("cache_writes") > 0
    assert telem.get("cache_written_rows") > 0

    # Verify BBB was stored in DuckDB cache
    cache_verify = DuckDBCache(db_path, read_only=True)
    max_date_b = cache_verify.get_universe_max_bar_date(["BBB"], provider="cl1_test_provider")
    assert max_date_b is not None
    assert str(max_date_b) >= "2024-12-31"


def test_cl1_default_end_warns_on_synthetic_provider_without_breaking(tmp_path, monkeypatch):
    """Synthetic provider emits UserWarning when --end is omitted, but does not crash."""
    strategy_path = tmp_path / "strategy.json"
    _write_dummy_strategy(strategy_path)
    results_dir = tmp_path / "results"

    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "SPY", "TLT",
        "--data-provider", "synthetic",
        "--results-dir", str(results_dir),
        "--start", "2020-01-01",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.warns(UserWarning, match=r"\[WARN\]\[CL1\] --end was not specified"):
        main()

    assert (results_dir / "backtest_equity.csv").exists()


def test_cl1_absent_cache_read_only_does_not_crash_or_create_file(tmp_path, monkeypatch):
    """When cache file does not exist and --allow-cache-write is False, fetches in-memory without crashing or writing."""
    strategy_path = tmp_path / "strategy.json"
    _write_dummy_strategy(strategy_path)
    empty_cache_dir = tmp_path / "empty_cache"
    empty_cache_dir.mkdir(parents=True, exist_ok=True)
    db_file = empty_cache_dir / "cache.duckdb"
    assert not db_file.exists()

    results_dir = tmp_path / "results_absent_cache"
    argv = [
        "run_backtest.py",
        "--strategy-file", str(strategy_path),
        "--universe", "AAA",
        "--data-provider", "cl1_test_provider",
        "--results-dir", str(results_dir),
        "--cache-dir", str(empty_cache_dir),
        "--start", "2024-01-01",
        "--end", "2024-12-31",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.warns(UserWarning, match=r"\[WARN\]\[CL1\] Cache file .* does not exist"):
        main()

    assert (results_dir / "backtest_equity.csv").exists()
    assert not db_file.exists(), f"cache.duckdb was unexpectedly created in read-only mode!"

    manifest = json.loads((results_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["db_used"] is False
    assert manifest["data_load_telemetry"]["cache_writes"] == 0
    assert manifest["data_load_telemetry"]["cache_written_rows"] == 0

