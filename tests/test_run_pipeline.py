import glob
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import run_pipeline


def _fake_result(returncode=0, stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stderr = stderr
    return result


def test_build_arg_parser_defaults():
    args = run_pipeline.build_arg_parser().parse_args([])
    assert args.data_provider == "synthetic"
    assert args.select_method == "threshold"
    assert args.select_max_k == 8
    assert args.mode == "standard"
    assert args.mine_patterns is False
    assert args.baseline_symbol is None
    assert args.no_plots is False
    assert args.dry_run is False


def _manifest_path(tmp_path):
    matches = glob.glob(os.path.join(str(tmp_path), "pipeline_manifest_*.json"))
    assert len(matches) == 1, f"expected exactly one manifest file, found {matches}"
    return matches[0]


def test_main_happy_path_chains_all_4_steps_in_order(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--universe", "A", "B"])
    monkeypatch.setattr(run_pipeline, "RESULTS_DIR", str(tmp_path))

    mock_run = MagicMock(return_value=_fake_result(returncode=0, stderr=""))
    monkeypatch.setattr(run_pipeline.subprocess, "run", mock_run)

    run_pipeline.main()

    assert mock_run.call_count == 4
    calls = mock_run.call_args_list

    argv1 = calls[0].args[0]
    argv2 = calls[1].args[0]
    argv3 = calls[2].args[0]
    argv4 = calls[3].args[0]

    assert run_pipeline.SCRIPTS["research_strategy"] in argv1
    assert run_pipeline.SCRIPTS["instrument_selection"] in argv2
    assert run_pipeline.SCRIPTS["strategy_generator"] in argv3
    assert run_pipeline.SCRIPTS["backtester"] in argv4

    assert "--universe" in argv2 and "A" in argv2 and "B" in argv2
    assert "--universe-file" in argv3 and run_pipeline.BASKET_PATH in argv3
    assert "--strategy-file" in argv4

    manifest_path = _manifest_path(tmp_path)
    with open(manifest_path) as f:
        manifest = json.load(f)
    assert manifest["status"] == "ok"
    assert len(manifest["steps"]) == 4


def test_main_stops_after_failing_step(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py"])
    monkeypatch.setattr(run_pipeline, "RESULTS_DIR", str(tmp_path))

    mock_run = MagicMock(side_effect=[
        _fake_result(returncode=0, stderr=""),
        _fake_result(returncode=0, stderr=""),
        _fake_result(returncode=1, stderr="boom"),
    ])
    monkeypatch.setattr(run_pipeline.subprocess, "run", mock_run)

    with pytest.raises(SystemExit):
        run_pipeline.main()

    assert mock_run.call_count == 3
    captured = capsys.readouterr()
    assert "boom" in captured.err

    manifest_path = _manifest_path(tmp_path)
    with open(manifest_path) as f:
        manifest = json.load(f)
    assert manifest["status"] == "failed"
    assert len(manifest["steps"]) == 3


def test_mine_patterns_and_baseline_flags_passthrough_only_when_set(monkeypatch, tmp_path):
    monkeypatch.setattr(run_pipeline, "RESULTS_DIR", str(tmp_path))

    # First run: neither flag set.
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py"])
    mock_run = MagicMock(return_value=_fake_result(returncode=0, stderr=""))
    monkeypatch.setattr(run_pipeline.subprocess, "run", mock_run)
    run_pipeline.main()

    for call in mock_run.call_args_list:
        argv = call.args[0]
        assert "--mine-patterns" not in argv
        assert "--baseline-symbol" not in argv

    # Second run: both flags set.
    monkeypatch.setattr(sys, "argv", [
        "run_pipeline.py", "--mine-patterns",
        "--baseline-symbol", "SPY", "--baseline-template", "equal_weight",
    ])
    mock_run2 = MagicMock(return_value=_fake_result(returncode=0, stderr=""))
    monkeypatch.setattr(run_pipeline.subprocess, "run", mock_run2)
    run_pipeline.main()

    calls = mock_run2.call_args_list
    argv_step3 = calls[2].args[0]
    argv_step4 = calls[3].args[0]

    assert "--mine-patterns" in argv_step3
    assert "--baseline-symbol" in argv_step4 and "SPY" in argv_step4
    assert "--baseline-template" in argv_step4 and "equal_weight" in argv_step4


def test_dry_run_never_invokes_subprocess(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    monkeypatch.setattr(run_pipeline, "RESULTS_DIR", str(tmp_path))

    mock_run = MagicMock()
    monkeypatch.setattr(run_pipeline.subprocess, "run", mock_run)

    run_pipeline.main()

    mock_run.assert_not_called()
