"""R3 -- the offline synthetic 5m panel, and the frozen spec it is built against.

Why this module exists (history, load-bearing): `docs/development_plan.md` `R3` (§2.20) registers
that the repo has **no controllable synthetic 5m panel** at all. `common/testing.py` stops at daily
granularity (grep `intraday` / `5m` / `minute` -> zero hits), every existing 5m test either drives
the real cache or imports a calibration library that lives under the gitignored
`backtests_research/`, and that gap is what blocks `R1` / `R4` / `R6` / `R7`. This module is the
tracked, 100%-offline answer: it builds a panel out of nothing but `numpy` + `pandas`, and it
*reports* the three defects the other four tasks must be able to trip on -- timestamps, timezone,
coverage -- instead of hiding them.

FROZEN SPEC (doc `R3` item 1 -- every one of the other four tasks quotes THIS table, none of them
gets its own):

  * ``SESSION_BARS_PER_DAY = 48``.
    The A-share continuous auction is 09:30-11:30 plus 13:00-15:00 = 240 minutes = 48 closed
    five-minute intervals. **48, not 49**: the endpoints are *half-open* -- 09:30 belongs to the
    first bar as its opening instant, 11:30 belongs to the last morning bar as its closing
    instant, and 13:00/15:00 replay that split in the afternoon. Counting both endpoints (an
    inclusive-inclusive grid) yields 49 labels and an off-by-one that the M3.3 gate explicitly
    rejects (`expected_bars_per_day`).
  * ``BAR_TIMESTAMP_CONVENTION = "END"`` -- a label ``t`` covers ``(t-5m, t]``.
    This is BaoStock's convention and the one `common/intraday_bar_contract.py` pins
    (``EXPECTED_CONVENTION = "END"``, ``CONTRACT_VERSION = "m3.3.1"``, `_grid_labels(True)`). It is
    frozen here **by construction**: the label grid starts at 09:35 (the close of the first bar),
    not at 09:30 (its open). A START-labelled feed is a different contract, and the gate fails it
    rather than re-pinning -- so nothing here may ever emit a START grid.
  * ``GRID_LABELS`` == 09:35, 09:40, ... 11:30, then 13:05, 13:10, ... 15:00 (exactly 48 labels;
    `test_frozen_spec_constants_are_exact` compares this tuple element-for-element against
    `common.intraday_bar_contract.END_LABELS`, so the two files cannot drift apart).
  * ``LUNCH_BREAK_START = "11:30"`` / ``LUNCH_BREAK_END = "13:00"`` -- the lunch break carries
    **no bars**. 11:30 is a legitimate END label (it closes 11:25-11:30); everything strictly
    between 11:30 and 13:00 is a defect. That strict inequality is copied from the gate
    (`check_shape`), not re-derived, so the two agree on the boundary.
  * ``SESSION_TIMEZONE = "Asia/Shanghai"``, ``EXPECTED_UTC_OFFSET_MINUTES = 480``.
    China has observed **no DST since 1991**, so Asia/Shanghai is a constant +08:00 with no
    ambiguous or non-existent local times. That is why the timezone check here is an exact
    UTC-offset equality against +08:00 -- no ``ambiguous=`` / ``nonexistent=`` handling is needed,
    and a "did you sort around the DST fold" bug is structurally impossible in this market.
  * 停牌 (``suspended``) vs 缺失 (``missing``) -- frozen exactly as the M3.3 gate classifies them:
      - ``suspended``: the *daily* row exists for (symbol, day) but its ``volume`` is NaN or 0.0,
        i.e. nothing traded. **No 5m bars are expected**; this is not a defect and contributes 0
        to ``missing``.
      - ``missing``: the day traded (daily ``volume`` > 0, or bars exist) but the 5m feed does not
        carry ``expected_bars`` *distinct on-grid* bars. Counted in **bars**, per session.
      - no daily row at all -> ``unknown_no_daily_bar``. "We cannot tell" is never laundered into
        "it was halted" (the gate's own honest caveat, reused verbatim).

WHAT THIS MODULE DELIBERATELY DOES NOT DO: no production signal, no factor, no ``amount``
reconstruction via ``volume * close`` (the M3.3 item-7 forbidden path -- the generated ``amount``
is ``volume * (high+low+close)/3``, so it reconciles against VWAP without ever being a rebuild of
close), no cache, no network, no import of anything under ``backtests_research/``.

HONEST CAVEATS (disclosed rather than buried):
  * The classifier's three axes are kept **orthogonal on purpose**. Coverage and grid shape are
    measured on each bar's **labelled wall clock** (the label the feed wrote), while the zone
    defect is reported on its own axis. A +09:00-labelled feed therefore shows up as
    ``tz_mismatch`` and *not* additionally as ``missing``: one defect, one number. Smeared
    counts are what makes a bug report unreadable.
  * The zone check compares the **UTC offset**, not the zone *name*. Two zone names with the same
    offset describe the same instant, so refusing on the name alone would be a false alarm; the
    per-row zone name is still reported in ``details["tz_mismatch_rows"]`` for a human.
  * A day with **zero** bars and **no** daily row is invisible to the classifier -- iterating the
    union of the two tables is the only honest way to enumerate sessions, and "no info at all" has
    no representation. Supply ``daily`` (the regime of record) whenever whole-day gaps matter; that
    is the same limit that forces the gate's ``unknown_no_daily_bar`` bucket.
  * The synthetic trading calendar is ``pd.bdate_range`` (weekdays only). It is *not* the real
    exchange calendar -- the panel is synthetic, so any day it contains is by construction a
    trading day for the panel. Injecting holidays is out of scope for `R3`.
  * Timestamp normalisation falls back to a per-row ``pd.Timestamp`` loop when the column is
    ``object`` dtype (which is what a mixed-zone feed produces). That path is O(n) in Python; for
    the panel sizes `R3` needs (hundreds to a few thousand rows) it is not worth vectorising
    around the dtype instability.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------------------------
# frozen spec -- module-level constants, quoted by R1 / R4 / R6 / R7
# ---------------------------------------------------------------------------------------------
# Spec revision of THIS module's contract. Deliberately *not* part of the strategy version axis
# (no BASE_VERSION, no per-class `version`); it exists so a downstream assertion can say "I was
# written against panel spec r3.1" without pretending to be a strategy version.
PANEL_SPEC_VERSION = "r3.1"

SESSION_TIMEZONE = "Asia/Shanghai"
EXPECTED_UTC_OFFSET_MINUTES = 480          # +08:00, constant -- China has had no DST since 1991
BAR_MINUTES = 5
SESSION_BARS_PER_DAY = 48
BAR_TIMESTAMP_CONVENTION = "END"           # label t covers (t-5m, t]

MORNING_OPEN = "09:30"
MORNING_CLOSE = "11:30"
AFTERNOON_OPEN = "13:00"
SESSION_CLOSE = "15:00"
LUNCH_BREAK_START = "11:30"
LUNCH_BREAK_END = "13:00"

# The two session blocks, as (first bar's open, last bar's close). Closed on the right only.
SESSION_BLOCKS: tuple[tuple[str, str], ...] = ((MORNING_OPEN, MORNING_CLOSE), (AFTERNOON_OPEN, SESSION_CLOSE))


def _grid_labels(end_labelled: bool) -> list[str]:
    """48 A-share 5m labels for the given convention (END by default).

    Same derivation as `common/intraday_bar_contract._grid_labels`; duplicated rather than
    imported so this module stays dependency-free, and pinned equal to the contract's tuple by
    `test_frozen_spec_constants_are_exact`. END: 09:35..11:30 / 13:05..15:00.
    START would be 09:30..11:25 / 13:00..14:55.
    """
    labels: list[str] = []
    for block_open, block_close in SESSION_BLOCKS:
        t = pd.Timestamp(f"2000-01-01 {block_open}")
        end = pd.Timestamp(f"2000-01-01 {block_close}")
        while t < end:
            labels.append((t + pd.Timedelta(minutes=BAR_MINUTES) if end_labelled else t).strftime("%H:%M"))
            t += pd.Timedelta(minutes=BAR_MINUTES)
    return labels


GRID_LABELS: tuple[str, ...] = tuple(_grid_labels(True))
assert len(GRID_LABELS) == SESSION_BARS_PER_DAY, "frozen grid must be 48 END labels"
assert GRID_LABELS[0] == "09:35" and GRID_LABELS[-1] == "15:00"

# Session-level classifications, in the precedence order `classify_5m_panel` applies them.
CLASS_OK = "ok"
CLASS_MISSING = "missing"                  # 缺失
CLASS_SUSPENDED = "suspended"              # 停牌
CLASS_UNKNOWN = "unknown_no_daily_bar"     # cannot tell -> never counted as a halt
CLASS_TZ_MISMATCH = "tz_mismatch"
CLASS_DUPLICATE = "duplicate"
CLASS_OFF_GRID = "off_grid"

# Classification rules, echoed into every diagnostics dict so a caller can print *why* a number is
# what it is without reading this file.
FROZEN_RULES: dict[str, str] = {
    "bars_per_day": f"{SESSION_BARS_PER_DAY} END-labelled 5m bars/日 = (11:30-09:30 + 15:00-13:00)/5m",
    "timestamp_convention": "END: label t covers (t-5m, t]; first label 09:35, last 15:00",
    "lunch_break": f"{LUNCH_BREAK_START}-{LUNCH_BREAK_END} carries no bars; 11:30 is a valid END "
                   f"label, so 'inside lunch' is the STRICT interior (>11:30 and <13:00)",
    "timezone": f"{SESSION_TIMEZONE} (+{EXPECTED_UTC_OFFSET_MINUTES // 60:02d}:00, no DST since "
                f"1991); a naive or off-offset timestamp is a tz_mismatch, never silently assumed",
    "suspended": "daily row exists AND daily volume is NaN or 0 -> 停牌, expects 0 bars, "
                 "contributes 0 to missing",
    "missing": "session traded (daily volume > 0 or bars present) but distinct on-grid bars != 48 "
               "-> 缺失, counted in BARS",
    "unknown_no_daily_bar": "no daily row and no bars -> cannot tell; never merged into suspended",
    "duplicate": "same (symbol, wall-clock ts) appears more than once; counted as EXTRA copies, "
                 "rows are never dropped",
    "missing_counts": "missing is a BAR count, duplicate/tz_mismatch/lunch/off_grid are BAR counts, "
                      "the *_sessions keys are SESSION counts",
}

BARS_REQUIRED_COLUMNS = ("symbol", "ts", "open", "high", "low", "close", "volume", "amount")
DAILY_REQUIRED_COLUMNS = ("symbol", "date", "open", "high", "low", "close", "volume", "amount")

# Deterministic allocation order for the injection slots. Fixed so that "2 missing days" always
# names the same (symbol, day) pairs across runs -- a test that pins a number must also be able to
# pin *which* rows it is talking about.
_INJECTION_ORDER = ("suspended", "missing", "cross_lunch", "tz_offset", "duplicate")


class PanelSpecError(ValueError):
    """Raised when the panel (or the request) cannot satisfy the frozen spec.

    Explicitly NOT raised for data defects: those are *counted*. It is raised for things that make
    the classification meaningless (unparseable timestamp, missing column, a grid request wider
    than the 48-slot session, more injections than there are sessions), because a silent fallback
    there would be the exact "looks fine, is wrong" failure this task exists to prevent.
    """


def panel_spec() -> dict:
    """The frozen spec as a plain dict (safe to embed in JSON/report artifacts).

    Downstream tasks assert against these values instead of re-declaring 48 / "END" / "+08:00"
    locally; that is the whole point of freezing it in one place.
    """
    return {
        "spec_version": PANEL_SPEC_VERSION,
        "bars_per_day": SESSION_BARS_PER_DAY,
        "bar_minutes": BAR_MINUTES,
        "timestamp_convention": BAR_TIMESTAMP_CONVENTION,
        "grid_labels": list(GRID_LABELS),
        "session_blocks": [list(b) for b in SESSION_BLOCKS],
        "lunch_break": [LUNCH_BREAK_START, LUNCH_BREAK_END],
        "timezone": SESSION_TIMEZONE,
        "utc_offset_minutes": EXPECTED_UTC_OFFSET_MINUTES,
        "has_dst": False,
        "rules": dict(FROZEN_RULES),
        "bar_columns": list(BARS_REQUIRED_COLUMNS),
        "daily_columns": list(DAILY_REQUIRED_COLUMNS),
    }


# ---------------------------------------------------------------------------------------------
# timestamp normalisation -- tz-aware, tz-naive and mixed-offset columns all accepted
# ---------------------------------------------------------------------------------------------
def _offset_minutes(value: pd.Timestamp) -> int | None:
    """UTC offset of one timestamp in whole minutes, or None when the timestamp is naive."""
    off = value.utcoffset()
    if off is None:
        return None
    return int(round(off / pd.Timedelta(minutes=1)))


def _parse_offset_str(text: str) -> int:
    """Parse a ``%z`` field ("+0800", "-0530") into minutes.

    Straight ``int("+0900") / 60`` is wrong (it reads the digits as a decimal number), which is
    exactly the kind of quiet sign/scale error this module exists to surface -- so the parse is
    explicit and signed.
    """
    sign = -1 if text.startswith("-") else 1
    digits = text.lstrip("+-")
    if len(digits) != 4 or not digits.isdigit():
        raise PanelSpecError(f"cannot parse UTC offset {text!r}")
    return sign * (int(digits[:2]) * 60 + int(digits[2:]))


def _ts_frame(values: pd.Series) -> pd.DataFrame:
    """Split a timestamp column into (labelled wall clock, utc offset, zone label) per row.

    The wall clock is the label *as written by the feed* (tz dropped, clock kept) -- see the
    module docstring's "orthogonal axes" caveat. A mixed-offset column reaches pandas as ``object``
    dtype, hence the per-row fallback; anything unparseable raises instead of becoming NaT,
    because a NaT would silently drop out of every count below.
    """
    if values.isna().any():
        raise PanelSpecError(
            "bar timestamp column contains NaT before parsing; a NaT would silently disappear "
            "from every missing/duplicate/tz count, so the panel is refused instead"
        )
    dtype = values.dtype
    n = len(values)
    if isinstance(dtype, pd.DatetimeTZDtype):
        wall = values.dt.tz_localize(None)
        offsets = [_parse_offset_str(v) for v in values.dt.strftime("%z")]
        zones = [str(dtype.tz)] * n
    elif pd.api.types.is_datetime64_any_dtype(dtype):
        wall = values.copy()
        offsets = [None] * n
        zones = [None] * n
    else:
        wall_parts: list[pd.Timestamp] = []
        offsets = []
        zones = []
        for raw in values.tolist():
            try:
                ts = pd.Timestamp(raw)
            except (TypeError, ValueError) as exc:
                raise PanelSpecError(
                    f"unparseable bar timestamp {raw!r}: a NaT here would silently disappear from "
                    f"every missing/duplicate/tz count, so the panel is refused instead"
                ) from exc
            if pd.isna(ts):
                raise PanelSpecError(f"bar timestamp {raw!r} parsed to NaT (empty/invalid)")
            wall_parts.append(ts.tz_localize(None) if ts.tzinfo is not None else ts)
            offsets.append(_offset_minutes(ts))
            zones.append(str(ts.tz) if ts.tzinfo is not None else None)
        wall = pd.Series(pd.to_datetime(wall_parts, errors="raise"), index=values.index)
        if wall.isna().any():
            raise PanelSpecError("bar timestamp column contains a value that normalised to NaT")

    out = pd.DataFrame({
        "wall": pd.Series(wall, index=values.index),
        # object dtype on purpose: `None` (naive) must stay `None`, and a float NaN here would
        # turn "no zone at all" into "an offset of NaN minutes" -- a silent coercion.
        "offset_minutes": pd.Series(offsets, index=values.index, dtype=object),
        "tz_label": pd.Series(zones, index=values.index, dtype=object),
    }, index=values.index)
    if out["wall"].isna().any():
        raise PanelSpecError("bar timestamp column contains NaT; refusing to classify a panel "
                             "whose coverage cannot be counted")
    return out


# ---------------------------------------------------------------------------------------------
# the classifier -- the reusable half (R1 / R4 / R6 / R7 import this)
# ---------------------------------------------------------------------------------------------
def _validate_timezone(timezone: str) -> None:
    """Assert that `timezone` IS the frozen zone (+08:00 in winter *and* summer).

    The parameter exists so a caller can state what it means, not so it can redefine the spec: a
    zone with a different offset (or one that shifts in summer, i.e. has DST) would silently
    change what "09:35" points at. Refusing beats re-interpreting. An unavailable `zoneinfo`
    database surfaces here as a `PanelSpecError` rather than being smoothed over -- the task's
    standing rule is "report, do not silently fall back".
    """
    for stamp in ("2024-01-15 09:35", "2024-07-15 09:35"):
        try:
            off = _offset_minutes(pd.Timestamp(stamp, tz=timezone))
        except Exception as exc:
            raise PanelSpecError(
                f"timezone={timezone!r} is not usable (missing zoneinfo data?): {exc}"
            ) from exc
        if off != EXPECTED_UTC_OFFSET_MINUTES:
            raise PanelSpecError(
                f"timezone={timezone!r} has UTC offset {off} minutes at {stamp}, but the frozen "
                f"panel spec is {SESSION_TIMEZONE} (+{EXPECTED_UTC_OFFSET_MINUTES} minutes, no DST); "
                f"a different zone would change which instant '09:35' names"
            )


def wall_clock(ts: pd.Series) -> pd.Series:
    """The *labelled* wall clock of a ``ts`` column, as tz-naive datetimes.

    Public because it is the only way to group/sort a panel that carries more than one UTC offset
    (pandas cannot hold mixed offsets in one ``datetime64`` column, so such a column arrives as
    ``object`` and refuses the ``.dt`` accessor). The classifier uses exactly this definition of
    "what the feed says the time is".
    """
    parts = _ts_frame(ts)
    return pd.Series(pd.to_datetime(parts["wall"]), index=ts.index)


def _require_columns(frame: pd.DataFrame, required: tuple[str, ...], name: str) -> None:
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise PanelSpecError(f"{name} is missing required column(s) {missing}; got {list(frame.columns)}")


def classify_5m_panel(bars: pd.DataFrame, *, daily: pd.DataFrame | None = None,
                      expected_bars: int = SESSION_BARS_PER_DAY,
                      timezone: str = SESSION_TIMEZONE,
                      assume_naive_tz: str | None = None,
                      grid: tuple[str, ...] | list[str] | None = None) -> dict:
    """Classify an arbitrary 5m frame into ``missing`` / ``duplicate`` / ``tz_mismatch`` counts.

    Nothing is dropped, deduplicated or filtered: every input row is either clean or visible in at
    least one detail frame. The axes are not exclusive -- a bar that is both inside the lunch break
    and stamped +09:00 appears in ``lunch_rows`` and in ``tz_mismatch_rows``, because collapsing
    those into one "bad row" bucket would hide which guard has to fire. ``missing`` is a *count of
    absent bars*, so it is reported per (symbol, day) rather than per row -- you cannot keep a row
    that was never there, which is precisely why the diagnostic has to say how many are gone.

    Counts (all ints, at the top level of the return value):
      ``bars``          rows in the input frame (after nothing at all)
      ``missing``       bars absent = sum over sessions of max(0, expected - distinct on-grid bars)
      ``duplicate``     EXTRA copies of an already-seen (symbol, wall-clock) key
      ``tz_mismatch``   bars whose timestamp is naive or whose UTC offset != the frozen +08:00
      ``lunch``         bars inside the strict lunch interior (LUNCH_BREAK_START, LUNCH_BREAK_END)
      ``pre_session``   bars labelled before the first grid label (09:35)
      ``post_session``  bars labelled after the last grid label (15:00)
      ``off_grid``      bars on none of the above but still not on the 48-label grid
      ``suspended``     sessions with zero bars whose daily row shows volume NaN/0
      ``unknown``       sessions with zero bars and no daily evidence either way
      plus ``sessions`` and the three ``*_sessions`` counters.

    ``assume_naive_tz`` localises naive timestamps into that zone (documented opt-in). Default
    ``None`` means naive timestamps are a ``tz_mismatch``: a naive label is an ambiguous instant and
    pretending otherwise is how an off-by-one-hour feed passes review.

    ``timezone`` / ``expected_bars`` / ``grid`` are *declarations*, not knobs: ``timezone`` must be
    the frozen +08:00-no-DST zone (anything else raises, see `_validate_timezone`),
    ``expected_bars`` is 48 unless a caller is deliberately measuring another contract, and
    ``grid`` exists so a caller that has already established its own label set can hand it over
    instead of re-deriving it.

    Session ``classification`` precedence (first match wins): zero bars -> suspended / missing /
    unknown; then tz_mismatch; then duplicate; then off_grid (lunch / pre / post / other); then
    missing; then ok. The *counts* stay independent of that precedence -- a session with both a
    duplicate and a deficit contributes to both numbers.

    DISCLOSED DEVIATION from `common.intraday_bar_contract.check_shape`: that function returns
    ``unknown_no_daily_bar`` for a zero-bar day whose daily row *did* trade (its ``if`` falls
    through). Here such a day is ``missing`` with ``missing_bars == expected_bars``, because "it
    traded and we have no bars" is 缺失, not "we cannot tell"; ``unknown`` is reserved for the
    genuinely evidence-free case (no daily row at all). Nothing is loosened -- both are counted, and
    neither can ever masquerade as 停牌.
    """
    if grid is None:
        grid = GRID_LABELS
    grid = tuple(str(g) for g in grid)
    _validate_timezone(timezone)
    _require_columns(bars, ("symbol", "ts"), "bars")
    detail_cols = ["symbol", "ts", "date", "time", "reason"]

    frame = pd.DataFrame({"symbol": bars["symbol"].to_numpy()}, index=bars.index)
    ts_parts = _ts_frame(bars["ts"])
    offsets = ts_parts["offset_minutes"].tolist()
    wall = pd.Series(pd.to_datetime(ts_parts["wall"]), index=bars.index)

    frame["ts"] = bars["ts"].to_numpy()
    frame["wall"] = wall
    frame["date"] = wall.dt.date
    frame["time"] = wall.dt.strftime("%H:%M")
    frame["offset"] = offsets

    # --- tz axis: offset equality, not zone-name equality (see module caveats) ---------------
    tz_bad: list[str] = []
    for off in offsets:
        if off is None:
            tz_bad.append("naive_timestamp" if assume_naive_tz is None else "")
        elif off != EXPECTED_UTC_OFFSET_MINUTES:
            sign = "-" if off < 0 else "+"
            tz_bad.append(f"utc_offset_{sign}{abs(off) // 60:02d}:{abs(off) % 60:02d}")
        else:
            tz_bad.append("")
    frame["tz_reason"] = tz_bad
    frame["tz_bad"] = frame["tz_reason"] != ""
    if assume_naive_tz is not None:
        # Opt-in only: the caller asserted the frame's zone explicitly, so the wall clock stands.
        try:
            pd.Timestamp("2000-01-01 09:35", tz=assume_naive_tz)
        except Exception as exc:  # zoneinfo data missing / bad name
            raise PanelSpecError(
                f"assume_naive_tz={assume_naive_tz!r} is not a usable timezone: {exc}"
            ) from exc

    # --- grid axis: orthogonal to the tz axis by design (wall clock, not the instant) --------
    on_grid = frame["time"].isin(set(grid))
    frame["on_grid"] = on_grid
    frame["lunch"] = (frame["time"] > LUNCH_BREAK_START) & (frame["time"] < LUNCH_BREAK_END)
    frame["pre_session"] = frame["time"] < grid[0]
    frame["post_session"] = frame["time"] > grid[-1]
    frame["off_grid"] = ~on_grid & ~frame["lunch"] & ~frame["pre_session"] & ~frame["post_session"]

    # --- duplicate axis: keyed on (symbol, wall clock), never on the instant ----------------
    key = frame["symbol"].astype(str) + "|" + frame["wall"].astype(str)
    dup_counts = key.groupby(key).transform("size")
    frame["dup_copies"] = dup_counts.astype(int)
    frame["duplicate"] = frame["dup_copies"] > 1

    duplicate = (
        frame.loc[frame["duplicate"], ["symbol", "ts", "date", "time", "dup_copies"]]
        .assign(reason="duplicate_timestamp")
        .sort_values(["symbol", "time"], kind="stable")
        .reset_index(drop=True)
    )
    duplicate = duplicate[detail_cols[:-1] + ["dup_copies", "reason"]]

    tz_mismatch = (
        frame.loc[frame["tz_bad"], ["symbol", "ts", "date", "time", "tz_reason"]]
        .rename(columns={"tz_reason": "reason"})
        .reset_index(drop=True)
    )
    lunch_rows = (
        frame.loc[frame["lunch"], ["symbol", "ts", "date", "time"]]
        .assign(reason="bar_inside_lunch_break").reset_index(drop=True)
    )
    # One row per off-grid bar, with the reason naming which side of the session it fell on --
    # "3 off-grid bars" without "2 of them were before 09:35" is not actionable.
    off_mask = frame["off_grid"] | frame["pre_session"] | frame["post_session"]
    off_grid_rows = (
        frame.loc[off_mask, ["symbol", "ts", "date", "time"]]
        .assign(reason=np.select(
            [frame.loc[off_mask, "pre_session"], frame.loc[off_mask, "post_session"]],
            ["bar_pre_session", "bar_post_session"],
            default="bar_off_grid",
        ))
        .reset_index(drop=True)
    )

    # --- session axis ----------------------------------------------------------------------
    bars_agg = frame.groupby(["symbol", "date"], sort=True).agg(
        n_bars=("time", "size"),
        n_distinct=("wall", "nunique"),
        n_on_grid_rows=("on_grid", "sum"),
        n_off_grid=("off_grid", "sum"),
        n_lunch=("lunch", "sum"),
        n_pre=("pre_session", "sum"),
        n_post=("post_session", "sum"),
        n_tz_bad=("tz_bad", "sum"),
    ).reset_index()
    # Coverage is DISTINCT on-grid bars, not on-grid rows: a duplicated 13:05 is still one slot
    # of the session, so counting rows would understate `missing` by exactly the duplicate count.
    on_grid_distinct = (
        frame.loc[frame["on_grid"], ["symbol", "date", "wall"]]
        .drop_duplicates()
        .groupby(["symbol", "date"], sort=True)
        .size()
        .rename("n_on_grid")
        .reset_index()
    )
    bars_agg = bars_agg.merge(on_grid_distinct, on=["symbol", "date"], how="left")
    bars_agg["n_on_grid"] = pd.to_numeric(bars_agg["n_on_grid"], errors="coerce").fillna(0).astype(int)
    bars_agg["n_dup_extra"] = bars_agg["n_bars"] - bars_agg["n_distinct"]

    if daily is not None:
        _require_columns(daily, ("symbol", "date", "volume"), "daily")
        dv = daily[["symbol", "date", "volume"]].copy()
        dv["daily_volume"] = pd.to_numeric(dv["volume"], errors="coerce")
        dv["has_daily_bar"] = True
        dv = dv.drop(columns=["volume"]).drop_duplicates(subset=["symbol", "date"])
        sessions = bars_agg.merge(dv, on=["symbol", "date"], how="outer")
    else:
        sessions = bars_agg.copy()
        sessions["daily_volume"] = np.nan
        sessions["has_daily_bar"] = False
    for col in ("n_bars", "n_distinct", "n_on_grid", "n_on_grid_rows", "n_off_grid", "n_lunch",
                "n_pre", "n_post", "n_tz_bad", "n_dup_extra"):
        sessions[col] = sessions[col].fillna(0).astype(int)
    sessions["has_daily_bar"] = sessions["has_daily_bar"].fillna(False).astype(bool)
    sessions["daily_volume"] = pd.to_numeric(sessions["daily_volume"], errors="coerce")

    sessions["expected_bars"] = int(expected_bars)
    sessions["observed_bars"] = sessions["n_on_grid"]
    sessions["missing_bars"] = (int(expected_bars) - sessions["n_on_grid"]).clip(lower=0).astype(int)

    def _classify(row) -> str:
        if row["n_bars"] == 0:
            if row["has_daily_bar"]:
                vol = row["daily_volume"]
                if pd.isna(vol) or float(vol) == 0.0:
                    return CLASS_SUSPENDED          # 停牌: nothing traded, no bars expected
                return CLASS_MISSING                # 缺失: it traded, the whole 5m day is gone
            return CLASS_UNKNOWN                # no daily evidence either way -> never a halt
        if row["n_tz_bad"] > 0:
            return CLASS_TZ_MISMATCH
        if row["n_dup_extra"] > 0:
            return CLASS_DUPLICATE
        if row["n_lunch"] or row["n_off_grid"] or row["n_pre"] or row["n_post"]:
            return CLASS_OFF_GRID
        if row["missing_bars"] > 0:
            return CLASS_MISSING                # 缺失: traded, but the 5m feed is short
        return CLASS_OK

    if len(sessions):
        sessions["classification"] = sessions.apply(_classify, axis=1)
    else:
        sessions["classification"] = pd.Series([], dtype=object)
    sessions = sessions.sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)

    suspended = sessions["classification"] == CLASS_SUSPENDED
    # A halt (or an unknowable day) must never be booked as missing bars: 停牌 is not 缺失.
    no_expectation = suspended | (sessions["classification"] == CLASS_UNKNOWN)
    sessions.loc[no_expectation, "missing_bars"] = 0
    missing_days = sessions.loc[sessions["missing_bars"] > 0,
                                ["symbol", "date", "expected_bars", "observed_bars",
                                 "missing_bars"]].reset_index(drop=True)
    suspended_days = sessions.loc[suspended, ["symbol", "date", "daily_volume"]].reset_index(drop=True)

    counts = {
        "bars": int(len(frame)),
        "sessions": int(len(sessions)),
        "missing": int(sessions["missing_bars"].sum()),
        "missing_sessions": int((sessions["missing_bars"] > 0).sum()),
        "duplicate": int(sessions["n_dup_extra"].sum()),
        "duplicate_sessions": int((sessions["n_dup_extra"] > 0).sum()),
        "tz_mismatch": int(frame["tz_bad"].sum()),
        "tz_mismatch_sessions": int((sessions["n_tz_bad"] > 0).sum()),
        "lunch": int(sessions["n_lunch"].sum()),
        "pre_session": int(sessions["n_pre"].sum()),
        "post_session": int(sessions["n_post"].sum()),
        "off_grid": int(sessions["n_off_grid"].sum()),
        "suspended": int(suspended.sum()),
        "unknown": int((sessions["classification"] == CLASS_UNKNOWN).sum()),
        "ok_sessions": int((sessions["classification"] == CLASS_OK).sum()),
    }
    result = dict(counts)
    result.update({
        "expected_bars": int(expected_bars),
        "timezone": timezone,
        "assume_naive_tz": assume_naive_tz,
        "grid_labels": list(grid),
        "has_defects": bool(counts["missing"] or counts["duplicate"] or counts["tz_mismatch"]
                            or counts["lunch"] or counts["pre_session"] or counts["post_session"]
                            or counts["off_grid"] or counts["unknown"]),
        "details": {
            "missing_days": missing_days,
            "suspended_days": suspended_days,
            "duplicate_rows": duplicate,
            "tz_mismatch_rows": tz_mismatch,
            "lunch_rows": lunch_rows,
            "off_grid_rows": off_grid_rows,
            "sessions": sessions,
        },
        "rules": dict(FROZEN_RULES),
    })
    return result


# ---------------------------------------------------------------------------------------------
# the generator
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SyntheticPanel:
    """A synthetic 5m panel plus the diagnostics that prove what was injected into it.

    ``bars`` / ``daily`` use the exact schemas `common/intraday_bar_contract` expects
    (``symbol, ts, open, high, low, close, volume, amount`` / ``..., date, ...``), so a test can
    hand them straight to the M3.3 gate or to `common.intraday_features`.
    """

    bars: pd.DataFrame
    daily: pd.DataFrame
    diagnostics: dict = field(default_factory=dict)
    injected: dict = field(default_factory=dict)

    @property
    def counts(self) -> dict:
        """Shorthand for the classified counts (``missing`` / ``duplicate`` / ``tz_mismatch`` ...)."""
        return {k: v for k, v in self.diagnostics.items()
                if isinstance(v, int) and not isinstance(v, bool)}

    @property
    def sessions(self) -> pd.DataFrame:
        return self.diagnostics["details"]["sessions"]


def _symbol_names(symbols: int | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(symbols, int) and not isinstance(symbols, bool):
        if symbols < 1:
            raise PanelSpecError(f"symbols={symbols}: need at least one symbol")
        return [f"SYN{i:04d}.SH" for i in range(1, symbols + 1)]
    names = [str(s) for s in symbols]
    if not names:
        raise PanelSpecError("symbols is empty")
    if len(set(names)) != len(names):
        raise PanelSpecError(f"duplicate symbol names in {names}")
    return names


def build_synthetic_5m_panel(
    *,
    symbols: int | list[str] | tuple[str, ...] = 2,
    sessions: int = 3,
    start: str = "2024-06-03",
    seed: int = 20240603,
    bars_per_day: int = SESSION_BARS_PER_DAY,
    start_price: float = 100.0,
    suspended_days: int = 0,
    missing_days: int = 0,
    missing_bars_per_day: int = 3,
    cross_lunch_days: int = 0,
    cross_lunch_bars: int = 2,
    tz_offset_days: int = 0,
    tz_offset: str = "+09:00",
    duplicate_days: int = 0,
    duplicate_bars_per_day: int = 2,
    timezone: str = SESSION_TIMEZONE,
    assume_naive_tz: str | None = None,
) -> SyntheticPanel:
    """Build a deterministic, 100%-offline synthetic 5m panel with explicit defect injection.

    Every injected defect is allocated a **disjoint** (symbol, session) slot, in the fixed order
    ``_INJECTION_ORDER``, so a caller can predict the exact counts: one injected defect can never
    mask another and the arithmetic stays checkable by hand (``missing_days * missing_bars_per_day``
    bars, ``duplicate_days * duplicate_bars_per_day`` extra rows, ``tz_offset_days * bars_per_day``
    off-zone bars, ``cross_lunch_days * cross_lunch_bars`` lunch bars). Asking for more slots than
    there are sessions raises `PanelSpecError` instead of quietly clamping.

    Injection semantics (frozen):
      ``suspended_days``    no 5m bars at all AND a daily row with ``volume = 0.0`` -> 停牌.
      ``missing_days``      the first ``missing_bars_per_day`` bars of the afternoon are absent;
                            the daily row keeps its real volume -> 缺失.
      ``cross_lunch_days``  ``cross_lunch_bars`` extra bars are stamped inside the lunch break
                            (11:35, 11:40, ...), i.e. the feed kept emitting across the halt. The
                            honest scope of this model: it reproduces "bars inside 11:30-13:00",
                            not "every afternoon label shifted by 5m".
      ``tz_offset_days``    the same wall-clock labels are stamped with ``tz_offset`` (default
                            +09:00) -> ``tz_mismatch``, and *not* additionally ``missing``.
      ``duplicate_days``    ``duplicate_bars_per_day`` bars are emitted twice (rows kept).

    The daily table is aggregated from the **clean** session (the regime of record), so a 5m
    coverage defect never turns into a fake 停牌 -- the deficit stays visible as 缺失.
    """
    names = _symbol_names(symbols)
    if not 1 <= int(bars_per_day) <= SESSION_BARS_PER_DAY:
        raise PanelSpecError(
            f"bars_per_day={bars_per_day}: the A-share 5m session has exactly "
            f"{SESSION_BARS_PER_DAY} slots in this frozen spec (no 49th bar exists)"
        )
    if int(sessions) < 1:
        raise PanelSpecError(f"sessions={sessions}: need at least one trading day")
    _validate_timezone(timezone)
    try:
        pd.Timestamp("2024-01-02 09:35", tz=tz_offset)
    except Exception as exc:
        raise PanelSpecError(f"tz_offset={tz_offset!r} is not a usable offset: {exc}") from exc

    dates = [d.date() for d in pd.bdate_range(start, periods=int(sessions))]
    slots = [(sym, day) for sym in names for day in dates]

    requested = {
        "suspended": int(suspended_days),
        "missing": int(missing_days),
        "cross_lunch": int(cross_lunch_days),
        "tz_offset": int(tz_offset_days),
        "duplicate": int(duplicate_days),
    }
    if any(v < 0 for v in requested.values()):
        raise PanelSpecError(f"injection counts must be >= 0, got {requested}")
    if not 0 <= int(missing_bars_per_day) <= int(bars_per_day):
        raise PanelSpecError(
            f"missing_bars_per_day={missing_bars_per_day} must be within 0..{bars_per_day} "
            f"(a gap wider than the session is not a gap, it is a missing day)"
        )
    if int(duplicate_bars_per_day) < 0 or int(cross_lunch_bars) < 0:
        raise PanelSpecError("duplicate_bars_per_day / cross_lunch_bars must be >= 0")
    if sum(requested.values()) > len(slots):
        raise PanelSpecError(
            f"asked for {sum(requested.values())} defect slots but the panel only has "
            f"{len(slots)} (symbol, session) pairs; injections must stay disjoint so the counts "
            f"remain hand-checkable"
        )
    slot_iter = iter(slots)
    slot_of = {kind: [next(slot_iter) for _ in range(requested[kind])] for kind in _INJECTION_ORDER}
    suspended_set = set(slot_of["suspended"])
    missing_set = set(slot_of["missing"])
    cross_lunch_set = set(slot_of["cross_lunch"])
    tz_set = set(slot_of["tz_offset"])
    duplicate_set = set(slot_of["duplicate"])

    grid = list(GRID_LABELS[:int(bars_per_day)])
    rows: list[dict] = []
    daily_rows: list[dict] = []

    for sym_index, sym in enumerate(names):
        rng = np.random.default_rng([int(seed), sym_index])
        price = float(start_price) * (1.0 + 0.02 * sym_index)
        for day in dates:
            # --- the clean session: prices are generated for every day, defects touch only the
            # --- time/coverage axes, so "injection must not move the price path" is testable.
            session_bars: list[dict] = []
            open_first = price
            high = low = price
            volume_total = 0.0
            amount_total = 0.0
            for label in grid:
                o = price
                ret = float(rng.normal(0.0, 0.0008))
                c = o * (1.0 + ret)
                up = abs(float(rng.normal(0.0, 0.0004)))
                dn = abs(float(rng.normal(0.0, 0.0004)))
                hi = max(o, c) * (1.0 + up)
                lo = min(o, c) * (1.0 - dn)
                vol = float(int(max(100.0, rng.normal(5000.0, 800.0))))
                # amount = volume * (H+L+C)/3: inside [low, high] by construction (so A2 holds)
                # and never a rebuild of `volume * close` (the M3.3 item-7 forbidden path).
                amt = vol * (hi + lo + c) / 3.0
                session_bars.append({
                    "symbol": sym, "ts": pd.Timestamp(f"{day.isoformat()} {label}", tz=timezone),
                    "open": round(o, 4), "high": round(hi, 4), "low": round(lo, 4),
                    "close": round(c, 4), "volume": round(vol, 4), "amount": round(amt, 4),
                })
                high = max(high, hi)
                low = min(low, lo)
                volume_total += vol
                amount_total += amt
                price = c
            close_last = session_bars[-1]["close"]

            if (sym, day) in suspended_set:
                daily_rows.append({
                    "symbol": sym, "date": day,
                    "open": round(open_first, 4), "high": round(high, 4),
                    "low": round(low, 4), "close": round(close_last, 4),
                    # 0.0, not NaN: "真零成交" is what a halt means, and NaN is reserved (M3.3 /
                    # LV5) for "not collected". Both classify as suspended here, but 0.0 is the
                    # honest one.
                    "volume": 0.0, "amount": 0.0,
                })
                continue

            daily_rows.append({
                "symbol": sym, "date": day,
                "open": round(open_first, 4), "high": round(high, 4),
                "low": round(low, 4), "close": round(close_last, 4),
                "volume": round(volume_total, 4), "amount": round(amount_total, 4),
            })

            session_bars = [dict(r) for r in session_bars]
            if (sym, day) in missing_set:
                # Drop from the afternoon on purpose: an intraday 5m gap that only removes the
                # tail would still leave the session's first bar present and hide "no opening
                # print" defects. The daily row above is untouched -> 缺失, not 停牌.
                drop_at = len(session_bars) - int(missing_bars_per_day)
                del session_bars[drop_at:]
            if (sym, day) in cross_lunch_set:
                base = pd.Timestamp(f"{day.isoformat()} {LUNCH_BREAK_START}", tz=timezone)
                ref = session_bars[-1]
                for i in range(1, int(cross_lunch_bars) + 1):
                    extra = dict(ref)
                    extra["ts"] = base + pd.Timedelta(minutes=BAR_MINUTES * i)
                    session_bars.append(extra)
            if (sym, day) in tz_set:
                # Re-label the SAME wall clock into a different zone: the label reads 09:35 but
                # the instant is 08:35 Shanghai. That is what a mis-zoned feed actually looks
                # like, and it is why the classifier reads the offset instead of the label.
                for bar in session_bars:
                    bar["ts"] = bar["ts"].tz_localize(None).tz_localize(tz_offset)
            rows.extend(session_bars)
            if (sym, day) in duplicate_set:
                n_dup = int(duplicate_bars_per_day)
                for bar in session_bars[:n_dup]:
                    rows.append(dict(bar))

    bars_frame = pd.DataFrame(rows, columns=list(BARS_REQUIRED_COLUMNS))
    daily_frame = pd.DataFrame(daily_rows, columns=list(DAILY_REQUIRED_COLUMNS))
    # Order by (symbol, labelled wall clock). Deliberately NOT by the instant: a tz-offset session
    # must keep its labelled order, otherwise the injected defect would also reorder the panel and
    # two defects would be indistinguishable.
    order = pd.DataFrame({
        "symbol": bars_frame["symbol"].to_numpy(),
        "wall": wall_clock(bars_frame["ts"]).to_numpy(),
        "_row": np.arange(len(bars_frame)),
    }).sort_values(["symbol", "wall", "_row"], kind="stable")
    bars_frame = bars_frame.iloc[order["_row"].to_numpy()].reset_index(drop=True)
    bars_frame = bars_frame.astype({c: "float64" for c in ("open", "high", "low", "close",
                                                           "volume", "amount")})

    # expected_bars is the FROZEN SPEC value, never the requested `bars_per_day`: building a
    # 40-bar day does not redefine "a full A-share session", it produces a 缺失 day, and the
    # diagnostics must say so instead of comparing the panel against itself.
    diagnostics = classify_5m_panel(
        bars_frame, daily=daily_frame, expected_bars=SESSION_BARS_PER_DAY, timezone=timezone,
        assume_naive_tz=assume_naive_tz,
    )
    injected = {
        "symbols": names,
        "sessions": int(sessions),
        "start": str(dates[0]),
        "end": str(dates[-1]),
        "seed": int(seed),
        "bars_per_day": int(bars_per_day),
        "suspended_days": requested["suspended"],
        "missing_days": requested["missing"],
        "missing_bars_per_day": int(missing_bars_per_day),
        "cross_lunch_days": requested["cross_lunch"],
        "cross_lunch_bars": int(cross_lunch_bars),
        "tz_offset_days": requested["tz_offset"],
        "tz_offset": tz_offset,
        "duplicate_days": requested["duplicate"],
        "duplicate_bars_per_day": int(duplicate_bars_per_day),
        "slots": {kind: [(s, d.isoformat()) for s, d in pairs] for kind, pairs in slot_of.items()},
        "expected_counts": {
            "missing": requested["missing"] * int(missing_bars_per_day),
            "duplicate": requested["duplicate"] * int(duplicate_bars_per_day),
            "tz_mismatch": requested["tz_offset"] * int(bars_per_day),
            "lunch": requested["cross_lunch"] * int(cross_lunch_bars),
            "suspended_sessions": requested["suspended"],
        },
        "spec": panel_spec(),
    }
    diagnostics["injected"] = injected
    return SyntheticPanel(bars=bars_frame, daily=daily_frame, diagnostics=diagnostics,
                          injected=injected)
