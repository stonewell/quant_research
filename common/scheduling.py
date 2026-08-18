"""Shared rebalance-schedule helper used by allocation templates and strategies
across every project in this workspace.
"""

import pandas as pd


def get_rebalance_dates(index: pd.DatetimeIndex, freq_days: int) -> pd.DatetimeIndex:
    """Simple rebalance schedule: every N trading days."""
    return index[::freq_days]
