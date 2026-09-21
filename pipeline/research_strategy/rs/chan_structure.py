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

from collections import OrderedDict

import numpy as np
import pandas as pd

_STROKE_COLUMNS = ["start_pos", "end_pos", "start_price", "end_price", "direction", "bars"]
_PIVOT_COLUMNS = ["start_pos", "end_pos", "zg", "zd", "gg", "dd", "start_stroke_idx", "end_stroke_idx"]
_PIVOT_RELATION_COLUMNS = ["pivot_idx", "direction", "contained", "state"]

_SIGNAL_CACHE: "OrderedDict" = OrderedDict()
_SIGNAL_CACHE_MAXSIZE = 4096


def _signal_cache_key(df: pd.DataFrame, *params) -> tuple:
    """Cheap content fingerprint for memoizing the Chan structural pipeline --
    deliberately NOT `id(df)` (unsafe: object ids get reused once a
    garbage-collected frame's memory is reclaimed), just enough of the
    frame's shape/edges to make an accidental collision between two
    genuinely different price series astronomically unlikely.

    Includes a full hash of `Volume` (not just first/last, unlike `Close`
    below) when present: `compute_chan_pivot_macd_signals`'s
    `require_volume_confirmation` reads Volume, and a first/last-only
    fingerprint failed to distinguish two frames with identical Close but a
    Volume spike confined to the middle of the series (caught by this
    module's own test suite) -- Close has no such caller today, so it keeps
    the cheaper first/last heuristic."""
    close = df["Close"] if "Close" in df.columns else (df["High"] if "High" in df.columns else df.iloc[:, 0])
    volume_fingerprint = hash(df["Volume"].to_numpy().tobytes()) if "Volume" in df.columns else None
    return (
        len(df), df.index[0], df.index[-1],
        round(float(close.iloc[0]), 8), round(float(close.iloc[-1]), 8),
        volume_fingerprint,
        params,
    )


def _cached_signals(prefix: str, df: pd.DataFrame, params: tuple, compute_fn):
    """Bounded LRU memoization shared by `compute_chan_signals`,
    `compute_chan3_signals`, and `compute_chan_pivot_macd_signals`
    (`chan_signals.py` imports this helper). `ChanBestSelectorStrategy` runs
    several sub-strategies that default to identical `(df, min_gap_bars,
    min_strokes, macd_fast, macd_slow, macd_signal)` params per symbol --
    this avoids redoing the full merge/fractal/stroke/pivot pipeline for
    each of them."""
    key = (prefix, _signal_cache_key(df, *params))
    cached = _SIGNAL_CACHE.get(key)
    if cached is not None:
        _SIGNAL_CACHE.move_to_end(key)
        return cached
    result = compute_fn()
    _SIGNAL_CACHE[key] = result
    if len(_SIGNAL_CACHE) > _SIGNAL_CACHE_MAXSIZE:
        _SIGNAL_CACHE.popitem(last=False)
    return result


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
    n = len(merged)
    if n < 3:
        return pd.DataFrame(columns=["pos", "kind", "price"])

    highs = merged["high"].to_numpy(dtype=float)
    lows = merged["low"].to_numpy(dtype=float)

    h_mid = highs[1:-1]
    l_mid = lows[1:-1]
    is_top = (h_mid > highs[:-2]) & (h_mid > highs[2:])
    is_bottom = (l_mid < lows[:-2]) & (l_mid < lows[2:])

    top_only = is_top & ~is_bottom
    bottom_only = is_bottom & ~is_top
    valid = top_only | bottom_only

    if not np.any(valid):
        return pd.DataFrame(columns=["pos", "kind", "price"])

    pos = np.flatnonzero(valid) + 1
    kinds = np.where(top_only[valid], "top", "bottom")
    prices = np.where(top_only[valid], highs[pos], lows[pos])

    return pd.DataFrame({"pos": pos, "kind": kinds, "price": prices}, index=merged.index[pos])


