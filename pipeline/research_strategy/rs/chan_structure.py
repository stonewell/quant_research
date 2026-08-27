"""Independent, from-scratch implementation of 缠中说禅 ("Chan theory") price
structure detection: inclusion-merged bars -> fractals (分型) -> strokes
(笔) -> pivots (中枢).

This module is written natively for this project. It does NOT import,
port, or copy any code, formula, or default parameter from the `czsc`
Rust/Python library (a separate third-party project this workspace is
aware of only as prior art on the same theory) -- every rule below is
this project's own, disclosed reading of the theory, including several
deliberate simplifications called out inline. Only pandas/numpy are used.

Pipeline, each stage causal (no lookahead beyond what the theory itself
requires -- a fractal is only knowable once the bar after its center bar
prints):

1. `merge_inclusion`: collapse inclusion-relationship bars (K线包含关系)
   into single merged bars.
2. `find_fractals`: local 3-bar extrema (顶分型/底分型) on the merged series.
3. `build_strokes`: alternate top/bottom fractals into strokes (笔),
   enforcing a minimum merged-bar gap between the fractals of consecutive
   strokes (independence rule) and keeping the more extreme fractal when
   two of the same kind appear back to back.
4. `build_pivots`: any `min_strokes` consecutive strokes whose price ranges
   overlap form a pivot (中枢); it extends while further strokes keep
   overlapping the band, and closes once one doesn't.
5. `compute_chan_signals`: derives per-bar buy/sell signals from how pivots
   shift over time -- see its docstring for the exact rule.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_STROKE_COLUMNS = ["start_pos", "end_pos", "start_price", "end_price", "direction", "bars"]
_PIVOT_COLUMNS = ["start_pos", "end_pos", "zg", "zd", "gg", "dd", "start_stroke_idx", "end_stroke_idx"]


def merge_inclusion(df: pd.DataFrame) -> pd.DataFrame:
    """Collapses K-line inclusion relationships (一根 K 线的高低点被另一根完全
    包住) into single merged bars, per the standard Chan preprocessing step.

    Returns a DataFrame indexed by the timestamp of each merged bar's last
    contributing original bar, columns `high`/`low` (merged extremes) and
    `orig_pos` (that original bar's integer position in `df`).

    Simplification (disclosed): the merge direction for an inclusion pair
    should, per the theory, follow the trend established before the pair;
    this implementation instead tracks it directly off the running merged
    series (the bar before the current merged top) and treats the
    not-yet-determined case (only one merged bar exists so far) as "up" --
    an inconsequential edge case affecting at most the very first bars.
    """
    highs = df["High"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
    n = len(df)
    if n == 0:
        return pd.DataFrame(columns=["high", "low", "orig_pos"])

    m_idx = [0]
    m_high = [highs[0]]
    m_low = [lows[0]]
    direction = 0  # 0/1 = up-biased, -1 = down

    for i in range(1, n):
        h, l = highs[i], lows[i]
        top_h, top_l = m_high[-1], m_low[-1]
        included = (h <= top_h and l >= top_l) or (h >= top_h and l <= top_l)
        if included:
            if direction >= 0:
                m_high[-1] = max(top_h, h)
                m_low[-1] = max(top_l, l)
            else:
                m_high[-1] = min(top_h, h)
                m_low[-1] = min(top_l, l)
            m_idx[-1] = i
        else:
            if h > top_h and l > top_l:
                direction = 1
            elif h < top_h and l < top_l:
                direction = -1
            m_idx.append(i)
            m_high.append(h)
            m_low.append(l)

    merged_index = df.index[m_idx]
    return pd.DataFrame({"high": m_high, "low": m_low, "orig_pos": m_idx}, index=merged_index)


def find_fractals(merged: pd.DataFrame) -> pd.DataFrame:
    """Detects top/bottom fractals (顶分型/底分型): a merged bar whose high
    (low) is strictly greater (less) than both immediate neighbors' highs
    (lows).

    Returns a DataFrame indexed by the fractal's timestamp, columns `pos`
    (its integer position within `merged`), `kind` (`"top"` or `"bottom"`),
    `price` (the extreme value).

    Simplification (disclosed): a merged bar that would qualify as BOTH a
    top and a bottom fractal simultaneously (a single very wide-range bar
    straddling both neighbors) is dropped rather than assigned a kind --
    it doesn't carry an unambiguous single-direction turning signal.
    """
    if len(merged) < 3:
        return pd.DataFrame(columns=["pos", "kind", "price"])

    highs = merged["high"].to_numpy()
    lows = merged["low"].to_numpy()
    rows = []
    for i in range(1, len(merged) - 1):
        is_top = highs[i] > highs[i - 1] and highs[i] > highs[i + 1]
        is_bottom = lows[i] < lows[i - 1] and lows[i] < lows[i + 1]
        if is_top and not is_bottom:
            rows.append((merged.index[i], i, "top", highs[i]))
        elif is_bottom and not is_top:
            rows.append((merged.index[i], i, "bottom", lows[i]))

    if not rows:
        return pd.DataFrame(columns=["pos", "kind", "price"])
    idx, pos, kind, price = zip(*rows)
    return pd.DataFrame({"pos": pos, "kind": kind, "price": price}, index=pd.Index(idx))


def build_strokes(fractals: pd.DataFrame, min_gap_bars: int) -> pd.DataFrame:
    """Alternates confirmed fractals into strokes (笔).

    Walks fractals in time order, maintaining a list of confirmed,
    strictly-alternating-kind fractals: a same-kind fractal following the
    last confirmed one replaces it if more extreme (refining the same
    swing point); an opposite-kind fractal is only accepted as the next
    confirmed point if at least `min_gap_bars` merged bars separate it from
    the last confirmed fractal (independence rule) -- otherwise it is
    treated as noise and dropped. Each pair of consecutive confirmed
    fractals becomes one stroke.

    Returns a DataFrame with columns `start_pos`/`end_pos` (merged-bar
    positions), `start_price`/`end_price`, `direction` (`"up"` if the
    stroke ends at a top fractal, else `"down"`), `bars` (`end_pos -
    start_pos`).
    """
    if len(fractals) < 2:
        return pd.DataFrame(columns=_STROKE_COLUMNS)

    confirmed = [fractals.iloc[0]]
    for i in range(1, len(fractals)):
        fx = fractals.iloc[i]
        last = confirmed[-1]
        if fx["kind"] == last["kind"]:
            if (fx["kind"] == "top" and fx["price"] > last["price"]) or (
                fx["kind"] == "bottom" and fx["price"] < last["price"]
            ):
                confirmed[-1] = fx
        else:
            if fx["pos"] - last["pos"] >= min_gap_bars:
                confirmed.append(fx)
            # else: too close to be an independent turning point -- drop it.

    if len(confirmed) < 2:
        return pd.DataFrame(columns=_STROKE_COLUMNS)

    rows = []
    for a, b in zip(confirmed[:-1], confirmed[1:]):
        rows.append(
            {
                "start_pos": int(a["pos"]),
                "end_pos": int(b["pos"]),
                "start_price": float(a["price"]),
                "end_price": float(b["price"]),
                "direction": "up" if b["kind"] == "top" else "down",
                "bars": int(b["pos"] - a["pos"]),
            }
        )
    return pd.DataFrame(rows, columns=_STROKE_COLUMNS)


def build_pivots(strokes: pd.DataFrame, min_strokes: int = 3) -> pd.DataFrame:
    """Builds pivots (中枢) from consecutive overlapping strokes.

    A pivot seed is any run of `min_strokes` (theory minimum: 3) consecutive
    strokes whose price ranges share a common overlap `[zd, zg]` (`zg =
    min(high)`, `zd = max(low)` across the window, valid only if `zg >
    zd`). It then extends one stroke at a time while the next stroke's
    range still intersects `[zd, zg]`, tracking `gg`/`dd` as the running
    high/low extremes over all included strokes; it closes the moment a
    stroke's range no longer intersects the band. Scanning resumes
    immediately after a closed pivot (no overlapping pivots).

    Returns a DataFrame with columns `start_pos`/`end_pos` (merged-bar
    positions spanned), `zg`/`zd` (pivot band), `gg`/`dd` (extremes reached
    while the pivot was open), `start_stroke_idx`/`end_stroke_idx` (integer
    positions into `strokes`).
    """
    n = len(strokes)
    if n < min_strokes:
        return pd.DataFrame(columns=_PIVOT_COLUMNS)

    lows = np.minimum(strokes["start_price"].to_numpy(), strokes["end_price"].to_numpy())
    highs = np.maximum(strokes["start_price"].to_numpy(), strokes["end_price"].to_numpy())
    start_pos = strokes["start_pos"].to_numpy()
    end_pos = strokes["end_pos"].to_numpy()

    pivots = []
    i = 0
    while i + min_strokes <= n:
        window_high = highs[i : i + min_strokes]
        window_low = lows[i : i + min_strokes]
        zg = float(window_high.min())
        zd = float(window_low.max())
        if zg <= zd:
            i += 1
            continue

        gg = float(window_high.max())
        dd = float(window_low.min())
        j = i + min_strokes
        while j < n and lows[j] < zg and highs[j] > zd:
            gg = max(gg, float(highs[j]))
            dd = min(dd, float(lows[j]))
            j += 1

        pivots.append(
            {
                "start_pos": int(start_pos[i]),
                "end_pos": int(end_pos[j - 1]),
                "zg": zg,
                "zd": zd,
                "gg": gg,
                "dd": dd,
                "start_stroke_idx": i,
                "end_stroke_idx": j - 1,
            }
        )
        i = j

    return pd.DataFrame(pivots, columns=_PIVOT_COLUMNS)


def compute_chan_signals(df: pd.DataFrame, min_gap_bars: int = 4, min_strokes: int = 3) -> pd.DataFrame:
    """Derives per-bar `buy_signal`/`sell_signal` booleans (aligned to
    `df.index`) from the Chan structure above.

    Rules (an original, disclosed reading of "trend = pivots stepping up/
    down", not a reproduction of any formal 买卖点 taxonomy):

    - **Buy**: once a pivot's band steps wholly above the prior pivot's
      band (`curr.zd >= prev.zg`), buy at the confirmation of the first
      down-stroke at/after that pivot's start (the pullback low that
      follows the breakout) -- one bar after that stroke's ending fractal,
      to respect the fractal confirmation lag.
    - **Sell (pivot shift down)**: symmetric -- `curr.zg <= prev.zd`, sell
      at the confirmation of the first up-stroke at/after that pivot's
      start.
    - **Sell (momentum divergence proxy)**: for consecutive up-strokes
      where the later stroke reaches a higher price but with a lower
      `abs(price change) / bar count` ("power"), sell at that stroke's
      confirmation. This is a simple, self-contained proxy -- NOT `czsc`'s
      SNR/rsq structure metrics.

    A pivot's `zg`/`zd` band is only knowable once its minimal `min_strokes`
    window closes (extension only ever updates `gg`/`dd`), so the qualifying
    stroke used for the Buy/Sell timestamp above is always searched for
    starting at that window's OWN last stroke, never earlier -- searching
    from the window's first stroke instead would date the signal to a bar
    before the pivot (and therefore the shift) was actually knowable, i.e.
    lookahead bias.
    """
    buy = pd.Series(False, index=df.index)
    sell = pd.Series(False, index=df.index)

    merged = merge_inclusion(df)
    fractals = find_fractals(merged)
    strokes = build_strokes(fractals, min_gap_bars)
    pivots = build_pivots(strokes, min_strokes)

    def _mark(series: pd.Series, fractal_pos: int) -> None:
        confirm_pos = fractal_pos + 1
        if confirm_pos < len(merged):
            series.loc[merged.index[confirm_pos]] = True

    def _first_stroke_after(start_idx: int, direction: str) -> int | None:
        for si in range(start_idx, len(strokes)):
            if strokes.iloc[si]["direction"] == direction:
                return si
        return None

    for k in range(1, len(pivots)):
        prev_p, curr_p = pivots.iloc[k - 1], pivots.iloc[k]
        # The pivot's zg/zd (and therefore the shift comparison below) is
        # only knowable once its own minimal window closes -- search for the
        # qualifying stroke starting there, never at the window's start.
        window_last_idx = int(curr_p["start_stroke_idx"]) + min_strokes - 1
        if curr_p["zd"] >= prev_p["zg"]:
            si = _first_stroke_after(window_last_idx, "down")
            if si is not None:
                _mark(buy, int(strokes.iloc[si]["end_pos"]))
        if curr_p["zg"] <= prev_p["zd"]:
            si = _first_stroke_after(window_last_idx, "up")
            if si is not None:
                _mark(sell, int(strokes.iloc[si]["end_pos"]))

    up_strokes = strokes[strokes["direction"] == "up"].reset_index(drop=True)
    for idx in range(1, len(up_strokes)):
        a, b = up_strokes.iloc[idx - 1], up_strokes.iloc[idx]
        power_a = abs(a["end_price"] - a["start_price"]) / max(a["bars"], 1)
        power_b = abs(b["end_price"] - b["start_price"]) / max(b["bars"], 1)
        if b["end_price"] > a["end_price"] and power_b < power_a:
            _mark(sell, int(b["end_pos"]))

    return pd.DataFrame({"buy_signal": buy, "sell_signal": sell})
