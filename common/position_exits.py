"""Shared stateful single-symbol position-exit loops, used by every
`AllocationTemplate`/timing aspect that trades one symbol at a time with an
entry signal plus a stop-loss/max-holding-days safety net (research_strategy's
RSI, Chan-family, and other single-asset timing strategies). Kept separate
from `common/allocation_templates.py` since this is its own distinct concern
(a stateful per-bar loop, not a weight-shaping utility).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def run_stop_timeout_exit(
    close,
    entry_signal,
    exit_signal,
    stop_loss_pct: float | None,
    max_holding_days: int | None,
    position_size_pct: float,
) -> np.ndarray:
    """Stateful single-symbol position loop: enters on `entry_signal`, holds
    at `position_size_pct` until `exit_signal` fires, a stop-loss (close
    dropping `stop_loss_pct` below the entry price) triggers, or
    `max_holding_days` elapses -- whichever comes first. Returns a per-bar
    raw weight array (explicit 0.0 on every de-risked/flat bar, never NaN,
    per the sparse-weights NaN-vs-0.0 contract).

    `close`/`entry_signal`/`exit_signal` accept either a `pd.Series` or a
    plain `np.ndarray` (via `np.asarray`) -- callers differ on which they
    already have in hand by this point, and this loop only ever needs
    positional access, not the index.
    """
    close_arr = np.asarray(close)
    entry_arr = np.asarray(entry_signal)
    exit_arr = np.asarray(exit_signal)
    n_bars = len(close_arr)
    raw = np.zeros(n_bars)
    in_position, entry_idx = False, 0
    for i in range(n_bars):
        if in_position:
            held = i - entry_idx
            stopped = stop_loss_pct is not None and (close_arr[i] / close_arr[entry_idx] - 1) <= -stop_loss_pct
            timed_out = max_holding_days is not None and held >= max_holding_days
            if exit_arr[i] or stopped or timed_out:
                in_position = False
                raw[i] = 0.0
            else:
                raw[i] = position_size_pct
        elif entry_arr[i]:
            in_position = True
            entry_idx = i
            raw[i] = position_size_pct
    return raw
