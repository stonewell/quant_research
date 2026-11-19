"""Unit and integration tests for run_alpha_mining.py CLI and benchmark pipeline.

100% offline: uses synthetic universes only.
"""

import json
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_AM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _AM_ROOT not in sys.path:
    sys.path.insert(0, _AM_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd

from common.testing import make_ohlcv_from_closes
from amine.benchmark import evaluate_rule9_gate, run_alpha_mining_benchmark
from amine.config import AlphaMiningConfig, CombinerConfig, GPConfig, PurgedCVConfig
import run_alpha_mining


def test_cli_arg_parsing_and_config_builder():
    parser = run_alpha_mining.build_arg_parser()
    args = parser.parse_args(
        [
            "--data-provider", "synthetic",
            "--small-data",
            "--gp-population", "24",
            "--gp-generations", "2",
            "--n-splits", "3",
            "--objective", "regression",
            "--label-mode", "market_residual",
            "--sector-neutral",
            "--lowdin-combiner",
            "--k", "5",
            "--exec-lag", "1",
            "--embargo-days", "4",
            "--seed", "123",
        ]
    )
    cfg = run_alpha_mining.build_config_from_args(args)
    assert cfg.gp.population_size == 24
    assert cfg.gp.n_generations == 2
    assert cfg.cv.n_splits == 3
    assert cfg.cv.embargo_days == 4
    assert cfg.combiner.objective == "regression"
    assert cfg.label_mode == "market_residual"
    assert cfg.sector_neutral is True
    assert cfg.lowdin_for_combiner is True
    assert cfg.holding_period == 5
    assert cfg.exec_lag == 1
    assert cfg.seed == 123


def test_cli_end_to_end_synthetic_writes_report_and_oof(tmp_path):
    out_dir = str(tmp_path / "am_out")
    rc = run_alpha_mining.main(
        [
            "--data-provider", "synthetic",
            "--universe", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "JPM", "XOM",
            "--start", "2021-01-01",
            "--end", "2023-12-31",
            "--small-data",
            "--gp-population", "20",
            "--gp-generations", "2",
            "--n-splits", "3",
            "--output-dir", out_dir,
            "--seed", "42",
        ]
    )
    assert rc == 0

    report_path = os.path.join(out_dir, "alpha_mining_report.json")
    oof_path = os.path.join(out_dir, "oof_scores.csv")
    assert os.path.exists(report_path)
    assert os.path.exists(oof_path)

    with open(report_path, "r", encoding="utf-8") as fh:
        report = json.load(fh)

    assert report["strategy_version"] == "1.0.0"
    assert set(report["holdout_arms"].keys()) == {
        "lgbm_top_n",
        "equal_weight_universe",
        "best_single_seed",
        "equal_weight_gp_combo",
        "lgbm_lowdin_on",
    }
    assert report["monitoring"]["report_only"] is True
    assert "passed" in report["rule9_gate"]

    oof_df = pd.read_csv(oof_path, index_col=0)
    assert not oof_df.empty


def test_rule9_gate_pass_on_planted_signal_and_null_noise_check():
    # 1. Planted-signal gate pass verification via evaluate_rule9_gate
    arms_pass = {
        "lgbm_top_n": {
            "ic_summary": {"mean_ic": 0.12, "t_stat": 4.2},
            "sharpe_ratio": 1.8,
            "total_rebalances": 12,
            "total_trades": 24,
        },
        "best_single_seed": {
            "ic_summary": {"mean_ic": 0.05, "t_stat": 1.5},
            "sharpe_ratio": 0.7,
            "total_rebalances": 12,
            "total_trades": 20,
        },
        "equal_weight_gp_combo": {
            "ic_summary": {"mean_ic": 0.08, "t_stat": 2.4},
            "sharpe_ratio": 1.2,
            "total_rebalances": 12,
            "total_trades": 22,
        },
    }
    mon_clean = {
        "adversarial_validation": {"alarm": False},
        "importance_decay": {"alarm": False},
        "turnover_breaker": {"alarm": False},
    }
    gate_res = evaluate_rule9_gate(arms_pass, mon_clean)
    assert gate_res["passed"] is True
    assert all(gate_res["checks"].values())

    # 2. Pure-noise null panel check: pure iid random walk must NOT spuriously clear OOF t >= 2.0
    rng = np.random.default_rng(2026)
    n_dates = 320
    n_syms = 10
    dates = pd.bdate_range("2021-01-04", periods=n_dates)
    universe = {}
    for i in range(n_syms):
        rets = rng.normal(0.0, 0.015, size=n_dates)
        closes = 100.0 * np.exp(np.cumsum(rets))
        df = make_ohlcv_from_closes(closes)
        df.index = dates
        universe[f"N{i}"] = df

    cfg = AlphaMiningConfig(
        holding_period=5,
        exec_lag=1,
        residual_min_obs=5,
        top_n=4,
        seed=2026,
        gp=GPConfig(population_size=16, n_generations=2, tournament_size=3, top_k=4, seed=2026),
        cv=PurgedCVConfig(n_splits=3, embargo_days=3, min_train_dates=10, min_test_dates=5),
        combiner=CombinerConfig.small_data(seed=2026),
    )
    report, _ = run_alpha_mining_benchmark(universe, cfg)
    oof_t = float(report["stage2_combiner_oof"]["ic_summary"]["t_stat"])
    assert oof_t < 2.0, f"Pure-noise null panel unexpectedly produced OOF t={oof_t:.2f} >= 2.0"
