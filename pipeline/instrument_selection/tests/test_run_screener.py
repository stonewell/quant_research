"""Regression tests for the CLI's `--select-method` dispatch (`select_basket`
in run_screener.py) -- kept separate from full `main()` runs so these don't
need to load any market data (this repo's tests never hit yfinance/network;
see README/CLAUDE conventions)."""

import numpy as np
import pandas as pd

from common.cli_utils import shared_data_dir
from selectorbot import data as selectorbot_data
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
    """After the OHLCV cache consolidation, run_screener.py and
    selectorbot/data.py must both resolve their cache directory via the same
    `shared_data_dir()` function, i.e. the literal same path."""
    assert DATA_DIR == shared_data_dir()
    assert selectorbot_data.DATA_DIR == shared_data_dir()
    assert DATA_DIR == selectorbot_data.DATA_DIR


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
