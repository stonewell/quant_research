"""Turning-point indicator pattern mining.

Given a universe, builds its aggregate portfolio curve
(`common.allocation_templates.build_aggregate_curve`), detects the curve's
major peaks/troughs (`turning_points.find_turning_points`), and tests
whether any indicator in a fixed "popular technical indicators" menu
(`common.indicator_features.DEFAULT_FEATURE_MENU`), read `lag_bars` trading
days BEFORE those turning points, differs significantly from the same
indicator read before a random (non-turning-point) date. Any pattern that
survives this test can be turned into a `PatternBasedAllocationTemplate`
(`common.allocation_templates`) and passed into `StrategyGenerator.generate`'s
`extra_templates` -- where it competes through the SAME grid-search +
Equivalent Random Search validation as every static template.

WHY THE LAG MATTERS -- THE SINGLE MOST IMPORTANT METHODOLOGICAL DECISION
HERE: reading an indicator EXACTLY AT a zigzag-confirmed turning point is
NEARLY TAUTOLOGICAL, not a discovery. A zigzag peak is, by construction, a
local price maximum reached via a recent run-up; a momentum-style indicator
(RSI, ROC, sma_rel, stochastic %K, ...) computed AT that exact bar is
measuring THE SAME run-up the label is built from -- of course RSI reads
high at a point defined by "price just went up a lot." An early version of
this module tested indicators at lag=0 and found ~80% of the whole menu
"significant" on a PURE RANDOM WALK universe with zero real structure --
not a bug in the significance math, but proof the lag=0 question itself is
nearly definitionally true regardless of the underlying data. Reading the
indicator `lag_bars` (default 20) trading days BEFORE the turning point asks
a genuinely different, forecast-relevant question instead: "did this
indicator already look unusual before the reversal happened, in a way you
could have observed and acted on in real time." A larger lag makes the
test more honest but weaker (indicator readings decay back toward baseline
the further back you look); `lag_bars` is deliberately a single, disclosed,
fixed default rather than another dimension added to the multiple-
comparisons menu.

HONEST RESIDUAL LIMITATION (measured, not assumed): even at `lag_bars=20`,
repeated pure-random-walk negative-control runs still occasionally flag a
handful of the ~26-test menu "significant" -- fewer than at lag=0/5/10 (which
flagged 12-21 of 26), but not a clean, reliable zero. This means some
mechanical/tautological correlation between momentum-style indicators and
momentum-defined turning points survives the lag adjustment; this
significance test alone should be read as a candidate-generation FILTER,
not proof of a real edge. This is exactly why every mined candidate must
ALSO clear the SAME Equivalent Random Search bar every static template
does: a false positive from this residual bias reflects no genuine
predictive structure, so it has no reason to also produce backtested
outperformance against random portfolios -- the ERS layer is the real
check, not the mining significance test by itself.

CONFIRMATION-LAG / HINDSIGHT CAVEAT (read `turning_points.py`'s module
docstring for the full version) -- a SEPARATE issue from the tautology above:
labeling a historical date a "turning point" at all requires a few bars of
hindsight past the turning point itself, regardless of `lag_bars`. Legitimate
for this research/mining pass, but disclosed here and in every mined
template's own `explain_weights()`. The resulting live template has NO such
lag: it only ever compares a live, already-known indicator reading against
the mined threshold, never trying to detect a turning point in real time.

WHY THIS MENU IS AN EXCEPTION TO THIS PROJECT'S "SMALL PRIMITIVE SET" RULE:
`stratgen/indicators.py`'s own docstring explains why the 9 static templates
deliberately restrict themselves to a handful of primitives (Allen &
Karjalainen 1999's data-snooping caution). This module needs a genuinely
broad menu to mine against instead, and guards against the resulting
multiple-comparisons risk a DIFFERENT way: a Bonferroni-corrected
significance test across the whole menu (not just a single named statistic,
unlike `instrument_selection`'s momentum/candlestick/persistence
significance tests, none of which test a menu), plus the same ERS bar
every template must clear regardless of how it was discovered.

EXPECTED OUTCOME ON SYNTHETIC DATA: per this workspace's own repeated
finding elsewhere (`instrument_selection`'s Hurst/momentum/candlestick
significance tests, `strategy_generator`'s own ERS checks), most series show
no significant structure. Finding ZERO significant patterns here is the
common, correct result on synthetic GBM-like data, not a bug -- callers must
degrade gracefully to the 9 static templates, not treat it as an error.
"""

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from common.allocation_templates import PatternBasedAllocationTemplate, build_aggregate_curve
from common.indicator_features import DEFAULT_FEATURE_MENU, compute_feature, feature_label
from common.significance import shuffle_null_test
from .turning_points import find_turning_points


