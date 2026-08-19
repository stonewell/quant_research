"""Shared primitives for shuffle/placebo-null empirical significance testing.

Four significance tests in this workspace (`instrument_selection`'s
`persistence.hurst_significance`, `candlestick.candlestick_significance`,
`momentum.momentum_efficacy`, and `strategy_generator`'s
`pattern_mining.mine_indicator_patterns`) share the same numeric core: compute
an observed statistic, draw N surrogate statistics under some test-specific
randomization scheme, and report the two-sided empirical fraction of
surrogates at least as extreme as the observation. This module extracts ONLY
that numeric core -- the randomization scheme itself (full shuffle, signal
relocation, exclusion-buffered resampling, ...) is genuinely different per
caller and stays in that caller's own module, injected here as a
`surrogate_stat_fn` callback. This mirrors `common/hurst.py`'s own stated
precedent: "different projects build different significance-testing
methodologies on top of this same base estimator ... that project-specific
logic stays local to each project; only the underlying math is shared here."

The callers differ, deliberately, in what "distance from the null" is
measured FROM -- this is real, not inconsistency to unify away:
  - `hurst_significance` measures deviation from the fixed random-walk value
    0.5.
  - `candlestick_significance`/`momentum_efficacy` measure deviation from 0
    (comparing `abs(surrogate)` to `abs(observed)` directly).
  - `mine_indicator_patterns` measures deviation from the EMPIRICAL surrogate
    mean, since its statistic (a raw feature-value mean) has no natural zero.
The `reference` parameter below makes this explicit per call.
"""

from typing import Callable, Union

import numpy as np


class StopSurrogates(Exception):
    """Raise from a `surrogate_stat_fn` passed to `shuffle_null_test` to
    signal that no further surrogates can be generated (e.g. too few
    eligible bars/dates remain to sample from) and drawing should stop
    immediately, keeping whatever surrogates were already collected."""


def empirical_pvalue(observed_stat: float, surrogate_stats: np.ndarray,
                      reference: float = 0.0) -> float:
    """Two-sided empirical p-value: the fraction of `surrogate_stats` whose
    absolute deviation from `reference` is >= the observed statistic's
    absolute deviation from `reference`. `reference=0.0` (default) reproduces
    `abs(surrogate) >= abs(observed)` directly. Returns `np.nan`, without
    raising, when `surrogate_stats` is empty."""
    surrogate_stats = np.asarray(surrogate_stats, dtype=float)
    if surrogate_stats.size == 0:
        return np.nan
    return np.mean(np.abs(surrogate_stats - reference) >= abs(observed_stat - reference))


def significance_flag(p_value: float, alpha: float = 0.05) -> bool:
    """`p_value < alpha`, treating NaN as not-significant."""
    return bool(p_value < alpha) if not np.isnan(p_value) else False


def shuffle_null_test(
    observed_stat: float,
    surrogate_stat_fn: Callable[[np.random.Generator], float],
    n_surrogates: int,
    rng: np.random.Generator,
    reference: Union[float, Callable[[np.ndarray], float]] = 0.0,
    alpha: float = 0.05,
    skip_nan: bool = True,
) -> dict:
    """Draw up to `n_surrogates` surrogate statistics by calling
    `surrogate_stat_fn(rng)` repeatedly, then report the two-sided empirical
    p-value and significance flag.

    - `rng` is CALLER-OWNED. Pass a fresh `np.random.default_rng(seed)` for a
      single, one-shot test; pass one long-lived generator shared across many
      calls if a single seed must drive a whole menu of tests in sequence
      (each call advances the shared generator's state, exactly as a raw
      inline loop would).
    - `skip_nan=True` (default) drops -- without counting toward
      `n_surrogates` -- any surrogate for which `surrogate_stat_fn` returns
      NaN. Pass `skip_nan=False` for a caller whose surrogates are guaranteed
      non-NaN by construction.
    - `surrogate_stat_fn` may raise `StopSurrogates` to end the loop
      immediately with whatever was collected so far.
    - `reference` is a fixed float, or a callable applied to the assembled
      `surrogate_stats` array to compute a data-dependent reference (e.g.
      `lambda s: s.mean()`).

    Returns: `{"p_value", "significant", "surrogate_stats" (np.ndarray,
    possibly shorter than n_surrogates), "n_surrogates_used", "reference"
    (the resolved float actually used)}`.
    """
    surrogates = []
    for _ in range(n_surrogates):
        try:
            val = surrogate_stat_fn(rng)
        except StopSurrogates:
            break
        if skip_nan and np.isnan(val):
            continue
        surrogates.append(val)

    surrogate_stats = np.array(surrogates, dtype=float)
    if callable(reference):
        resolved_reference = reference(surrogate_stats) if surrogate_stats.size else np.nan
    else:
        resolved_reference = reference

    p_value = empirical_pvalue(observed_stat, surrogate_stats, resolved_reference)
    return {
        "p_value": p_value,
        "significant": significance_flag(p_value, alpha),
        "surrogate_stats": surrogate_stats,
        "n_surrogates_used": int(surrogate_stats.size),
        "reference": resolved_reference,
    }
