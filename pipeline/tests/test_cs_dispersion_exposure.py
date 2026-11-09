"""Offline regression suite for `R4` -- cross-sectional return **dispersion collapse**.

Unit under test: `common.cs_dispersion_exposure` (read its module docstring and the frozen
judgement `docs/cs_dispersion_exposure.md`, not this file, if the two ever disagree).

WHAT IS PINNED (and what must turn red)
---------------------------------------
1. Normal day -> the multiplier is the literal `1.0` (bit-identical no-op, `R4` item 4①).
2. Light collapse (`0.5*B < D <= 0.75*B`) -> `0.75`; collapse (`D <= 0.5*B`) -> `0.50`, both with a
   visible reason carrying `D`, `B` and the ratio (`R4` item 4②).
3. Boundary equality de-risks at **both** bands (`<=`, not `<`) and one ULP above does not -- pinned
   on exact floats, which compounded price arithmetic cannot express.
4. A single extreme name does **not** fake a collapse (`R4` item 4③): the outlier *raises* `D`, so
   the reading moves away from the collapse band, never into it.
5. `std` over `MAD`: on a cross-section where more than half of the names tie exactly, `MAD` is
   `0.0` (i.e. a fabricated "maximum collapse") while `std` stays at the baseline level. Both
   statistics are computed from the same rows so the discrimination is demonstrated, not asserted.
6. Thin cross-section (`< MIN_CROSS_SECTION_SYMBOLS = 8`) -> `1.0` + `INSUFFICIENT_DATA` **and**
   `dispersion is None` (never `0.0`).
7. Cash proxies (`BIL` / `511880` / `GC001`) never enter the cross-section, even when the panel
   carries them with wildly dispersed prices.
8. A symbol missing **one** bar inside the 21-bar window is dropped and counted, and `D` equals the
   value computed from a panel where that symbol is absent entirely (i.e. nothing was interpolated).
9. A baseline shorter than `baseline_window` is refused (`INSUFFICIENT_DATA`), never silently
   shortened and never read as "not triggered" (mirrors `common/overnight_derisk.py:60-61`).
10. The advisory fence travels on every result (`advisory=True`, `gates_orders=False`), and the
    config refuses inverted bands (a dead threshold is not a tighter one).

100% offline: synthetic frames built from `common.intraday_synthetic.GRID_LABELS` only. No network,
no cache, no real market data, no `data/runs/**`, no `backtests_research/`.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import sys

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.cs_dispersion_exposure import (  # noqa: E402
    CASH_PROXY_SYMBOLS,
    DEFAULT_BASELINE_WINDOW,
    FULL_COLLAPSE_RATIO,
    LIGHT_COLLAPSE_RATIO,
    MIN_CROSS_SECTION_SYMBOLS,
    MULTIPLIER_FULL,
    MULTIPLIER_LIGHT,
    MULTIPLIER_UNCHANGED,
    RETURN_LOOKBACK_BARS,
    CsdConfig,
    classify_dispersion,
    evaluate_exposure,
    exclude_cash_proxies,
    session_close_matrix,
    window_returns,
)
from common.intraday_synthetic import GRID_LABELS, SESSION_BARS_PER_DAY  # noqa: E402

#: A fixed, mean-zero, unit-ish scale design vector: symbol `i`'s per-bar log return is
#: `0.0005 * k * Z[i]`, so after the 20-bar window each name's return is `exp(0.01*k*Z[i]) - 1`
#: and the cross-sectional dispersion is `~0.0101 * k`. A prescribed `k` is therefore a prescribed
#: `D/B` ratio, which is what makes "light" / "full" collapse constructible without tuning.
Z10 = (-1.5, -1.17, -0.83, -0.5, -0.17, 0.17, 0.5, 0.83, 1.17, 1.5)

DEFAULT_SYMBOLS = tuple(f"EQ{i:02d}.SZ" for i in range(1, 11))
START = "2024-06-03"


def _panel(sessions, *, symbols=DEFAULT_SYMBOLS, start: str = START) -> pd.DataFrame:
    """Build a 5m panel whose per-session cross-sectional dispersion is prescribed.

    `sessions` entry semantics: a float `k` uses `Z10` (dispersion ~ `0.0101 * k`); a
    `(k, z_vector)` tuple uses the given design vector (used by the MAD-degeneracy case, where the
    tie structure of the cross-section is the whole point).
    """
    symbols = list(symbols)
    dates = [d.date() for d in pd.bdate_range(start, periods=len(sessions))]
    closes = {sym: 100.0 + 5.0 * i for i, sym in enumerate(symbols)}
    rows = []
    for day, spec in zip(dates, sessions):
        scale, z = (spec, Z10[:len(symbols)]) if isinstance(spec, (int, float)) else spec
        for label in GRID_LABELS:
            for i, sym in enumerate(symbols):
                closes[sym] *= math.exp(0.0005 * float(scale) * float(z[i]))
                rows.append({
                    "symbol": sym,
                    "ts": pd.Timestamp(f"{day.isoformat()} {label}", tz="Asia/Shanghai"),
                    "close": closes[sym],
                })
    return pd.DataFrame(rows)


def _sessions(n: int) -> list:
    """Session dates for an `n`-session panel starting at `START`."""
    return [d.date() for d in pd.bdate_range(START, periods=n)]


BASELINE_ONLY = [1.0] * DEFAULT_BASELINE_WINDOW          # 20 quiet sessions -> a flat, known B


# --------------------------------------------------------------------------- #
# 1. normal day: the multiplier is bit-identical 1.0
# --------------------------------------------------------------------------- #
def test_normal_day_multiplier_is_exactly_one():
    dates = _sessions(DEFAULT_BASELINE_WINDOW + 1)
    decision = evaluate_exposure(_panel(BASELINE_ONLY + [1.30]), dates[-1])

    assert decision.status == "NORMAL"
    # The literal constant, not `0.9999...`: "no collapse" must equal not calling the module.
    assert decision.multiplier == MULTIPLIER_UNCHANGED == 1.0
    assert decision.ratio > LIGHT_COLLAPSE_RATIO
    assert decision.dispersion > 0.0 and decision.baseline > 0.0
    assert decision.advisory is True and decision.gates_orders is False


# --------------------------------------------------------------------------- #
# 2. the two collapse bands, each with a visible reason
# --------------------------------------------------------------------------- #
def test_light_collapse_gives_three_quarter_exposure():
    dates = _sessions(DEFAULT_BASELINE_WINDOW + 1)
    decision = evaluate_exposure(_panel(BASELINE_ONLY + [0.70]), dates[-1])

    assert decision.status == "LIGHT_COLLAPSE"
    assert decision.multiplier == MULTIPLIER_LIGHT == 0.75
    # 0.70 sits strictly inside `(0.50, 0.75]`, so the band is discriminated from both neighbours.
    assert FULL_COLLAPSE_RATIO < decision.ratio <= LIGHT_COLLAPSE_RATIO
    assert "D/B" in decision.reason and "x0.75" in decision.reason


def test_collapse_gives_half_exposure_with_visible_reason():
    dates = _sessions(DEFAULT_BASELINE_WINDOW + 1)
    decision = evaluate_exposure(_panel(BASELINE_ONLY + [0.30]), dates[-1])

    assert decision.status == "COLLAPSE"
    assert decision.multiplier == MULTIPLIER_FULL == 0.50
    assert decision.ratio <= FULL_COLLAPSE_RATIO
    assert "collapsed" in decision.reason and "x0.50" in decision.reason


# --------------------------------------------------------------------------- #
# 3. boundaries: `<=` de-risks at BOTH bands, one ULP above does not
# --------------------------------------------------------------------------- #
def test_boundary_equality_derisks_at_both_bands():
    baseline = 0.0104

    # Exactly at `0.75 * B` -> light collapse (the `<=` convention).
    status, multiplier, _ = classify_dispersion(LIGHT_COLLAPSE_RATIO * baseline, baseline)
    assert (status, multiplier) == ("LIGHT_COLLAPSE", MULTIPLIER_LIGHT)

    # Exactly at `0.5 * B` -> full collapse, i.e. the equality belongs to the LOWER (deeper) band:
    # if the full band were exclusive, `0.5*B` would silently fall into the light band.
    status, multiplier, _ = classify_dispersion(FULL_COLLAPSE_RATIO * baseline, baseline)
    assert (status, multiplier) == ("COLLAPSE", MULTIPLIER_FULL)

    # One ULP above each threshold restores the next-lighter band.
    above_light = math.nextafter(LIGHT_COLLAPSE_RATIO * baseline, math.inf)
    status, multiplier, _ = classify_dispersion(above_light, baseline)
    assert (status, multiplier) == ("NORMAL", MULTIPLIER_UNCHANGED)

    above_full = math.nextafter(FULL_COLLAPSE_RATIO * baseline, math.inf)
    status, multiplier, _ = classify_dispersion(above_full, baseline)
    assert (status, multiplier) == ("LIGHT_COLLAPSE", MULTIPLIER_LIGHT)


# --------------------------------------------------------------------------- #
# 4. a single extreme name must not fake a collapse
# --------------------------------------------------------------------------- #
def test_single_outlier_does_not_fake_a_collapse():
    dates = _sessions(DEFAULT_BASELINE_WINDOW + 1)
    bars = _panel(BASELINE_ONLY + [1.0])
    before = evaluate_exposure(bars, dates[-1])
    assert before.multiplier == MULTIPLIER_UNCHANGED

    # One name jumps 50% on the very last bar -- everything else is untouched.
    modified = bars.copy()
    victim = modified[modified["symbol"] == DEFAULT_SYMBOLS[0]].sort_values("ts").index[-1]
    modified.loc[victim, "close"] = modified.loc[victim, "close"] * 1.5
    after = evaluate_exposure(modified, dates[-1])

    # The outlier INFLATES `D` (a single name 50% away from nine flat ones). It must move the
    # reading away from the collapse bands, never into them -- that is precisely where a
    # robust-but-degenerate statistic would fail (see the MAD case below).
    assert after.dispersion > before.dispersion
    assert after.multiplier == MULTIPLIER_UNCHANGED
    assert after.status == "NORMAL"
    assert after.ratio > LIGHT_COLLAPSE_RATIO


def test_mad_would_degenerate_but_std_does_not():
    # 8 of 10 names tie exactly; the two that move are scaled so the *std* dispersion equals the
    # baseline. A MAD reading would be 0.0 here -- "maximum collapse" - which is the degeneration
    # `docs/development_plan.md:334` item 1 rejects.
    tied_z = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -3.4, 3.4)
    dates = _sessions(DEFAULT_BASELINE_WINDOW + 1)
    bars = _panel(BASELINE_ONLY + [(1.0, tied_z)])

    matrix = session_close_matrix(bars)
    returns = window_returns(matrix, lookback_bars=RETURN_LOOKBACK_BARS)
    row = returns.loc[(dates[-1], "15:00")].dropna()
    mad = float(np.median(np.abs(row.to_numpy() - np.median(row.to_numpy()))))
    assert mad == 0.0            # what MAD would have reported: a fabricated full collapse
    assert float(row.std(ddof=1)) > 0.0

    decision = evaluate_exposure(bars, dates[-1])
    assert decision.dispersion is not None and decision.dispersion > 0.0
    assert decision.multiplier == MULTIPLIER_UNCHANGED
    assert decision.status == "NORMAL"


# --------------------------------------------------------------------------- #
# 5. thin cross-section: 1.0 + INSUFFICIENT_DATA, `dispersion` is None (never 0.0)
# --------------------------------------------------------------------------- #
def test_thin_cross_section_is_insufficient_not_collapsed():
    thin = tuple(f"EQ{i:02d}.SZ" for i in range(1, MIN_CROSS_SECTION_SYMBOLS))  # 7 symbols
    dates = _sessions(DEFAULT_BASELINE_WINDOW + 1)
    decision = evaluate_exposure(_panel(BASELINE_ONLY + [0.30], symbols=thin), dates[-1])

    assert decision.status == "INSUFFICIENT_DATA"
    assert decision.multiplier == MULTIPLIER_UNCHANGED
    # `is None`, not `== 0.0`: a zero would read as "measured, the flattest possible tape".
    assert decision.dispersion is None
    assert decision.baseline is None
    assert len(decision.symbols_used) < MIN_CROSS_SECTION_SYMBOLS
    assert str(MIN_CROSS_SECTION_SYMBOLS) in decision.reason


# --------------------------------------------------------------------------- #
# 6. cash proxies never enter the cross-section
# --------------------------------------------------------------------------- #
def _noisy_proxy_rows(dates, symbol: str) -> list:
    rows = []
    price = 100.0
    for day in dates:
        for label in GRID_LABELS:
            price *= math.exp(0.02)          # a ~2%/bar grind: wildly dispersed vs the equities
            rows.append({
                "symbol": symbol,
                "ts": pd.Timestamp(f"{day.isoformat()} {label}", tz="Asia/Shanghai"),
                "close": price,
            })
    return rows


def test_cash_proxies_never_enter_the_cross_section():
    dates = _sessions(DEFAULT_BASELINE_WINDOW + 1)
    bars = _panel(BASELINE_ONLY + [0.30])
    clean = evaluate_exposure(bars, dates[-1])
    assert clean.status == "COLLAPSE"

    proxy_rows = []
    for proxy in ("BIL", "511880", "GC001"):
        proxy_rows.extend(_noisy_proxy_rows(dates, proxy))
    polluted = pd.concat([bars, pd.DataFrame(proxy_rows)], ignore_index=True)

    with_proxies = evaluate_exposure(polluted, dates[-1])
    # Identical to the proxy-free panel: had they been included, their ~40% 20-bar returns would
    # have inflated `D` far above `B` and turned the reading into a no-op.
    assert with_proxies.multiplier == clean.multiplier == MULTIPLIER_FULL
    assert with_proxies.dispersion == clean.dispersion
    assert with_proxies.baseline == clean.baseline
    assert set(with_proxies.symbols_used).isdisjoint(CASH_PROXY_SYMBOLS)
    assert exclude_cash_proxies(["EQ01.SZ", "BIL", "511880", "GC001"]) == ("EQ01.SZ",)

    # Passing the proxies in `universe` explicitly must not reintroduce them.
    explicit = evaluate_exposure(polluted, dates[-1],
                                 universe=list(DEFAULT_SYMBOLS) + ["BIL", "511880", "GC001"])
    assert explicit.multiplier == clean.multiplier
    assert set(explicit.symbols_used).isdisjoint(CASH_PROXY_SYMBOLS)


# --------------------------------------------------------------------------- #
# 7. a missing bar inside the window is dropped and counted, never interpolated
# --------------------------------------------------------------------------- #
def test_symbol_missing_a_bar_inside_the_window_is_dropped_not_interpolated():
    dates = _sessions(DEFAULT_BASELINE_WINDOW + 1)
    bars = _panel(BASELINE_ONLY + [0.30])
    victim = DEFAULT_SYMBOLS[2]
    # Interior bar of the 21-bar window: 20 bars back from 15:00 is GRID_LABELS[27], so taking an
    # index strictly inside (28..46) proves the hole is *inside* the lookback, not at its edge.
    hole_label = GRID_LABELS[30]
    mask = ((bars["symbol"] == victim)
            & (bars["ts"].dt.date == dates[-1])
            & (bars["ts"].dt.strftime("%H:%M") == hole_label))
    assert mask.sum() == 1
    holed = bars[~mask]

    decision = evaluate_exposure(holed, dates[-1])
    assert decision.symbols_dropped == (victim,)
    assert victim not in decision.symbols_used
    assert len(decision.symbols_used) == len(DEFAULT_SYMBOLS) - 1

    removed = bars[bars["symbol"] != victim]
    expected = evaluate_exposure(removed, dates[-1])
    assert decision.dispersion == expected.dispersion
    assert decision.multiplier == expected.multiplier


# --------------------------------------------------------------------------- #
# 8. baseline discipline: a shorter window is refused, never shortened
# --------------------------------------------------------------------------- #
def test_baseline_needs_a_full_window():
    short = DEFAULT_BASELINE_WINDOW - 10           # only 10 prior sessions
    dates = _sessions(short + 1)
    decision = evaluate_exposure(_panel([1.0] * short + [0.30]), dates[-1])

    assert decision.status == "INSUFFICIENT_DATA"
    assert decision.multiplier == MULTIPLIER_UNCHANGED
    assert decision.baseline is None
    assert decision.baseline_observations == short
    # The day's own dispersion IS measurable -- "baseline missing" and "cross-section missing" are
    # different states and must not collapse into one another.
    assert decision.dispersion is not None
    assert str(DEFAULT_BASELINE_WINDOW) in decision.reason


# --------------------------------------------------------------------------- #
# 9. config / fence guards
# --------------------------------------------------------------------------- #
def test_config_refuses_inverted_or_degenerate_bands():
    with pytest.raises(ValueError):
        CsdConfig(full_collapse_ratio=0.80)         # full band above the light band
    with pytest.raises(ValueError):
        CsdConfig(full_multiplier=0.75, light_multiplier=0.75)   # not a downshift ladder
    with pytest.raises(ValueError):
        CsdConfig(min_cross_section=1)              # a sample std of one number does not exist
    with pytest.raises(ValueError):
        CsdConfig(baseline_window=0)
    with pytest.raises(ValueError):
        CsdConfig(full_multiplier=0.0)              # 0.0 is liquidation, not de-risking


def test_every_result_carries_the_advisory_fence():
    dates = _sessions(DEFAULT_BASELINE_WINDOW + 1)
    for spec in (1.30, 0.70, 0.30):
        decision = evaluate_exposure(_panel(BASELINE_ONLY + [spec]), dates[-1])
        assert decision.advisory is True
        assert decision.gates_orders is False
        assert decision.session_date == dates[-1]
        assert decision.bar_label == "15:00"


def test_unknown_session_is_refused_not_silently_unaffected():
    dates = _sessions(DEFAULT_BASELINE_WINDOW + 1)
    bars = _panel(BASELINE_ONLY + [0.30])
    with pytest.raises(ValueError):
        evaluate_exposure(bars, dt.date(2030, 1, 2))
    with pytest.raises(TypeError):
        evaluate_exposure(bars, dt.datetime(2024, 7, 1, 15, 0))
    with pytest.raises(ValueError):
        evaluate_exposure(bars, dates[-1], bar_label="15:07")   # off the frozen 5m grid


def test_grid_constants_match_the_frozen_panel_spec():
    # The operator is defined on bar counts, so the grid width it shifts across must be the frozen
    # 48-slot session -- a 49th label would silently change what "20 bars back" means.
    assert len(GRID_LABELS) == SESSION_BARS_PER_DAY
    assert RETURN_LOOKBACK_BARS == 20
