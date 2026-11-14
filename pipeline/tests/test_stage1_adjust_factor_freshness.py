"""2026-11-13: Stage 1 publishes `adjust_factor_freshness` -- and gates nothing with it.

The census itself is tested in `common/tests/test_adjust_factor_freshness.py` (including the
red-check that the lagging assertion depends on the comparison). This suite covers only the
WIRING, which is where a "helpful" refactor could quietly turn an observation into a gate:

* the block lands in `stage1_health_report.json` under `adjust_factor_freshness`;
* it declares `gates_orders: false` / `enforcement: observation_only`;
* omitting it leaves every pre-2026-11-13 caller's report valid (the key is null, not absent);
* the census reads the cache FILE the refresh actually opened, and a skipped refresh / missing
  file degrades to `unknown` instead of raising.

100% offline: temporary DuckDB caches, no providers, no network, no child process.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pandas as pd

from common.duckdb_cache import DuckDBCache

PROVIDER = "FuyaoDataProvider"
SYMBOL = "600028.SH"
FACTOR_DATES = pd.to_datetime(
    ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27"]
)
GAP_SESSIONS = [
    "2026-10-28", "2026-10-29", "2026-10-30",
    "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05",
    "2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12",
]


def _load_script(module_name, relative_path):
    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load_script(
    "check_live_portfolio_health_for_factor_freshness", "scripts/check_live_portfolio_health.py"
)


def _bars_df(dates) -> pd.DataFrame:
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


def _lagging_cache(tmp_path) -> str:
    """Bars through 2026-11-12, dense factors stopping at 2026-10-27 (the measured shape)."""
    db_file = str(tmp_path / "cache.duckdb")
    cache = DuckDBCache(db_file)
    bar_dates = pd.to_datetime(
        [d.strftime("%Y-%m-%d") for d in FACTOR_DATES] + GAP_SESSIONS
    )
    cache.store_bars(PROVIDER, SYMBOL, _bars_df(bar_dates), adjustflag="raw")
    cache.store_adjust_factors(
        SYMBOL,
        pd.DataFrame({"factor_hfq": [1.897382] * len(FACTOR_DATES)}, index=FACTOR_DATES),
        source=PROVIDER,
    )
    return cache.db_path


def _minimal_report_kwargs(tmp_path):
    """The smallest `generate_reports` payload the advisory-wiring suite already pins."""
    flat_state = {
        "as_of_date": None,
        "peak_nav": None,
        "tier1_consecutive_bars": 0,
        "tier3_consecutive_bars": 0,
        "freeze_remaining_bars": 0,
        "nav_history_10d": [],
    }
    dd_info = _mod.inspect_portfolio_drawdown(
        current_nav=1_030_000.0,
        peak_nav=1_100_000.0,
        state=flat_state,
        params={},
        as_of_date="2026-11-13",
    )
    return dict(
        output_dir=str(tmp_path),
        as_of_date="2026-11-13",
        strategy_info={"strategy_name": "unit-test", "template_name": "unit-test"},
        dd_info=dd_info,
        macro_info={
            "breadth": 0.40,
            "thrust": 0.45,
            "breadth_active": True,
            "thrust_active": False,
            "target_exposure": 0.50,
            "breadth_thresh": 0.30,
            "thrust_thresh": 0.60,
            "leaders": [],
            "tracked_symbols_count": 3,
        },
        vol_info={
            "realized_vol_21d": 0.18,
            "target_vol": 0.20,
            "vol_scaling_factor": 1.0,
            "vol_elevated": False,
        },
        gate_code="GO",
        gate_headline="GO",
        actions=[],
    )


def _read_report(tmp_path):
    with open(os.path.join(str(tmp_path), _mod.STAGE1_HEALTH_REPORT_JSON), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# 1. the observer reads the FILE the refresh opened, not a re-derived guess
# ---------------------------------------------------------------------------
def test_census_uses_the_refresh_cache_file_even_when_cache_dir_is_wrong(tmp_path):
    db_file = _lagging_cache(tmp_path)
    block = _mod.inspect_stage1_adjust_factor_freshness(
        refresh_summary={"status": "completed", "cache_file": db_file},
        cache_dir=str(tmp_path / "somewhere-else"),
    )

    assert block["status"] == "lagging"
    assert block["max_lag_sessions"] == 12
    assert block["per_symbol"][SYMBOL]["factor_max_date"] == "2026-10-27"
    assert block["per_symbol"][SYMBOL]["bar_max_date"] == "2026-11-12"


def test_skipped_refresh_on_a_missing_cache_degrades_to_unknown_without_raising(tmp_path):
    block = _mod.inspect_stage1_adjust_factor_freshness(
        refresh_summary={"status": "skipped", "reason": "synthetic_or_no_cache"},
        cache_dir=str(tmp_path / "does-not-exist"),
    )
    assert block["status"] == "unknown"
    assert block["error"]
    assert block["gates_orders"] is False
    assert _mod.format_adjust_factor_freshness_warning(block) is None


def test_cache_db_file_resolution_matches_duckdbcache(tmp_path):
    # A directory resolves to its cache.duckdb; a .duckdb path is taken literally.
    assert _mod.resolve_cache_db_file(str(tmp_path)) == os.path.join(str(tmp_path), "cache.duckdb")
    db_file = str(tmp_path / "cache.duckdb")
    assert _mod.resolve_cache_db_file(db_file) == db_file


# ---------------------------------------------------------------------------
# 2. the block is PERSISTED, and it says it gates nothing
# ---------------------------------------------------------------------------
def test_stage1_report_publishes_the_lagging_census(tmp_path):
    db_file = _lagging_cache(tmp_path)
    block = _mod.inspect_stage1_adjust_factor_freshness(
        refresh_summary={"cache_file": db_file}, cache_dir=str(tmp_path)
    )

    _mod.generate_reports(adjust_factor_freshness=block, **_minimal_report_kwargs(tmp_path))
    report = _read_report(tmp_path)

    published = report["adjust_factor_freshness"]
    assert published["status"] == "lagging"
    assert published["n_lagging"] == 1
    assert published["max_lag_sessions"] == 12
    assert published["enforcement"] == "observation_only"
    assert published["gates_orders"] is False
    # The gate verdict is untouched by the census: nothing here can flip a GO.
    assert report["gate_code"] == "GO"
    assert report["directive"] == _mod.coarse_directive("GO")


def test_report_key_is_null_when_the_caller_does_not_supply_it(tmp_path):
    """Every pre-2026-11-13 caller keeps a valid report; the new key is null, not absent."""
    _mod.generate_reports(**_minimal_report_kwargs(tmp_path))
    report = _read_report(tmp_path)
    assert "adjust_factor_freshness" in report
    assert report["adjust_factor_freshness"] is None
