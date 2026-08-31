"""Additive extension of `chan_structure.py`'s Chan-theory (缠中说禅) structure
detector: segments (线段), real MACD-histogram-area divergence (背驰), and the
formal first/second/third-type buy/sell point taxonomy (一/二/三类买卖点).

This module is new, parallel logic -- it does NOT modify `chan_structure.py`
or `compute_chan_signals` (whose own pivot-shift signal and stroke-slope
"momentum divergence proxy" are already covered by existing tests and
consumed by `ChanPivotShiftStrategy`; both are left exactly as they are).
`chan_structure.py`'s own module docstring states a 5-stage, segment-free
pipeline -- that description stays accurate for THAT module; this module adds
the two extra stages (segments, then segment-level pivots) as its own,
separate computation reusing `chan_structure`'s primitives unchanged.

Pipeline added here:

6. `build_segments`: groups strokes into segments (线段) -- a disclosed,
   price-only proxy for the real characteristic-sequence (特征序列) /
   fractal-on-strokes termination rule (see its own docstring).
7. `build_pivots(segments, ...)`: `chan_structure.build_pivots` is reused
   VERBATIM on `segments` instead of `strokes` -- it only ever reads
   `start_price`/`end_price`/`start_pos`/`end_pos` by column name, and
   `segments`' schema carries those under the same names/units, so no new
   "pivots-on-segments" variant function is needed. NOTE: despite
   `build_pivots`'s own column names, the `start_stroke_idx`/`end_stroke_idx`
   columns of a pivot built this way index into `segments`, not `strokes`.
8. `macd_divergence`: real MACD-histogram-area comparison between two
   same-direction segments, reusing `common.indicators.macd` (previously
   unused anywhere in this project's strategy code).
9. `classify_points`: the formal 一/二/三类买卖点 taxonomy, built on the
   segment-level pivots above and `macd_divergence`.
10. `compute_chan3_signals`: orchestrates 6-9 into per-bar boolean signals,
    aligned to `df.index`, mirroring `compute_chan_signals`'s own shape.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common.indicators import macd

from .chan_structure import _STROKE_COLUMNS, build_pivots, build_strokes, find_fractals, merge_inclusion

_SEGMENT_COLUMNS = _STROKE_COLUMNS + ["start_stroke_idx", "end_stroke_idx"]
_POINT_COLUMNS = ["pos", "price", "kind", "pivot_idx"]


def build_segments(strokes: pd.DataFrame, min_strokes: int = 3) -> pd.DataFrame:
    """Groups strokes (笔) into segments (线段).

    Simplification (disclosed): the real theory terminates a segment via a
    "characteristic sequence" (特征序列) fractal test on same-direction
    stroke projections -- genuinely ambiguous even among practitioners, and
    not implemented here. This uses a price-only proxy instead, in the same
    spirit as `build_pivots`'s own overlap test:

    - Seed: a window of `min_strokes` (theory minimum: 3) consecutive
      strokes starting at candidate index `i`, provisional
      `direction = strokes.iloc[i]["direction"]`. Requires BOTH price
      overlap across the window (`zg = min(high)`, `zd = max(low)`,
      `zg > zd` -- identical test to `build_pivots`'s own seed) AND net
      progress in the provisional direction (the window's last stroke's
      `end_price` must clear the window's first stroke's `start_price` in
      that direction). The progress condition is the one disclosed addition
      beyond a literal port of `build_pivots`'s test: overlap alone can pass
      on a directionless/degenerate range once the fractal-sequence
      machinery is dropped. Fails -> slide `i += 1` and retry.
    - Extend/terminate: once seeded, track the running same-direction
      pointer. After each new pullback stroke becomes available, test full
      retracement -- does it erase back through the start price of the last
      confirmed same-direction stroke? If yes, the segment terminates AT
      that same-direction stroke, and the next (opposite-direction)
      segment's seed search resumes exactly at the eraser pullback's own
      index (mirrors `build_pivots`'s `i = j` resume). If no, extend by one
      more same-direction/pullback pair and repeat.
    - A segment still extending when strokes run out is emitted anyway
      (open/trailing), same as `build_pivots`.

    Simplification (disclosed): because the seed test is re-applied at each
    resume point, an immediate reversal that doesn't itself pass the
    3-stroke overlap+progress test can leave a few strokes belonging to no
    segment (segments can therefore have gaps between them, exactly like
    `build_pivots`'s own pivots already can) -- segments are guaranteed
    non-overlapping, not guaranteed contiguous.

    Returns a DataFrame with the same columns as `strokes`
    (`start_pos`/`end_pos`/`start_price`/`end_price`/`direction`/`bars`)
    plus `start_stroke_idx`/`end_stroke_idx` (integer positions into
    `strokes` this segment spans) -- a strict superset, so a `segments`
    frame is a drop-in wherever a bare strokes-shaped frame is consumed by
    column name (e.g. `build_pivots`).
    """
    n = len(strokes)
    if n < min_strokes:
        return pd.DataFrame(columns=_SEGMENT_COLUMNS)

    starts = strokes["start_price"].to_numpy(dtype=float)
    ends = strokes["end_price"].to_numpy(dtype=float)
    directions = strokes["direction"].to_numpy()
    start_pos = strokes["start_pos"].to_numpy()
    end_pos = strokes["end_pos"].to_numpy()
    highs = np.maximum(starts, ends)
    lows = np.minimum(starts, ends)

    rows = []
    i = 0
    while i + min_strokes <= n:
        direction = directions[i]
        last_seed_idx = i + min_strokes - 1
        zg = float(highs[i : i + min_strokes].min())
        zd = float(lows[i : i + min_strokes].max())
        if direction == "up":
            progress = ends[last_seed_idx] > starts[i]
        else:
            progress = ends[last_seed_idx] < starts[i]
        if not (zg > zd and progress):
            i += 1
            continue

        seg_start_idx = i
        last_same_idx = last_seed_idx
        while True:
            pullback_idx = last_same_idx + 1
            if pullback_idx >= n:
                resume_idx = n
                break
            last_same_start = starts[last_same_idx]
            pullback_end = ends[pullback_idx]
            full_retracement = pullback_end <= last_same_start if direction == "up" else pullback_end >= last_same_start
            if full_retracement:
                resume_idx = pullback_idx
                break
            next_same_idx = pullback_idx + 1
            if next_same_idx >= n:
                resume_idx = pullback_idx
                break
            last_same_idx = next_same_idx

        rows.append(
            {
                "start_pos": int(start_pos[seg_start_idx]),
                "end_pos": int(end_pos[last_same_idx]),
                "start_price": float(starts[seg_start_idx]),
                "end_price": float(ends[last_same_idx]),
                "direction": "up" if ends[last_same_idx] >= starts[seg_start_idx] else "down",
                "bars": int(end_pos[last_same_idx] - start_pos[seg_start_idx]),
                "start_stroke_idx": seg_start_idx,
                "end_stroke_idx": last_same_idx,
            }
        )
        i = resume_idx

    return pd.DataFrame(rows, columns=_SEGMENT_COLUMNS)


def macd_divergence(earlier: pd.Series, later: pd.Series, hist, merged: pd.DataFrame) -> tuple[bool, float, float]:
    """Measures MACD-histogram-area momentum between two same-direction,
    comparable moves (stroke or segment rows sharing the
    `start_pos`/`end_pos`/`start_price`/`end_price`/`direction` schema).

    `hist` must be a plain, POSITION-indexed Series/array of the MACD
    histogram over the ORIGINAL `df["Close"]` bars (e.g.
    `common.indicators.macd(df["Close"])["hist"].reset_index(drop=True)`) --
    decoupled from timestamp indexing, an explicit caller contract to avoid
    alignment bugs. `merged` bridges each row's merged-bar `start_pos`/
    `end_pos` to original-bar positions via its own `orig_pos` column
    (`orig_pos` is the LAST original bar folded into that merged bar --
    disclosed: used as the range boundary anyway, consistent with the rest
    of this project's Chan modules reasoning only in merged-bar space).

    Area = sum of same-sign-as-direction histogram bars over the row's own
    bar range (the standard practitioner method for eyeballing a leg's
    momentum, not a deviation): for "up", positive-clipped `hist` summed;
    for "down", negative-clipped `hist` summed (sign-flipped to a positive
    magnitude). A range with no same-sign bars has area 0.0; a range still
    inside MACD's own EMA warm-up (all-NaN) has area NaN, in which case this
    function reports no divergence rather than raising.

    Divergence fires (simplest textbook rule, no magic threshold) iff BOTH:
    (1) `later` reaches a further price extreme than `earlier` in their
        shared direction, AND
    (2) `later`'s area is STRICTLY smaller than `earlier`'s (a tie is "no
        signal", matching this project's Chan modules' existing preference
        for dropping ambiguous cases rather than guessing).

    Returns `(has_divergence, area_earlier, area_later)`.
    """
    if earlier["direction"] != later["direction"]:
        raise ValueError(
            f"macd_divergence requires earlier/later to share a direction, got "
            f"{earlier['direction']!r} vs {later['direction']!r}"
        )
    direction = earlier["direction"]
    hist_arr = np.asarray(hist, dtype=float)

    def _area(row: pd.Series) -> float:
        lo = int(merged.iloc[int(row["start_pos"])]["orig_pos"])
        hi = int(merged.iloc[int(row["end_pos"])]["orig_pos"])
        window = hist_arr[lo : hi + 1]
        if window.size == 0 or np.all(np.isnan(window)):
            return float("nan")
        window = np.nan_to_num(window, nan=0.0)
        if direction == "up":
            return float(np.clip(window, 0.0, None).sum())
        return float((-np.clip(window, None, 0.0)).sum())

    area_earlier = _area(earlier)
    area_later = _area(later)

    if direction == "up":
        new_extreme = later["end_price"] > earlier["end_price"]
    else:
        new_extreme = later["end_price"] < earlier["end_price"]

    if np.isnan(area_earlier) or np.isnan(area_later):
        has_divergence = False
    else:
        has_divergence = bool(new_extreme and area_later < area_earlier)

    return has_divergence, area_earlier, area_later


def classify_points(segments: pd.DataFrame, pivots: pd.DataFrame, hist, merged: pd.DataFrame) -> pd.DataFrame:
    """Classifies formal first/second/third-type buy/sell points (一/二/三
    类买卖点) from segment-level pivots, per this project's disclosed reading
    of the theory:

    - **First-type** (per pivot `P`): `E` = the segment immediately before
      `P`'s own window (the entering move); `L` = the first segment after
      `P`'s window clearing the band in one direction (buy: down, below
      `zd`; sell: up, above `zg`). Requires `E`/`L` share direction, `L`
      reaches further than `E` in that direction, AND `macd_divergence(E,
      L)` confirms divergence. Point = `L`'s own end.
    - **Second-type**: given a confirmed first-type point at segment index
      `ell` (for `L`), `N` = `segments[ell + 2]` (the next same-direction-
      as-`L` move, after one bounce). Fires iff `N` does NOT exceed `L`'s
      own extreme (buy: `N.end_price > L.end_price`; sell: mirrored). Point
      = `N`'s own end.
    - **Third-type** (per pivot `P`, no divergence check -- purely
      structural): `B` = the first segment after `P`'s window clearing the
      band in one direction (buy: up, above `zg`; sell: down, below `zd`);
      `R` = the very next segment (opposite direction by alternation).
      Fires iff `R` does NOT re-enter the band (buy: `R.end_price > zg`
      strictly; sell: mirrored). Point = `R`'s own end.

    Disclosed guard: a pivot that already produced a first-type point of a
    given polarity (buy/sell) is skipped for THAT SAME polarity's
    third-type check -- this is what keeps the theoretical invariant that a
    first-type and a later same-polarity third-type point are always
    separated by at least one pivot (real theory: price must form an
    entirely new pivot between an exhaustion point and a breakout-retest
    continuation point of the same polarity).

    Returns one row per detected point: `pos` (merged-bar position, same
    space as `segments`/`pivots`), `price`, `kind` (one of `"first_buy"`,
    `"first_sell"`, `"second_buy"`, `"second_sell"`, `"third_buy"`,
    `"third_sell"`), `pivot_idx` (integer row-position into `pivots`).
    """
    if len(pivots) == 0 or len(segments) == 0:
        return pd.DataFrame(columns=_POINT_COLUMNS)

    def _first_after(start_idx: int, direction: str, price_ok) -> int | None:
        for si in range(start_idx, len(segments)):
            seg = segments.iloc[si]
            if seg["direction"] == direction and price_ok(seg["end_price"]):
                return si
        return None

    rows = []
    first_type_by_pivot: dict[int, list[tuple[str, int]]] = {}

    for p in range(len(pivots)):
        P = pivots.iloc[p]
        start_idx = int(P["start_stroke_idx"])  # indexes into `segments`, see module docstring
        end_idx = int(P["end_stroke_idx"])
        zg, zd = float(P["zg"]), float(P["zd"])
        pivot_first_kinds = set()

        if start_idx > 0:
            E = segments.iloc[start_idx - 1]

            L_idx = _first_after(end_idx + 1, "down", lambda price: price < zd)
            if L_idx is not None:
                L = segments.iloc[L_idx]
                if E["direction"] == "down" and L["end_price"] < E["end_price"]:
                    has_div, _, _ = macd_divergence(E, L, hist, merged)
                    if has_div:
                        rows.append({"pos": int(L["end_pos"]), "price": float(L["end_price"]), "kind": "first_buy", "pivot_idx": p})
                        first_type_by_pivot.setdefault(p, []).append(("first_buy", L_idx))
                        pivot_first_kinds.add("buy")

            L_idx_s = _first_after(end_idx + 1, "up", lambda price: price > zg)
            if L_idx_s is not None:
                L = segments.iloc[L_idx_s]
                if E["direction"] == "up" and L["end_price"] > E["end_price"]:
                    has_div, _, _ = macd_divergence(E, L, hist, merged)
                    if has_div:
                        rows.append({"pos": int(L["end_pos"]), "price": float(L["end_price"]), "kind": "first_sell", "pivot_idx": p})
                        first_type_by_pivot.setdefault(p, []).append(("first_sell", L_idx_s))
                        pivot_first_kinds.add("sell")

        if "buy" not in pivot_first_kinds:
            B_idx = _first_after(end_idx + 1, "up", lambda price: price > zg)
            if B_idx is not None and B_idx + 1 < len(segments):
                R = segments.iloc[B_idx + 1]
                if R["direction"] == "down" and R["end_price"] > zg:
                    rows.append({"pos": int(R["end_pos"]), "price": float(R["end_price"]), "kind": "third_buy", "pivot_idx": p})

        if "sell" not in pivot_first_kinds:
            B_idx_s = _first_after(end_idx + 1, "down", lambda price: price < zd)
            if B_idx_s is not None and B_idx_s + 1 < len(segments):
                R = segments.iloc[B_idx_s + 1]
                if R["direction"] == "up" and R["end_price"] < zd:
                    rows.append({"pos": int(R["end_pos"]), "price": float(R["end_price"]), "kind": "third_sell", "pivot_idx": p})

    for p, entries in first_type_by_pivot.items():
        for kind, ell in entries:
            if ell + 2 >= len(segments):
                continue
            L = segments.iloc[ell]
            N = segments.iloc[ell + 2]
            if kind == "first_buy" and N["end_price"] > L["end_price"]:
                rows.append({"pos": int(N["end_pos"]), "price": float(N["end_price"]), "kind": "second_buy", "pivot_idx": p})
            elif kind == "first_sell" and N["end_price"] < L["end_price"]:
                rows.append({"pos": int(N["end_pos"]), "price": float(N["end_price"]), "kind": "second_sell", "pivot_idx": p})

    if not rows:
        return pd.DataFrame(columns=_POINT_COLUMNS)
    result = pd.DataFrame(rows, columns=_POINT_COLUMNS)
    return result.sort_values("pos", kind="stable").reset_index(drop=True)


def compute_chan3_signals(
    df: pd.DataFrame,
    min_gap_bars: int = 4,
    min_strokes: int = 3,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
) -> pd.DataFrame:
    """Derives per-bar boolean signals for all three formal buy/sell-point
    types (aligned to `df.index`), mirroring `compute_chan_signals`'s own
    shape (single public entry point over the same merge/fractal/stroke
    primitives), but extending the pipeline with segments and segment-level
    pivots (see module docstring) and real MACD divergence.

    Returns a DataFrame with boolean columns `first_buy`, `first_sell`,
    `second_buy`, `second_sell`, `third_buy`, `third_sell`, plus convenience
    `buy_signal`/`sell_signal` (OR of the three same-polarity columns) --
    drop-in wherever `compute_chan_signals`'s `buy_signal`/`sell_signal`
    frame is consumed today.
    """
    merged = merge_inclusion(df)
    fractals = find_fractals(merged)
    strokes = build_strokes(fractals, min_gap_bars)
    segments = build_segments(strokes, min_strokes)
    pivots = build_pivots(segments, min_strokes)
    hist = macd(df["Close"], macd_fast, macd_slow, macd_signal)["hist"].reset_index(drop=True)
    points = classify_points(segments, pivots, hist, merged)

    point_kinds = ["first_buy", "first_sell", "second_buy", "second_sell", "third_buy", "third_sell"]
    out = {kind: pd.Series(False, index=df.index) for kind in point_kinds}
    for _, row in points.iterrows():
        confirm_pos = int(row["pos"]) + 1
        if confirm_pos < len(merged):
            out[row["kind"]].loc[merged.index[confirm_pos]] = True

    result = pd.DataFrame(out)
    result["buy_signal"] = result["first_buy"] | result["second_buy"] | result["third_buy"]
    result["sell_signal"] = result["first_sell"] | result["second_sell"] | result["third_sell"]
    return result
