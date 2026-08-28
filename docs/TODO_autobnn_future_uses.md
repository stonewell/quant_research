[ English | [简体中文](TODO_autobnn_future_uses_ZH.md) ]

# TODO: other potential AutoBNN uses in this workspace

**Status: parked for later revisit. Nothing here is implemented, and this file is deliberately not
linked from any README or referenced by any code.** The only AutoBNN integration actually shipped
is the standalone `ml/bnn_forecaster/` project (see its own README). This file captures a broader
analysis of where else AutoBNN's capability (a compositional Bayesian Neural Network producing a
calibrated median + confidence-interval forecast per time series, via `predict_quantiles`) could
plausibly apply elsewhere in this workspace, for whenever that's worth picking up.

## 1. `instrument_selection` — a new "forecastability" signal

Today's `selectorbot/persistence.py` (Hurst exponent) and `selectorbot/momentum.py`
(momentum-efficacy correlation) only answer "is this series statistically distinguishable from a
random walk" via a shuffle-null significance test on a descriptive statistic. Neither produces a
forward point-or-interval forecast.

AutoBNN's own in-sample vs. held-out interval calibration would be a genuinely different, deeper
gate: split an instrument's history, fit on everything up to `T - horizon_days`, then check whether
the true held-out value at `T` actually falls inside the model's predicted interval at the stated
confidence level. An instrument whose intervals are reliably well-calibrated (not too wide to be
useless, not so narrow they're routinely violated) has "BNN-discoverable structure" in a sense
neither Hurst nor momentum-efficacy currently measure — complementary to both, not a replacement.

## 2. Position-sizing overlay for existing strategies (`strategy_generator` / `research_strategy`)

Rather than a standalone buy/sell strategy (which is what `bnn_forecaster` already is), AutoBNN's
`ci_width` output could scale an EXISTING template's position size as a risk overlay: narrower
confidence interval -> larger allocation, wider -> smaller. Natural hook points:
`common/allocation_templates.py`'s weighting logic, or `common/strategy_aspects.py`'s
`WeightingAspect` abstraction (a new `bnn_confidence_scaled` weighting aspect that wraps any
existing weighting aspect's output and rescales it by inverse CI width). This would be a
cross-cutting risk-scaling signal layered on top of any existing entry logic, not a competing
entry signal — a meaningfully different integration shape than `bnn_forecaster`'s own standalone
strategy.

## 3. `pattern_mining` cross-validation

AutoBNN's `ChangePoint`/`LearnableChangePoint` operators detect structural breaks via Bayesian
model comparison — a completely different mechanism than `pattern_mining`'s own Bonferroni-corrected
shuffle-null turning-point significance test. Comparing the two independent methods' detected dates
on the same aggregate-portfolio curve would be a genuine robustness cross-check for either one's
mined patterns (agreement between two structurally-different detectors is stronger evidence than
either one alone), without either module depending on the other.

## 4. Factor taxonomy gap

None of `common/factor_taxonomy.py`'s 8 `FACTOR_CATEGORIES` describe a probabilistic/structural
decomposition forecast (the closest, `regime_trend_strength`, is about ADX/Hurst-style regime
classification, not a calibrated forecast). If any of items 1-3 above are ever actually wired into
`research_strategy`'s factor-tagging system, a new tag (something like `"structural_decomposition"`
or `"probabilistic_forecast"`) would be needed first — flagged here only; not added now, since no
strategy consumes it yet.
