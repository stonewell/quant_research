"""Hard investability screens, applied BEFORE `scoring.score_universe()` and
BEFORE `selection.py`'s diversification-aware methods -- a separate,
earlier pipeline stage this project previously lacked. `config.
min_avg_dollar_volume` existed already but was never actually enforced
anywhere; it was only consumed by `scoring.liquidity_score` as a soft,
rank-based input, so a genuinely illiquid instrument could still "buy back"
a good composite score with strength on an unrelated dimension (predictable
returns, low correlation, long history) and end up selected anyway.

A follow-up deep-research pass specifically checked whether that soft-only
treatment is defensible, and found real, citable evidence it isn't:

1. **Index-provider precedent** (MSCI GIMI / Factor Index methodology,
   verified against primary-source PDFs, stable across ~2013-2024 document
   vintages): a set of enumerated, BINARY pass/fail investability screens
   (minimum size, minimum liquidity, minimum length of trading, financial
   reporting) defines an eligible universe strictly BEFORE any
   factor-tilting, weighting, or optimization step runs on it. MSCI's own
   factor "Alpha score" formula deliberately excludes liquidity as a
   component -- liquidity is resolved entirely by universe construction,
   never blended into the score optimization selects on.
2. **The composite-indicator/MCDA literature** (OECD/JRC Handbook on
   Constructing Composite Indicators; Cinelli, Kadziński, Gonzalez &
   Słowiński, *Omega*, peer-reviewed) formally names this failure mode
   "full compensability": an additive/weighted score lets a unit offset a
   deficiency on one dimension with strength on another, which the
   Handbook's own worked example shows can make (21,1,1,1) and (6,6,6,6)
   score identically despite representing very different underlying
   conditions. Its prescription for a NON-NEGOTIABLE requirement (a
   tradability floor is exactly that) is to remove it from the composite
   via a prior hard gate -- not to keep tuning weights.
3. This project's OWN existing `hurst_min_obs` floor (see `persistence.py`)
   already implements exactly this pattern correctly for the Hurst test
   specifically -- below `hurst_min_obs` observations, `persistence_summary`
   returns NaN/`"insufficient_data"` rather than a low-confidence score,
   which independent peer-reviewed evidence (Weron 2002, *Physica A*)
   supports directly: DFA/GPH Hurst-estimator error degrades by roughly an
   order of magnitude between L=256 and L=65536 observations, and the
   original study excluded its own shortest sample size from part of its
   analysis rather than including it with a caveat. This module extends the
   SAME pattern to liquidity and overall history length, which previously
   had no equivalent hard floor.

`min_history_years` (a new, deliberately low/permissive HARD floor -- not
the existing `min_history_years_for_full_credit`, which stays a SOFT
scoring threshold for ETF-closure-risk credit) and `min_avg_dollar_volume`
are the two gates implemented here. The benchmark symbol is never screened
out (beta and the correlation regime-shift check both require it to be
present), and every excluded symbol's reason is reported rather than having
it silently vanish -- the same "expose what got dropped and why"
transparency this project already uses for redundant pairs and per-symbol
score breakdowns.

Not resolved by this pass, and not by the wider literature searched for it
either (an explicitly open question after adversarial verification): whether
screening BEFORE vs. AFTER correlation/clustering changes which instruments
end up as cluster representatives or greedy diversification picks. This
project screens before (matching the index-provider precedent's ordering),
but that ordering choice itself is not independently verified to matter --
or not to -- for these particular selection algorithms.
"""

import pandas as pd


def screen_universe(metrics: pd.DataFrame, config, benchmark: str = None) -> tuple:
    """`metrics` must have `avg_dollar_volume` and `history_years` columns
    (both already computed by `run_screener.py` before this is called).
    Returns `(passed, screened_out)`: `passed` is the subset of `metrics`
    clearing every hard gate (benchmark always included, if present);
    `screened_out` is the excluded rows plus a `screen_fail_reason` column
    (semicolon-joined if more than one gate failed)."""
    reasons = pd.Series([""] * len(metrics), index=metrics.index, dtype=object)

    fails_liquidity = metrics["avg_dollar_volume"].fillna(0) < config.min_avg_dollar_volume
    reasons = reasons.mask(fails_liquidity, reasons + "liquidity;")

    fails_history = metrics["history_years"].fillna(0) < config.min_history_years
    reasons = reasons.mask(fails_history, reasons + "history;")

    failed = (fails_liquidity | fails_history)
    if benchmark is not None and benchmark in metrics.index:
        failed.loc[benchmark] = False

    screened_out = metrics.loc[failed].copy()
    screened_out["screen_fail_reason"] = reasons.loc[failed].str.rstrip(";")
    passed = metrics.loc[~failed]
    return passed, screened_out