def _feature_own_lookback(lookback) -> int:
    """The single scalar lookback governing one feature's own exclusion
    buffer -- the LONGEST of its parameters for a multi-parameter feature
    (e.g. macd_hist's slow period)."""
    return max(lookback) if isinstance(lookback, (tuple, list)) else lookback


def _build_feature_table(curve: pd.DataFrame, feature_menu: list) -> pd.DataFrame:
    """Whole-curve indicator series for every (name, lookback) in the menu,
    one column per feature, keyed by `feature_label`. `macd_hist` and any
    other feature needing Volume/High/Low is silently skipped if `curve`
    lacks the required column (e.g. no Volume across the universe)."""
    cols = {}
    for name, lookback in feature_menu:
        try:
            cols[feature_label(name, lookback)] = compute_feature(curve, name, lookback)
        except KeyError:
            continue  # curve is missing a required column (e.g. Volume) for this feature
    return pd.DataFrame(cols, index=curve.index)


def mine_indicator_patterns(
    universe: Dict[str, pd.DataFrame],
    feature_menu: Optional[list] = None,
    min_swing_pct: float = 0.05,
    lag_bars: int = 20,
    n_surrogates: int = 200,
    seed: Optional[int] = None,
    pattern_min_obs: int = 200,
    pattern_min_turning_points: int = 20,
) -> Tuple[pd.DataFrame, str]:
    """Mines the universe's aggregate turning-point history for significant
    indicator patterns, reading each indicator `lag_bars` trading days
    BEFORE each turning point (see the module docstring's "WHY THE LAG
    MATTERS" section -- reading indicators exactly AT a turning point is
    nearly tautological for momentum-style indicators, not a discovery).

    Returns `(findings, status)`. `status` is `"ok"`, `"insufficient_data"`
    (fewer than `pattern_min_obs` aligned bars), or
    `"insufficient_turning_points"` (fewer than `pattern_min_turning_points`
    confirmed peaks+troughs) -- `findings` is an EMPTY DataFrame in both
    degenerate cases, and is also commonly empty with `status="ok"` when
    nothing survives the significance test (the expected, correct outcome
    on synthetic data, not an error).

    When `status="ok"`, `findings` has one row per (feature, event_type)
    combination with columns: `feature`, `lookback`, `event_type`
    ("peak"/"trough"), `observed_stat`, `null_mean`, `p_value`,
    `adjusted_alpha`, `significant`, `comparison` ("below"/"above" --
    whether the observed mean sits below or above the null mean), `n_events`.
    """
    feature_menu = feature_menu if feature_menu is not None else DEFAULT_FEATURE_MENU

    curve = build_aggregate_curve(universe)
    if len(curve) < pattern_min_obs:
        return pd.DataFrame(), "insufficient_data"

    turning_points = find_turning_points(curve["Close"], min_swing_pct=min_swing_pct)
    if len(turning_points) < pattern_min_turning_points:
        return pd.DataFrame(), "insufficient_turning_points"

    feature_table = _build_feature_table(curve, feature_menu)
    if feature_table.empty:
        return pd.DataFrame(), "insufficient_data"

    rng = np.random.default_rng(seed)

    all_dates = feature_table.index
    n_total = len(all_dates)
    date_to_pos = {d: i for i, d in enumerate(all_dates)}
    turning_point_positions = [date_to_pos[d] for d in turning_points.index if d in date_to_pos]

    def _eligible_positions_for(lookback_for_buffer: int) -> np.ndarray:
        """Exclude a window (this FEATURE's own lookback, not the whole
        menu's longest) around every real turning point from the pool of
        eligible null dates -- a trailing indicator reading ADJACENT to a
        real trough is itself likely to look trough-like, so an unbuffered
        null could accidentally re-import the very effect being tested
        (biasing toward NOT finding significance -- conservative, but worth
        the fix). Using each feature's OWN lookback (rather than a single
        buffer sized to the menu's longest feature) matters in practice:
        turning points are often spaced closer together than the menu's
        longest lookback, and a too-wide global buffer can exclude the
        entire series, leaving no eligible null dates for ANY feature."""
        excluded = np.zeros(n_total, dtype=bool)
        for pos in turning_point_positions:
            lo, hi = max(0, pos - lookback_for_buffer), min(n_total, pos + lookback_for_buffer + 1)
            excluded[lo:hi] = True
        return np.where(~excluded)[0]

    feature_names = list(feature_table.columns)
    event_types = [t for t in ("peak", "trough") if (turning_points["type"] == t).any()]
    n_tests = len(feature_names) * len(event_types)
    if n_tests == 0:
        return pd.DataFrame(), "ok"
    adjusted_alpha = 0.05 / n_tests  # Bonferroni correction across the full menu

    positions_by_type = {"peak": [], "trough": []}
    for date, row in turning_points.iterrows():
        if date in date_to_pos:
            positions_by_type[row["type"]].append(date_to_pos[date])

    findings = []
    for event_type in event_types:
        # Read each indicator `lag_bars` trading days BEFORE the turning
        # point (positional offset, not a calendar lookup) -- see the
        # module docstring's "WHY THE LAG MATTERS" section.
        lagged_positions = [p - lag_bars for p in positions_by_type[event_type] if p - lag_bars >= 0]
        n_events = len(lagged_positions)
        if n_events == 0:
            continue

        for feat_name, lookback in _feature_name_lookback_pairs(feature_menu, feature_names):
            label = feature_label(feat_name, lookback)
            if label not in feature_table.columns:
                continue
            series = feature_table[label]

            observed_values = series.iloc[lagged_positions].dropna()
            if observed_values.empty:
                continue
            observed_stat = float(observed_values.mean())

            eligible_positions = _eligible_positions_for(_feature_own_lookback(lookback))
            valid_eligible = [p for p in eligible_positions if not pd.isna(series.iloc[p])]
            if len(valid_eligible) < n_events:
                continue

            def _surrogate_stat(rng, series=series, valid_eligible=valid_eligible, n_events=n_events):
                return series.iloc[rng.choice(valid_eligible, size=n_events, replace=False)].mean()

            result = shuffle_null_test(
                observed_stat, _surrogate_stat, n_surrogates, rng,
                reference=lambda s: s.mean(),   # null_mean, computed from this test's own surrogates
                alpha=adjusted_alpha,           # Bonferroni layered on via alpha, no threshold math duplicated
                skip_nan=False,                 # guaranteed non-NaN: valid_eligible pre-filters NaN feature values
            )
            null_mean = float(result["reference"])
            p_value = float(result["p_value"])
            significant = result["significant"]
            comparison = "below" if observed_stat < null_mean else "above"

            findings.append({
                "feature": feat_name,
                "lookback": lookback,
                "event_type": event_type,
                "observed_stat": observed_stat,
                "null_mean": null_mean,
                "p_value": p_value,
                "adjusted_alpha": adjusted_alpha,
                "significant": significant,
                "comparison": comparison,
                "threshold": float(observed_values.median()),
                "n_events": n_events,
            })

    return pd.DataFrame(findings), "ok"