def build_strokes_from_arrays(kinds: np.ndarray, prices: np.ndarray, positions: np.ndarray, min_gap_bars: int):
    """NumPy-native fast stroke construction avoiding DataFrame wrapping."""
    n = len(kinds)
    if n < 2:
        return None

    confirmed = [0]
    for i in range(1, n):
        k, p, pos = kinds[i], prices[i], positions[i]
        last_idx = confirmed[-1]
        last_k, last_p, last_pos = kinds[last_idx], prices[last_idx], positions[last_idx]
        if k == last_k:
            if (k == "top" and p > last_p) or (k == "bottom" and p < last_p):
                confirmed[-1] = i
        else:
            if pos - last_pos >= min_gap_bars:
                confirmed.append(i)

    if len(confirmed) < 2:
        return None

    c_idx = np.array(confirmed)
    starts = positions[c_idx[:-1]]
    ends = positions[c_idx[1:]]
    s_prices = prices[c_idx[:-1]]
    e_prices = prices[c_idx[1:]]
    dirs = np.where(kinds[c_idx[1:]] == "top", "up", "down")
    bars = ends - starts
    return starts, ends, s_prices, e_prices, dirs, bars


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
    n = len(fractals)
    if n < 2:
        return pd.DataFrame(columns=_STROKE_COLUMNS)

    kinds = fractals["kind"].to_numpy()
    prices = fractals["price"].to_numpy(dtype=float)
    positions = fractals["pos"].to_numpy(dtype=int)

    res = build_strokes_from_arrays(kinds, prices, positions, min_gap_bars)
    if res is None:
        return pd.DataFrame(columns=_STROKE_COLUMNS)
    starts, ends, s_prices, e_prices, dirs, bars = res
    return pd.DataFrame({
        "start_pos": starts,
        "end_pos": ends,
        "start_price": s_prices,
        "end_price": e_prices,
        "direction": dirs,
        "bars": bars,
    })


def build_pivots_from_arrays(starts: np.ndarray, ends: np.ndarray, s_prices: np.ndarray, e_prices: np.ndarray, min_strokes: int = 3):
    """NumPy-native fast pivot construction avoiding DataFrame wrapping.
    Returns list of tuples (start_stroke_idx, end_stroke_idx, zg, zd, gg, dd).
    """
    n = len(starts)
    if n < min_strokes:
        return []

    lows = np.minimum(s_prices, e_prices)
    highs = np.maximum(s_prices, e_prices)
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

        pivots.append((i, j - 1, zg, zd, gg, dd))
        i = j

    return pivots


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

    starts = strokes["start_pos"].to_numpy()
    ends = strokes["end_pos"].to_numpy()
    s_prices = strokes["start_price"].to_numpy(dtype=float)
    e_prices = strokes["end_price"].to_numpy(dtype=float)

    raw_pivots = build_pivots_from_arrays(starts, ends, s_prices, e_prices, min_strokes)
    if not raw_pivots:
        return pd.DataFrame(columns=_PIVOT_COLUMNS)

    pivots = [
        {
            "start_pos": int(starts[p[0]]),
            "end_pos": int(ends[p[1]]),
            "zg": p[2],
            "zd": p[3],
            "gg": p[4],
            "dd": p[5],
            "start_stroke_idx": p[0],
            "end_stroke_idx": p[1],
        }
        for p in raw_pivots
    ]
    return pd.DataFrame(pivots, columns=_PIVOT_COLUMNS)


def compute_chan_signals(df: pd.DataFrame, min_gap_bars: int = 4, min_strokes: int = 3, causal: bool = False) -> pd.DataFrame:
    """Cache-wrapped entry point -- see `_compute_chan_signals_impl` for the
    actual rule. Memoized (see `_cached_signals`) since several strategies
    (notably `ChanBestSelectorStrategy`'s sub-strategies) call this with
    identical `df`/params for the same symbol."""
    return _cached_signals(
        "compute_chan_signals", df, (min_gap_bars, min_strokes, causal),
        lambda: _compute_chan_signals_impl(df, min_gap_bars, min_strokes, causal=causal),
    )


