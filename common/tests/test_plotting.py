"""Unit tests for common/plotting.py. Guaranteed 100% offline -- matplotlib
Agg backend, no display, no network."""

import os
import sys

import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.plotting import _build_equity_curve_figure, plot_equity_curve


def _equity_series(n=50, start="2020-01-01"):
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(range(100, 100 + n), index=idx, dtype=float)


def test_plot_equity_curve_creates_nonempty_file(tmp_path):
    path = plot_equity_curve(_equity_series(), str(tmp_path))
    assert os.path.exists(path)
    assert os.path.getsize(path) > 0


def test_plot_equity_curve_creates_results_dir_if_missing(tmp_path):
    nested = os.path.join(str(tmp_path), "a", "b", "c")
    path = plot_equity_curve(_equity_series(), nested)
    assert os.path.exists(path)
    assert os.path.getsize(path) > 0


def test_plot_equity_curve_filename_override(tmp_path):
    path = plot_equity_curve(_equity_series(), str(tmp_path), filename="custom.png")
    assert os.path.basename(path) == "custom.png"
    assert os.path.exists(path)


def test_plot_equity_curve_with_baseline_creates_file(tmp_path):
    path = plot_equity_curve(_equity_series(), str(tmp_path), baseline=_equity_series(n=50) * 0.9)
    assert os.path.exists(path)
    assert os.path.getsize(path) > 0


def test_build_equity_curve_figure_draws_two_lines_with_baseline():
    fig, ax = _build_equity_curve_figure(_equity_series(), _equity_series() * 0.95)
    assert len(ax.get_lines()) == 2


def test_build_equity_curve_figure_draws_one_line_without_baseline():
    fig, ax = _build_equity_curve_figure(_equity_series())
    assert len(ax.get_lines()) == 1


def test_build_equity_curve_figure_draws_one_line_with_empty_baseline():
    fig, ax = _build_equity_curve_figure(_equity_series(), pd.Series([], dtype=float))
    assert len(ax.get_lines()) == 1