def _feature_name_lookback_pairs(feature_menu, feature_names):
    """Re-derive (name, lookback) pairs matching feature_names' labels --
    avoids re-parsing a label string back into a (name, lookback) tuple."""
    for name, lookback in feature_menu:
        if feature_label(name, lookback) in feature_names:
            yield name, lookback


def build_pattern_templates(
    findings: pd.DataFrame, max_templates: int = 5
) -> List[PatternBasedAllocationTemplate]:
    """Turns the top (by p-value) significant rows of `mine_indicator_patterns`'s
    findings into concrete `PatternBasedAllocationTemplate` instances, ready
    to pass as `StrategyGenerator.generate(..., extra_templates=...)`.
    Returns an empty list if `findings` is empty or has no significant rows
    -- the expected common case, not an error."""
    if findings.empty:
        return []

    significant = findings[findings["significant"]].sort_values("p_value").head(max_templates)
    templates = []
    for _, row in significant.iterrows():
        templates.append(PatternBasedAllocationTemplate(
            feature_name=row["feature"],
            feature_lookback=row["lookback"],
            threshold=row["threshold"],
            comparison=row["comparison"],
            event_type=row["event_type"],
            mined_p_value=row["p_value"],
            mined_n_events=int(row["n_events"]),
        ))
    return templates
