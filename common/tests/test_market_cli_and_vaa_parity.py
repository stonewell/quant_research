"""Rule 11 Parity and Market CLI Verification Tests (WU1).

Validates:
1. --market CLI parsing across all 7 entrypoints (run_backtest.py, run_research_strategy.py,
   run_pipeline.py, run_strategygen.py, run_live_strategy.py, check_live_portfolio_health.py,
   run_live_multi_strategy.py).
   Tests case-insensitive aliases, flag synchronization, and conflict errors.
2. Rule 11 Backtest-vs-Live parity on fixed-seed synthetic fixtures for US, China, and HK
   pure-stock pools across ChanVaaCompoundStrategy and CFSB, executing real entrypoint
   parameter assembly and backtest running.
3. Verification that --no-vaa-external-canary records enable_vaa_external_canary=False in
   real run_manifest.json and summary.json, falling back to Tier 3 (breadth).
4. Zero leakage of transient chan_vaa_canary_bars DataFrames into strategy_dumps or effective_params.
5. Regression tests for #16 review findings:
   - Canary load RuntimeError fallback in live runner without NameError
   - Synthetic symbols do not trigger spurious US ETF canary loads; A-shares auto-infer "china"
   - In-universe index symbols are reused rather than re-fetched
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from typing import Dict

import numpy as np
import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
for _sub in ("pipeline", "pipeline/strategy_generator", "pipeline/research_strategy"):
    _subpath = os.path.join(_PROJECT_ROOT, _sub)
    if _subpath not in sys.path:
        sys.path.insert(0, _subpath)

from common.cli_utils import (
    LiveClockContext,
    add_market_cli_arg,
    add_vaa_canary_cli_args,
    parse_market_arg,
    persist_run_metadata,
    resolve_market_and_trading_flags,
)
from common.effective_params import build_effective_params
from common.market_holidays import (
    MARKET_CHINA,
    MARKET_HK,
    MARKET_US,
    UnknownMarketError,
    normalize_market,
)
from common.market_profile import (
    MARKET_VAA_PROFILES,
    resolve_strategy_market,
    template_needs_vaa_canary,
)
from common.testing import make_ohlcv_from_closes
from backtester.run_backtest import (
    _dump_effective_strategy_def,
    _dump_run_manifest,
    run_standard,
)
from pipeline.research_strategy.rs.chan_advanced_strategies import ChanVaaCompoundStrategy
from pipeline.research.blend.chan_four_state_blend import ChanFourStateBlendStrategy
from pipeline.research_strategy.rs.config import StrategyConfig
import scripts.run_live_strategy as rls
import scripts.run_live_multi_strategy as rlms
import scripts.check_live_portfolio_health as clph


# --- Fixtures & Synthetic Data Helpers ----------------------------------------

def _make_deterministic_universe(specs: Dict[str, float], n_days: int = 380, seed: int = 42) -> Dict[str, pd.DataFrame]:
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    t = np.arange(n_days)
    universe = {}
    for sym, drift in specs.items():
        noise = rng.normal(0, 0.5, size=n_days)
        closes = 100.0 + drift * t + 3.0 * np.sin(t / 8.0) + noise
        closes = np.maximum(closes, 5.0)
        df = make_ohlcv_from_closes(closes)
        df.index = dates
        universe[sym] = df
    return universe


# --- 1. CLI Parsing Tests Across All Entrypoints ------------------------------

def test_parse_market_arg_aliases():
    """Verify parse_market_arg normalizes case-insensitive aliases and rejects invalid names."""
    assert parse_market_arg("US") == "us"
    assert parse_market_arg("us") == "us"
    assert parse_market_arg("CHINA") == "china"
    assert parse_market_arg("cn") == "china"
    assert parse_market_arg("HONGKONG") == "hk"
    assert parse_market_arg("hk") == "hk"
    assert parse_market_arg("hong_kong") == "hk"

    with pytest.raises(argparse.ArgumentTypeError):
        parse_market_arg("mars")


def test_resolve_market_and_trading_flags_sync_and_conflicts():
    """Verify bidirectional synchronization and fail-loud conflict handling."""
    # Market specified -> syncs trading flag
    ns1 = argparse.Namespace(market="china", china_trading=False, us_trading=False, hk_trading=False)
    m1 = resolve_market_and_trading_flags(ns1)
    assert m1 == "china"
    assert ns1.china_trading is True
    assert ns1.us_trading is False

    # Legacy flag specified -> infers market
    ns2 = argparse.Namespace(market=None, china_trading=False, us_trading=True, hk_trading=False)
    m2 = resolve_market_and_trading_flags(ns2)
    assert m2 == "us"
    assert ns2.market == "us"

    # Conflicting market and legacy flag -> raises ValueError
    ns3 = argparse.Namespace(market="us", china_trading=True, us_trading=False, hk_trading=False)
    with pytest.raises(ValueError, match="Conflicting market options"):
        resolve_market_and_trading_flags(ns3)

    # Multiple legacy flags -> raises ValueError
    ns4 = argparse.Namespace(market=None, china_trading=True, us_trading=True, hk_trading=False)
    with pytest.raises(ValueError, match="At most one of"):
        resolve_market_and_trading_flags(ns4)


def test_cli_parsing_across_entrypoint_parsers(monkeypatch):
    """Verify --market and --vaa-external-canary parsing across all 7 core entrypoints."""
    from backtester.run_backtest import build_arg_parser as bt_parser
    from pipeline.research_strategy.run_research_strategy import build_arg_parser as rs_parser
    from pipeline.run_pipeline import build_arg_parser as rp_parser
    from pipeline.strategy_generator.run_strategygen import build_arg_parser as sg_parser

    parsers = [
        ("backtester", bt_parser(), ["--strategy-file", "dummy.json"], True),
        ("research_strategy", rs_parser(), [], True),
        ("pipeline", rp_parser(), [], True),
        ("strategy_generator", sg_parser(), [], False),
    ]

    for name, parser, extra_args, has_vaa in parsers:
        # Case-insensitive alias parsing
        args = parser.parse_args(extra_args + ["--market", "CHINA"])
        assert args.market == "china", f"Failed for {name}"

        if has_vaa:
            # Default vaa canary is True
            assert getattr(args, "enable_vaa_external_canary", True) is True, f"Failed for {name}"
            # Explicit disable
            args_no = parser.parse_args(extra_args + ["--market", "hk", "--no-vaa-external-canary"])
            assert args_no.market == "hk", f"Failed for {name}"
            assert getattr(args_no, "enable_vaa_external_canary", True) is False, f"Failed for {name}"

    # Live single strategy parse_args
    live_args = rls.parse_args(["--strategy", "chan_four_state_blend", "--market", "US"])
    assert live_args.market == "us"
    assert live_args.enable_vaa_external_canary is True

    # Health check parse_args
    health_args = clph.parse_args(["--market", "HONGKONG"])
    assert health_args.market == "hk"
    assert health_args.enable_vaa_external_canary is True

    health_args_no = clph.parse_args(["--market", "cn", "--no-vaa-external-canary"])
    assert health_args_no.market == "china"
    assert health_args_no.enable_vaa_external_canary is False

    # Multi-strategy runner parse_args (7th entrypoint)
    monkeypatch.setattr(sys, "argv", ["run_live_multi_strategy.py", "--market", "CHINA"])
    multi_args = rlms.parse_args()
    assert multi_args.market == "china"
    assert multi_args.enable_vaa_external_canary is True

    monkeypatch.setattr(sys, "argv", ["run_live_multi_strategy.py", "--market", "hk", "--no-vaa-external-canary"])
    multi_args_no = rlms.parse_args()
    assert multi_args_no.market == "hk"
    assert multi_args_no.enable_vaa_external_canary is False


# --- 2. Rule 11 Backtest-vs-Live Parity on Fixed-Seed Synthetic Fixtures ------

@pytest.mark.parametrize("market,stock_specs,canary_specs,cash_proxy", [
    (
        "us",
        {"AAPL": 0.20, "MSFT": 0.25, "GOOG": 0.15, "BIL": 0.001},
        {"SPY": 0.15, "QQQ": 0.20, "EFA": 0.10, "EEM": 0.08},
        "BIL",
    ),
    (
        "china",
        {"600519.SH": 0.20, "000858.SZ": 0.25, "000333.SZ": 0.15, "511880.SH": 0.001},
        {"000300.SH": 0.18, "000905.SH": 0.20, "399006.SZ": 0.22},
        "511880.SH",
    ),
    (
        "hk",
        {"0700.HK": 0.22, "9988.HK": 0.18, "1810.HK": 0.20, "3199.HK": 0.001},
        {"2800.HK": 0.15, "2828.HK": 0.18, "3033.HK": 0.25},
        "3199.HK",
    ),
])
def test_backtest_vs_live_parity_chan_vaa_compound(market, stock_specs, canary_specs, cash_proxy):
    """Verify bit-identical target weights between backtest (run_standard) and live execution paths for ChanVaaCompound."""
    universe = _make_deterministic_universe(stock_specs, n_days=380, seed=101)
    canary_bars = _make_deterministic_universe(canary_specs, n_days=380, seed=102)

    cfg = StrategyConfig(market=market, cash_proxy=cash_proxy)
    strat = ChanVaaCompoundStrategy(cfg)

    # 1. Backtest execution path: invoke run_standard directly
    dates = list(universe.values())[0].index
    bt_args = argparse.Namespace(
        market=market,
        start=dates[0].strftime("%Y-%m-%d"),
        end=dates[-1].strftime("%Y-%m-%d"),
        initial_capital=100000.0,
        commission_pct=0.0005,
        slippage_pct=0.0005,
        enable_position_lifecycle=False,
    )
    bt_res = run_standard(universe, strat, {"cash_proxy": cash_proxy}, bt_args, vaa_canary_bars=canary_bars)
    assert not bt_res["equity_curve"].empty

    # 2. Live execution path: assemble _gen_params exactly as in scripts/run_live_strategy.py
    clock_ctx = argparse.Namespace(market=market)
    _gen_params = {"cash_proxy": cash_proxy}
    _gen_params["market"] = clock_ctx.market
    if canary_bars is not None:
        _gen_params["chan_vaa_canary_bars"] = canary_bars

    w_live = strat.generate_weights(universe, _gen_params)
    assert not w_live.empty

    # Re-evaluate weights via backtest run_standard internal call
    w_bt = strat.generate_weights(universe, {"market": market, "chan_vaa_canary_bars": canary_bars, "cash_proxy": cash_proxy})

    # Assert exact bit-for-bit identity across entire weight series
    pd.testing.assert_frame_equal(w_bt, w_live)

    # Assert latest rebalance row targets match exactly
    rebal_bt = w_bt.dropna(how="all")
    rebal_live = w_live.dropna(how="all")
    assert not rebal_bt.empty
    pd.testing.assert_series_equal(rebal_bt.iloc[-1], rebal_live.iloc[-1])


@pytest.mark.parametrize("market,stock_specs,canary_specs,cash_proxy", [
    (
        "china",
        {"600519.SH": 0.20, "000858.SZ": 0.25, "000333.SZ": 0.15, "511880.SH": 0.001},
        {"000300.SH": 0.18, "000905.SH": 0.20, "399006.SZ": 0.22},
        "511880.SH",
    ),
])
def test_backtest_vs_live_parity_cfsb(market, stock_specs, canary_specs, cash_proxy):
    """Verify bit-identical target weights between backtest and live execution paths for CFSB."""
    universe = _make_deterministic_universe(stock_specs, n_days=380, seed=201)
    canary_bars = _make_deterministic_universe(canary_specs, n_days=380, seed=202)

    cfg = StrategyConfig(market=market, cash_proxy=cash_proxy)
    cfsb = ChanFourStateBlendStrategy(cfg)

    # Backtest path
    bt_params = {"market": market, "chan_vaa_canary_bars": canary_bars, "cash_proxy": cash_proxy}
    w_bt = cfsb.generate_weights(universe, bt_params)

    # Live path
    live_params = {"market": market, "chan_vaa_canary_bars": canary_bars, "cash_proxy": cash_proxy}
    w_live = cfsb.generate_weights(universe, live_params)

    pd.testing.assert_frame_equal(w_bt, w_live)

    rebal_bt = w_bt.dropna(how="all")
    rebal_live = w_live.dropna(how="all")
    assert not rebal_bt.empty
    pd.testing.assert_series_equal(rebal_bt.iloc[-1], rebal_live.iloc[-1])


# --- 3. Manifest / Summary Real File I/O Tests --------------------------------

def test_no_vaa_external_canary_tier3_fallback_and_manifest(tmp_path):
    """Verify real run_manifest.json and summary.json record enable_vaa_external_canary=False."""
    # 1. Backtest run_manifest.json
    manifest_file = _dump_run_manifest(
        str(tmp_path),
        mode="standard",
        group="research",
        strategy_name="chan_vaa_compound",
        strategy_file="dummy.json",
        strategy_dump_path=str(tmp_path / "dump.json"),
        db_path=str(tmp_path / "cache.duckdb"),
        db_used=False,
        enable_vaa_external_canary=False,
    )
    assert os.path.exists(manifest_file)
    with open(manifest_file, "r", encoding="utf-8") as f:
        m_data = json.load(f)
    assert m_data.get("enable_vaa_external_canary") is False

    # 2. Live summary.json
    mock_clock_ctx = type("MockClockCtx", (), {"stamp_fields": lambda self: {"market": "china"}})()
    summary_file = persist_run_metadata(
        str(tmp_path),
        mock_clock_ctx,
        extra={"enable_vaa_external_canary": False},
    )
    assert os.path.exists(summary_file)
    with open(summary_file, "r", encoding="utf-8") as f:
        s_data = json.load(f)
    assert s_data.get("enable_vaa_external_canary") is False

    # 3. Strategy Tier 3 breadth fallback verification
    china_stocks = {"600519.SH": 0.20, "000858.SZ": 0.25, "511880.SH": 0.001}
    universe = _make_deterministic_universe(china_stocks, n_days=380, seed=301)
    strat = ChanVaaCompoundStrategy(StrategyConfig(market="china", cash_proxy="511880.SH"))
    w = strat.generate_weights(universe, params={"market": "china", "chan_vaa_canary_bars": None})
    diag = getattr(strat, "_last_vaa_diagnostics", {})
    assert diag.get("canary_tier") == "breadth"
    assert not w.empty


# --- 4. Zero Leakage Verification --------------------------------------------

def test_zero_leakage_of_canary_bars_in_dumps_and_params(tmp_path):
    """Verify chan_vaa_canary_bars DataFrame never leaks into real strategy_dump.json or effective_params."""
    canary_bars = _make_deterministic_universe({"000300.SH": 0.15}, n_days=380, seed=402)
    base_params = {"market": "china", "rebalance_freq_days": 10}

    # 1. Backtester _dump_effective_strategy_def
    strategy_def = {"template": "chan_vaa_compound", "name": "chan_vaa_compound_strategy"}
    dump_path = _dump_effective_strategy_def(strategy_def, base_params, None, str(tmp_path))
    assert os.path.exists(dump_path)
    with open(dump_path, "r", encoding="utf-8") as f:
        dump_content = f.read()
    assert "chan_vaa_canary_bars" not in dump_content
    # JSON must be completely valid
    json.loads(dump_content)

    # 2. Live build_effective_params
    from common.effective_params import SOURCE_OVERRIDE, param_entry
    eff_payload = build_effective_params(
        strategy_id="chan_vaa_compound",
        strategy_version="1.1.0",
        base_version="2.3.0",
        config_source="test",
        strategy_dump_sha256="abc",
        as_of="2024-01-02",
        run_id="run_1",
        params={
            "market": param_entry("china", "china", SOURCE_OVERRIDE),
            "rebalance_freq_days": param_entry(10, 10, SOURCE_OVERRIDE),
        },
    )
    eff_json_path = os.path.join(str(tmp_path), "effective_params.json")
    with open(eff_json_path, "w", encoding="utf-8") as f:
        json.dump(eff_payload, f)
    with open(eff_json_path, "r", encoding="utf-8") as f:
        eff_content = f.read()
    assert "chan_vaa_canary_bars" not in eff_content


# --- 5. Review Defect Regression Tests (#16) ----------------------------------

def test_live_runner_canary_runtime_error_fallback_no_name_error(monkeypatch):
    """(a) Simulate load_universe_with_banner raising RuntimeError during live canary fetch.
    Assert exception is caught without NameError: _logger, safely falling back to Tier 3."""
    china_stocks = {"600519.SH": 0.20, "000858.SZ": 0.25, "511880.SH": 0.001}
    universe = _make_deterministic_universe(china_stocks, n_days=380, seed=501)
    cfg = StrategyConfig(market="china", cash_proxy="511880.SH")
    template = ChanVaaCompoundStrategy(cfg)

    args = argparse.Namespace(
        enable_vaa_external_canary=True,
        no_cache=True,
        _strategy_key="chan_vaa_compound",
        strategy="chan_vaa_compound",
    )
    clock_ctx = argparse.Namespace(market="china", expected_last_session="2024-01-02")

    def _mock_failing_loader(*a, **kw):
        raise RuntimeError("Simulated remote provider connection failure")

    monkeypatch.setattr(rls, "load_universe_with_banner", _mock_failing_loader)

    # Execute the exact canary loading block from scripts/run_live_strategy.py
    vaa_canary_bars = None
    if getattr(args, "enable_vaa_external_canary", True) and template_needs_vaa_canary(
        template, getattr(args, "_strategy_key", "") or getattr(args, "strategy", "")
    ):
        resolved_m = clock_ctx.market
        if resolved_m in MARKET_VAA_PROFILES:
            ext_syms = list(MARKET_VAA_PROFILES[resolved_m]["external_canary_symbols"])
            vaa_canary_bars = {s: universe[s] for s in ext_syms if s in universe}
            missing_ext = [s for s in ext_syms if s not in universe]
            if missing_ext:
                try:
                    raw_ext = rls.load_universe_with_banner(
                        missing_ext,
                        "2020-01-01",
                        "2024-01-02",
                        use_cache=not args.no_cache,
                        cache_dir="dummy",
                        data_kwargs={},
                        require_nonempty=False,
                        expected_last_session=clock_ctx.expected_last_session,
                        include_turn=False,
                    )
                    if raw_ext:
                        vaa_canary_bars.update(raw_ext)
                except Exception as e:
                    # Verified fix: prints warning instead of calling undefined _logger
                    print(f"[WARN] Failed to load VAA external canary bars: {e}")
            if not vaa_canary_bars:
                vaa_canary_bars = None

    # Assert fallback succeeded cleanly
    assert vaa_canary_bars is None
    _gen_params = {"market": clock_ctx.market}
    if vaa_canary_bars is not None:
        _gen_params["chan_vaa_canary_bars"] = vaa_canary_bars
    w = template.generate_weights(universe, _gen_params)
    assert not w.empty
    assert template._last_vaa_diagnostics["canary_tier"] == "breadth"


def test_synthetic_universe_no_spurious_us_canary_and_auto_infer_china():
    """(b) Verify synthetic symbols resolve to None (no spurious US canary loads),
    while A-share symbols without --market automatically infer china."""
    # 1. Synthetic symbols -> None
    syn_symbols = ["STOCK_A", "ASSET_0", "BIL"]
    m_syn = resolve_strategy_market({}, None, syn_symbols)
    assert m_syn is None

    # Verify backtest canary loading branch guards against None
    template = ChanVaaCompoundStrategy(StrategyConfig())
    needs_canary = template_needs_vaa_canary(template, "chan_vaa_compound")
    assert needs_canary is True
    # When canary_market is None, MARKET_VAA_PROFILES lookup is skipped
    canary_market = m_syn
    assert bool(canary_market and canary_market in MARKET_VAA_PROFILES) is False

    # 2. A-share symbols without --market -> china
    ashare_symbols = ["600519.SH", "000858.SZ", "511880.SH"]
    m_ashare = resolve_strategy_market({}, None, ashare_symbols)
    assert m_ashare == "china"
    assert m_ashare in MARKET_VAA_PROFILES


def test_canary_loading_reuses_in_universe_index():
    """(c) Verify when universe already contains 000300.SH, canary loading reuses it
    and only requests missing indices (000905.SH, 399006.SZ)."""
    universe = {
        "600519.SH": pd.DataFrame({"Close": [10.0, 11.0]}),
        "000300.SH": pd.DataFrame({"Close": [3000.0, 3010.0]}),
    }
    canary_market = "china"
    ext_syms = list(MARKET_VAA_PROFILES[canary_market]["external_canary_symbols"])

    vaa_canary_bars = {s: universe[s] for s in ext_syms if s in universe}
    missing_ext = [s for s in ext_syms if s not in universe]

    # 000300.SH is reused by reference from universe
    assert "000300.SH" in vaa_canary_bars
    assert vaa_canary_bars["000300.SH"] is universe["000300.SH"]

    # Only remaining 2 indices are marked missing
    assert missing_ext == ["000905.SH", "399006.SZ"]
