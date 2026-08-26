import json
import os
import sys
import tempfile
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from common import cli_utils
from common.testing import make_ohlcv_from_closes
from pmine.pattern_mining import build_pattern_templates, mine_indicator_patterns
from run_pattern_mining import RESULTS_DIR, build_arg_parser, main


def test_build_arg_parser_defaults():
    args = build_arg_parser().parse_args([])
    assert args.data_provider == "yfinance"
    assert args.pattern_min_swing_pct == 0.05
    assert args.pattern_lag_bars == 20
    assert args.cache_ttl_days is None


@patch("run_pattern_mining.load_universe_with_banner")
def test_main_writes_a_well_formed_pattern_report(mock_load, tmp_path, monkeypatch):
    monkeypatch.setattr("run_pattern_mining.RESULTS_DIR", str(tmp_path))
    n = 400
    closes = 100.0 + np.cumsum(np.random.default_rng(0).normal(0, 1, n))
    mock_load.return_value = {"A": make_ohlcv_from_closes(closes), "B": make_ohlcv_from_closes(closes + 1)}

    test_args = ["run_pattern_mining.py", "--universe", "A", "B"]
    with patch.object(sys, "argv", test_args):
        main()

    report_path = os.path.join(str(tmp_path), "pattern_report.json")
    with open(report_path) as f:
        report = json.load(f)

    assert report["status"] in ("ok", "insufficient_data", "insufficient_turning_points")
    assert isinstance(report["findings"], list)
    assert report["run_context"]["universe"] == ["A", "B"]
    assert report["run_context"]["min_swing_pct"] == 0.05
    assert report["run_context"]["lag_bars"] == 20


@patch("run_pattern_mining.load_universe_with_banner")
def test_main_wires_shared_data_dir_and_cache_ttl(mock_load, tmp_path, monkeypatch):
    monkeypatch.setattr("run_pattern_mining.RESULTS_DIR", str(tmp_path))
    closes = 100.0 + np.cumsum(np.random.default_rng(1).normal(0, 1, 400))
    mock_load.return_value = {"A": make_ohlcv_from_closes(closes)}

    test_args = ["run_pattern_mining.py", "--universe", "A", "--cache-ttl-days", "3.5"]
    with patch.object(sys, "argv", test_args):
        main()

    assert mock_load.call_count == 1
    _, call_kwargs = mock_load.call_args
    assert call_kwargs["cache_dir"] == cli_utils.shared_data_dir()
    assert call_kwargs["cache_max_age_days"] == 3.5


def test_pattern_report_round_trip_reconstructs_equivalent_templates(tmp_path):
    """The JSON hand-off must be lossless: building templates from a
    pattern_report.json's findings must match building them directly from a
    live mine_indicator_patterns() call on the same data/seed."""
    n = 1500
    rng = np.random.default_rng(42)
    universe = {
        "A": make_ohlcv_from_closes(100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, n))), start="2015-01-01"),
        "B": make_ohlcv_from_closes(100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, n))), start="2015-01-01"),
    }

    findings, status = mine_indicator_patterns(universe, min_swing_pct=0.03, lag_bars=20, seed=52)
    direct_templates = build_pattern_templates(findings, max_templates=5)

    report_path = tmp_path / "pattern_report.json"
    report_path.write_text(json.dumps({"status": status, "findings": findings.to_dict(orient="records")}))

    loaded = json.loads(report_path.read_text())
    reloaded_findings = pd.DataFrame(loaded["findings"])
    reloaded_templates = build_pattern_templates(reloaded_findings, max_templates=5)

    assert [t.name for t in reloaded_templates] == [t.name for t in direct_templates]
    for direct, reloaded in zip(direct_templates, reloaded_templates):
        assert direct.feature_name == reloaded.feature_name
        assert direct.feature_lookback == reloaded.feature_lookback
        assert direct.threshold == reloaded.threshold
        assert direct.comparison == reloaded.comparison
        assert direct.event_type == reloaded.event_type
        assert direct.mined_p_value == reloaded.mined_p_value
        assert direct.mined_n_events == reloaded.mined_n_events
        assert direct.factor_tags == reloaded.factor_tags