def _compute_chan_signals_impl(df: pd.DataFrame, min_gap_bars: int, min_strokes: int, causal: bool = False) -> pd.DataFrame:
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
    if causal:
        buy = pd.Series(False, index=df.index)
        sell = pd.Series(False, index=df.index)
        min_warmup = max(30, min_gap_bars * min_strokes * 2)
        if len(df) <= min_warmup:
            return pd.DataFrame({"buy_signal": buy, "sell_signal": sell})

        highs = df["High"].to_numpy(dtype=float)
        lows = df["Low"].to_numpy(dtype=float)
        m_idx = [0]
        m_high = [highs[0]]
        m_low = [lows[0]]
        direction = 0

        for t in range(1, min_warmup):
            h, l = highs[t], lows[t]
            top_h, top_l = m_high[-1], m_low[-1]
            included = (h <= top_h and l >= top_l) or (h >= top_h and l <= top_l)
            if included:
                if direction >= 0:
                    m_high[-1] = max(top_h, h)
                    m_low[-1] = max(top_l, l)
                else:
                    m_high[-1] = min(top_h, h)
                    m_low[-1] = min(top_l, l)
                m_idx[-1] = t
            else:
                if h > top_h and l > top_l:
                    direction = 1
                elif h < top_h and l < top_l:
                    direction = -1
                m_idx.append(t)
                m_high.append(h)
                m_low.append(l)

        for t in range(min_warmup, len(df)):
            h, l = highs[t], lows[t]
            top_h, top_l = m_high[-1], m_low[-1]
            included = (h <= top_h and l >= top_l) or (h >= top_h and l <= top_l)
            if included:
                if direction >= 0:
                    m_high[-1] = max(top_h, h)
                    m_low[-1] = max(top_l, l)
                else:
                    m_high[-1] = min(top_h, h)
                    m_low[-1] = min(top_l, l)
                m_idx[-1] = t
            else:
                if h > top_h and l > top_l:
                    direction = 1
                elif h < top_h and l < top_l:
                    direction = -1
                m_idx.append(t)
                m_high.append(h)
                m_low.append(l)

            last_dt = df.index[t]
            if m_idx[-1] != t:
                continue
            n_m = len(m_high)
            if n_m < 3:
                continue
            i_m = n_m - 2
            is_top = m_high[i_m] > m_high[i_m - 1] and m_high[i_m] > m_high[i_m + 1]
            is_bottom = m_low[i_m] < m_low[i_m - 1] and m_low[i_m] < m_low[i_m + 1]
            if not ((is_top and not is_bottom) or (is_bottom and not is_top)):
                continue

            last_m_pos = n_m - 1

            h_arr = np.array(m_high)
            l_arr = np.array(m_low)
            h_mid = h_arr[1:-1]
            l_mid = l_arr[1:-1]
            t_top = (h_mid > h_arr[:-2]) & (h_mid > h_arr[2:])
            b_bot = (l_mid < l_arr[:-2]) & (l_mid < l_arr[2:])
            v_top = t_top & ~b_bot
            v_bot = b_bot & ~t_top
            valid = v_top | v_bot
            if not np.any(valid):
                continue
            fx_pos = np.flatnonzero(valid) + 1
            fx_kinds = np.where(v_top[valid], "top", "bottom")
            fx_prices = np.where(v_top[valid], h_arr[fx_pos], l_arr[fx_pos])

            st = build_strokes_from_arrays(fx_kinds, fx_prices, fx_pos, min_gap_bars)
            if st is None:
                continue
            st_starts, st_ends, st_sprices, st_eprices, st_dirs, st_bars = st
            pivots = build_pivots_from_arrays(st_starts, st_ends, st_sprices, st_eprices, min_strokes)

            def _first_stroke_after(start_idx, d_str):
                for si in range(start_idx, len(st_dirs)):
                    if st_dirs[si] == d_str:
                        return si
                return None

            for k in range(1, len(pivots)):
                prev_p, curr_p = pivots[k - 1], pivots[k]
                win_last = curr_p[0] + min_strokes - 1
                if curr_p[3] >= prev_p[2]:  # zd >= prev.zg
                    si = _first_stroke_after(win_last, "down")
                    if si is not None and st_ends[si] + 1 == last_m_pos:
                        buy.loc[last_dt] = True
                if curr_p[2] <= prev_p[3]:  # zg <= prev.zd
                    si = _first_stroke_after(win_last, "up")
                    if si is not None and st_ends[si] + 1 == last_m_pos:
                        sell.loc[last_dt] = True

            up_idx = np.flatnonzero(st_dirs == "up")
            if len(up_idx) >= 2:
                b_si = up_idx[-1]
                if st_ends[b_si] + 1 == last_m_pos:
                    a_si = up_idx[-2]
                    p_a = abs(st_eprices[a_si] - st_sprices[a_si]) / max(st_bars[a_si], 1)
                    p_b = abs(st_eprices[b_si] - st_sprices[b_si]) / max(st_bars[b_si], 1)
                    if st_eprices[b_si] > st_eprices[a_si] and p_b < p_a:
                        sell.loc[last_dt] = True

        return pd.DataFrame({"buy_signal": buy, "sell_signal": sell})

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


