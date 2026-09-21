import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np
import pandas as pd
import pytest

from common.position_exits import run_stop_timeout_exit


def test_run_stop_timeout_exit_close_only():
    # 5 bars
    close = pd.Series([100.0, 95.0, 93.0, 91.0, 90.0])
    entry = pd.Series([True, False, False, False, False])
    exit_sig = pd.Series([False, False, False, False, False])

    # Stop loss at 8%: entry 100 -> stop level is 92.
    # Close: bar 0=100 (enter), bar 1=95 (held), bar 2=93 (held), bar 3=91 (stopped <= 92)
    weights = run_stop_timeout_exit(close, entry, exit_sig, stop_loss_pct=0.08, max_holding_days=60, position_size_pct=1.0)
    assert weights[0] == 1.0
    assert weights[1] == 1.0
    assert weights[2] == 1.0
    assert weights[3] == 0.0
    assert weights[4] == 0.0


def test_run_stop_timeout_exit_intraday_low():
    # Entry at 100 on bar 0.
    # On bar 1, Close is 95 (-5%), but Low dropped to 90 (-10%).
    # Stop loss at 8% (stop price 92).
    # Without low: bar 1 is NOT stopped (Close 95 > 92).
    # With low: bar 1 IS stopped (Low 90 <= 92).
    close = pd.Series([100.0, 95.0, 96.0, 97.0])
    low = pd.Series([99.0, 90.0, 95.0, 96.0])
    high = pd.Series([101.0, 96.0, 97.0, 98.0])
    entry = pd.Series([True, False, False, False])
    exit_sig = pd.Series([False, False, False, False])

    # 1. Close only: does not trigger stop loss on bar 1
    w_close = run_stop_timeout_exit(close, entry, exit_sig, stop_loss_pct=0.08, max_holding_days=60, position_size_pct=1.0)
    assert w_close[1] == 1.0

    # 2. With low provided: intraday low breaches 92 -> stops out on bar 1
    w_intraday = run_stop_timeout_exit(close, entry, exit_sig, stop_loss_pct=0.08, max_holding_days=60, position_size_pct=1.0, low=low, high=high)
    assert w_intraday[0] == 1.0
    assert w_intraday[1] == 0.0
    assert w_intraday[2] == 0.0
