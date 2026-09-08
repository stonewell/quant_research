"""Regression tests for the CLI's `--select-method` dispatch (`select_basket`
in run_screener.py) -- kept separate from full `main()` runs so these don't
need to load any market data (this repo's tests never hit yfinance/network;
see README/CLAUDE conventions)."""

import numpy as np
import pandas as pd

from common.cli_utils import shared_data_dir
from selectorbot.config import SelectionConfig
from run_screener import DATA_DIR, build_arg_parser, select_basket


def _make_scored(n=10):
    """`n` symbols with distinct scores and realized_vol, and an (almost)
    diagonal correlation matrix so nothing gets naturally deduplicated --
    isolates the CLI-wiring question (does --select-max-k reach the
    selection function at all?) from the diversification math itself."""
    symbols = [f"SYM{i}" for i in range(n)]
    rng = np.random.default_rng(0)
    scored = pd.DataFrame({
        "overall_selection_score": np.linspace(90.0, 50.0, n),
        "realized_vol_annualized_pct": rng.uniform(10.0, 30.0, n),
    }, index=symbols)
    corr_arr = np.eye(n) + rng.normal(0, 0.01, (n, n))
    np.fill_diagonal(corr_arr, 1.0)
    corr = pd.DataFrame(corr_arr, index=symbols, columns=symbols)
    return scored, corr


def test_data_dir_is_shared_workspace_wide_cache():
    """After the OHLCV cache consolidation, run_screener.py resolves its
    cache directory via the shared `shared_data_dir()` function."""
    assert DATA_DIR == shared_data_dir()


def test_cache_ttl_days_defaults_to_none():
    args = build_arg_parser().parse_args([])
    assert args.cache_ttl_days is None


def test_cache_ttl_days_parses_as_float():
    args = build_arg_parser().parse_args(["--cache-ttl-days", "7"])
    assert args.cache_ttl_days == 7.0


def test_select_max_k_defaults_to_none():
    args = build_arg_parser().parse_args(["--select-method", "max_diversification"])
    assert args.select_max_k is None


def test_max_diversification_self_sizes_when_select_max_k_omitted():
    scored, corr = _make_scored(n=10)
    config = SelectionConfig()
    args = build_arg_parser().parse_args(["--select-method", "max_diversification"])
    chosen = select_basket(args, config, scored, corr)
    # Self-sizes to the full surviving universe (per README's documented
    # contract for max_diversification/threshold) -- NOT capped at the
    # unrelated --top-n default of 8, and NOT capped at --select-k (unset).
    assert len(chosen) == 10
    assert len(chosen) > 8


def test_max_diversification_respects_select_max_k_cap():
    scored, corr = _make_scored(n=10)
    config = SelectionConfig()
    args = build_arg_parser().parse_args(["--select-method", "max_diversification", "--select-max-k", "3"])
    chosen = select_basket(args, config, scored, corr)
    assert len(chosen) == 3


def test_strategy_cli_args_parsing():
    args = build_arg_parser().parse_args([])
    assert args.strategy is None
    assert args.strategy_file is None
    assert args.no_simulation is False
    assert args.strategy_weight == 0.40

    args = build_arg_parser().parse_args([
        "--strategy", "trend",
        "--no-simulation",
        "--strategy-weight", "0.50",
    ])
    assert args.strategy == "trend"
    assert args.no_simulation is True
    assert args.strategy_weight == 0.50


def test_screener_main_runs_with_strategy_style_preset(monkeypatch, tmp_path):
    import json
    import run_screener

    monkeypatch.setattr(run_screener, "RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_screener.py",
            "--data-provider", "synthetic",
            "--universe", "SPY", "QQQ", "GLD", "TLT",
            "--strategy", "trend",
            "--no-plots",
        ]
    )
    run_screener.main()

    basket_path = tmp_path / "basket.json"
    assert basket_path.exists()
    with open(basket_path) as f:
        data = json.load(f)
    assert "strategy_target" in data
    assert data["strategy_target"]["style_label"] == "trend"

    fit_summary_path = tmp_path / "strategy_fit_summary.json"
    assert fit_summary_path.exists()
    with open(fit_summary_path) as f:
        fit_data = json.load(f)
    assert fit_data["strategy_name"] == "Trend-Following / Breakout Style"


def test_screener_main_runs_with_strategy_file(monkeypatch, tmp_path):
    import json
    import run_screener

    strat_file = tmp_path / "test_strat.json"
    with open(strat_file, "w") as f:
        json.dump({
            "template_name": "equal_weight",
            "name": "Test Strategy",
            "params": {"rebalance_freq_days": 21},
        }, f)

    results_dir = tmp_path / "results"
    monkeypatch.setattr(run_screener, "RESULTS_DIR", str(results_dir))
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_screener.py",
            "--data-provider", "synthetic",
            "--universe", "SPY", "QQQ", "GLD", "TLT",
            "--strategy-file", str(strat_file),
            "--no-plots",
        ]
    )
    run_screener.main()

    basket_path = results_dir / "basket.json"
    assert basket_path.exists()
    with open(basket_path) as f:
        data = json.load(f)
    assert "strategy_target" in data
    assert data["strategy_target"]["name"] == "Test Strategy"