def classify_pivot_relations(pivots: pd.DataFrame) -> pd.DataFrame:
    """Classifies each pivot's relationship to the PRIOR pivot, per the
    `(direction, contained)` early-warning notation from Lessons 92-99:
    `direction` is +1/-1 for whether the pivot's own band midpoint
    (`(zg+zd)/2`) moved up or down relative to the prior pivot's midpoint;
    `contained` is `True` while the new pivot's own extreme stays within the
    prior pivot's matching extreme (a healthy continuation -- an uptrend's
    new pivot doesn't undercut the prior pivot's low `dd`; a downtrend's new
    pivot doesn't poke back above the prior pivot's high `gg`), and `False`
    once it instead breaks through against the trend -- the `(dir, 1)`
    "dangerous consolidation" state the lessons use to bank cost / tighten
    exits ahead of a formal sell point.

    Simplification (disclosed): the lessons' own notation is richer (it
    also tracks WHERE inside/outside the band the break happens); this
    reduces it to the single binary "did the new pivot's relevant extreme
    hold or break" comparison, which is what a downstream risk overlay
    actually needs to act on.

    Returns one row per pivot from the second onward: `pivot_idx` (integer
    row-position into `pivots`), `direction` (+1/-1), `contained` (bool),
    `state` (a readable label, e.g. `"(1,0)"`/`"(1,1)"`/`"(-1,0)"`/`"(-1,1)"`).
    """
    if len(pivots) < 2:
        return pd.DataFrame(columns=_PIVOT_RELATION_COLUMNS)

    rows = []
    for k in range(1, len(pivots)):
        prev_p, curr_p = pivots.iloc[k - 1], pivots.iloc[k]
        prev_mid = (float(prev_p["zg"]) + float(prev_p["zd"])) / 2.0
        curr_mid = (float(curr_p["zg"]) + float(curr_p["zd"])) / 2.0
        direction = 1 if curr_mid >= prev_mid else -1

        if direction == 1:
            contained = float(curr_p["dd"]) >= float(prev_p["dd"])
        else:
            contained = float(curr_p["gg"]) <= float(prev_p["gg"])

        rows.append(
            {
                "pivot_idx": k,
                "direction": direction,
                "contained": bool(contained),
                "state": f"({direction},{0 if contained else 1})",
            }
        )

    return pd.DataFrame(rows, columns=_PIVOT_RELATION_COLUMNS)


def compute_stroke_trend(df: pd.DataFrame, min_gap_bars: int = 4) -> pd.Series:
    """Cache-wrapped entry point for stroke trend series (see `_compute_stroke_trend_impl`)."""
    return _cached_signals(
        "compute_stroke_trend",
        df,
        (min_gap_bars,),
        lambda: _compute_stroke_trend_impl(df, min_gap_bars),
    )


def _compute_stroke_trend_impl(df: pd.DataFrame, min_gap_bars: int) -> pd.Series:
    """Derives a per-bar boolean series indicating whether price is in a bullish stroke
    trend (Lesson 107 stroke-level trend definition: up-stroke in progress or higher low established).
    """
    trend = pd.Series(False, index=df.index)
    if len(df) < 5:
        return trend
    merged = merge_inclusion(df)
    fractals = find_fractals(merged)
    strokes = build_strokes(fractals, min_gap_bars)
    if len(strokes) < 2:
        return trend

    trend_arr = np.zeros(len(df), dtype=bool)
    n_strokes = len(strokes)
    for i in range(len(strokes)):
        s_curr = strokes.iloc[i]
        confirm_merged_pos = int(s_curr["end_pos"]) + 1
        if confirm_merged_pos >= len(merged):
            confirm_merged_pos = int(s_curr["end_pos"])
        confirm_ts = merged.index[confirm_merged_pos]
        start_idx = df.index.get_indexer([confirm_ts])[0]
        end_idx = len(df)
        if i + 1 < n_strokes:
            s_next = strokes.iloc[i + 1]
            next_confirm_merged_pos = int(s_next["end_pos"]) + 1
            if next_confirm_merged_pos >= len(merged):
                next_confirm_merged_pos = int(s_next["end_pos"])
            next_ts = merged.index[next_confirm_merged_pos]
            end_idx = df.index.get_indexer([next_ts])[0]
        is_up = s_curr["direction"] == "up"
        higher_low = False
        if not is_up and i >= 2:
            s_prior_down = strokes.iloc[i - 2] if strokes.iloc[i - 2]["direction"] == "down" else None
            if s_prior_down is not None and float(s_curr["end_price"]) > float(s_prior_down["end_price"]):
                higher_low = True
        trend_arr[start_idx:end_idx] = is_up or higher_low
    return pd.Series(trend_arr, index=df.index)

