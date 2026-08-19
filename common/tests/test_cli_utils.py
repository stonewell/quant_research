"""Unit tests for shared CLI scaffolding (common/cli_utils.py). Guaranteed
100% offline/synthetic -- load_universe is monkeypatched, never called for real.
"""

import argparse
import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import common.cli_utils as cli_utils
from common.cli_utils import (
    add_data_provider_cli_args,
    build_data_kwargs,
    default_data_dir,
    default_results_dir,
    load_universe_with_banner,
)


def test_default_results_dir_and_data_dir_are_siblings_of_caller_file():
    fake_caller = os.path.join("some", "project", "run_thing.py")
    results = default_results_dir(fake_caller)
    data = default_data_dir(fake_caller)
    assert os.path.basename(results) == "results"
    assert os.path.basename(data) == "data"
    assert os.path.dirname(results) == os.path.dirname(data) == os.path.dirname(os.path.abspath(fake_caller))


def test_add_data_provider_cli_args_default_yfinance():
    p = argparse.ArgumentParser()
    add_data_provider_cli_args(p)
    args = p.parse_args([])
    assert args.data_provider == "yfinance"
    assert args.data_dir is None
    assert args.no_cache is False
    assert "'yfinance'" in p.format_help()


def test_add_data_provider_cli_args_default_synthetic_lists_it_first_in_help():
    p = argparse.ArgumentParser()
    add_data_provider_cli_args(p, default_provider="synthetic")
    args = p.parse_args([])
    assert args.data_provider == "synthetic"
    help_text = p.format_help()
    provider_line = [l for l in help_text.splitlines() if "Market data source provider" in l][0]
    assert provider_line.index("'synthetic'") < provider_line.index("'yfinance'")


def test_add_data_provider_cli_args_no_cache_help_text():
    p = argparse.ArgumentParser()
    add_data_provider_cli_args(p, no_cache_help="Disable local CSV caching of fetched data")
    assert "Disable local CSV caching of fetched data" in p.format_help()


def test_add_data_provider_cli_args_overridable_by_cli():
    p = argparse.ArgumentParser()
    add_data_provider_cli_args(p)
    args = p.parse_args(["--data-provider", "csv", "--data-dir", "/tmp/x", "--no-cache"])
    assert args.data_provider == "csv"
    assert args.data_dir == "/tmp/x"
    assert args.no_cache is True


def test_build_data_kwargs_without_data_dir():
    args = argparse.Namespace(data_provider="synthetic", data_dir=None)
    assert build_data_kwargs(args) == {"provider": "synthetic"}


def test_build_data_kwargs_with_data_dir():
    args = argparse.Namespace(data_provider="csv", data_dir="/tmp/x")
    assert build_data_kwargs(args) == {"provider": "csv", "folder_path": "/tmp/x"}


def test_load_universe_with_banner_all_succeed(monkeypatch, capsys):
    def _fake_load_universe(symbols, start, end, interval, use_cache=True, cache_dir=None, **kwargs):
        return {s: object() for s in symbols}

    monkeypatch.setattr(cli_utils, "load_universe", _fake_load_universe)
    universe = load_universe_with_banner(["A", "B"], "2020-01-01", "2020-12-31")
    assert set(universe.keys()) == {"A", "B"}
    out = capsys.readouterr().out
    assert "Loading 2 symbols ..." in out
    assert "Loaded 2/2 symbols" in out


def test_load_universe_with_banner_partial_load_require_nonempty_false(monkeypatch, capsys):
    def _fake_load_universe(symbols, start, end, interval, use_cache=True, cache_dir=None, **kwargs):
        return {"A": object()}  # "B" silently failed to load

    monkeypatch.setattr(cli_utils, "load_universe", _fake_load_universe)
    universe = load_universe_with_banner(["A", "B"], "2020-01-01", "2020-12-31", require_nonempty=False)
    assert set(universe.keys()) == {"A"}


def test_load_universe_with_banner_raises_when_empty_and_require_nonempty(monkeypatch):
    monkeypatch.setattr(cli_utils, "load_universe", lambda *a, **k: {})
    with pytest.raises(ValueError):
        load_universe_with_banner(["A", "B"], "2020-01-01", "2020-12-31", require_nonempty=True)


def test_load_universe_with_banner_no_raise_when_empty_and_not_required(monkeypatch):
    monkeypatch.setattr(cli_utils, "load_universe", lambda *a, **k: {})
    universe = load_universe_with_banner(["A", "B"], "2020-01-01", "2020-12-31", require_nonempty=False)
    assert universe == {}


def test_load_universe_with_banner_custom_loading_msg(monkeypatch, capsys):
    monkeypatch.setattr(cli_utils, "load_universe", lambda *a, **k: {"A": object()})
    load_universe_with_banner(["A"], "2020-01-01", "2020-12-31", loading_msg="Custom banner text")
    out = capsys.readouterr().out
    assert "Custom banner text" in out
    assert "Loading 1 symbols ..." not in out
