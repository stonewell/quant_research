"""Percentage-based zigzag turning-point detector.

Nothing like this exists elsewhere in this workspace: `common/metrics.py`'s
`max_drawdown` only returns the WORST peak-to-trough MAGNITUDE as a single
float, never which dates the peak/trough actually fell on. This module finds
every major turning point in a price series, filtered by a minimum swing
size so genuine reversals aren't drowned out by day-to-day noise -- the
standard technical-analysis "zigzag" construction.

CONFIRMATION LAG (read before using this for anything beyond research/mining):
a swing can only be confirmed a turning point once price has moved
`min_swing_pct` AWAY from it in the opposite direction -- by construction,
`find_turning_points` needs a few bars of HINDSIGHT to label date T a peak or
trough. This is legitimate for a research/mining pass (the exact same
hindsight `max_drawdown` already uses) but means a labeled turning point is
NOT something you could have identified in real time on day T itself -- only
a few bars later, once the reversal was confirmed. The most recent candidate
swing extreme is therefore deliberately EXCLUDED from the returned frame
("repainting" avoidance): future bars could still extend it or reverse it,
so treating it as confirmed would make this function's answer for recent
dates change every time new data arrives, unlike every other, already-locked
-in turning point it returns.

See `pattern_mining.py`'s module docstring for how this confirmation lag is
handled downstream: `PatternBasedAllocationTemplate`'s actual trading signal
never calls this function at all -- it only compares a live, already-known
indicator reading against a mined threshold, so it never needs to detect
"is today a pivot" and never inherits this lag.
"""

import pandas as pd


def find_turning_points(close: pd.Series, min_swing_pct: float = 0.05) -> pd.DataFrame:
    """Returns a DataFrame indexed by date with columns `type`
    ("peak"/"trough") and `price`, one row per CONFIRMED turning point.

    A swing must move at least `min_swing_pct` from the running extreme
    since the last confirmed turning point before a new, opposite-direction
    turning point is confirmed. The direction of the very first swing is
    itself only decided once price first moves `min_swing_pct` away from
    the series' own first value.
    """
    if len(close) < 2:
        return pd.DataFrame(columns=["type", "price"])

    values = close.to_numpy()
    dates = close.index
    n = len(values)

    turning_points = []  # list of (position_index, "peak"/"trough")
    direction = None     # None until the first confirmed move; then "up" or "down"
    anchor_price = values[0]
    extreme_idx, extreme_price = 0, values[0]

    for i in range(1, n):
        price = values[i]

        if direction is None:
            if price >= anchor_price * (1 + min_swing_pct):
                direction = "up"
                extreme_idx, extreme_price = i, price
            elif price <= anchor_price * (1 - min_swing_pct):
                direction = "down"
                extreme_idx, extreme_price = i, price
            continue

        if direction == "up":
            if price > extreme_price:
                extreme_idx, extreme_price = i, price
            elif price <= extreme_price * (1 - min_swing_pct):
                turning_points.append((extreme_idx, "peak"))
                direction = "down"
                extreme_idx, extreme_price = i, price
        else:  # direction == "down"
            if price < extreme_price:
                extreme_idx, extreme_price = i, price
            elif price >= extreme_price * (1 + min_swing_pct):
                turning_points.append((extreme_idx, "trough"))
                direction = "up"
                extreme_idx, extreme_price = i, price

    # The final running extreme (`extreme_idx`) is deliberately NOT included
    # here -- see the module docstring's "confirmation lag" / repainting note.

    if not turning_points:
        return pd.DataFrame(columns=["type", "price"])

    idx_positions = [p[0] for p in turning_points]
    types = [p[1] for p in turning_points]
    return pd.DataFrame(
        {"type": types, "price": values[idx_positions]},
        index=dates[idx_positions],
    )
