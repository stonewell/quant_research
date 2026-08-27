"""Wraps AutoBNN's MAP estimator: fits a compositional Bayesian Neural
Network on log-price data and produces a full-history forecast_return/
ci_width series in one fit + one batched predict_quantiles call, so a
backtester strategy can build a genuinely time-varying daily signal from a
single (expensive) training pass -- see `bnnf/strategy.py`'s own docstring
for why this doesn't need per-fold refitting.
"""

import jax
import numpy as np
import pandas as pd
from autobnn import estimators

from .config import ForecasterConfig


def fit_forecast(closes: pd.Series, cfg: ForecasterConfig) -> pd.DataFrame:
    """Fits a BNN on the trailing `cfg.lookback_days` of `log(closes)`, then
    returns a DataFrame (same index as `closes`) with `forecast_return` and
    `ci_width` columns: the model's own median-forecast annualized log-return
    `cfg.horizon_days` ahead of each date, and the annualized 97.5/2.5
    quantile spread -- via ONE fit + ONE batched `predict_quantiles` call
    across the whole index, not a per-date refit.

    The training target is standardized (zero mean, unit variance) before
    fitting and the predicted quantiles are un-standardized afterward --
    AutoBNN's default priors/kernel scales assume roughly unit-variance
    data (like most GP/BNN libraries); raw log-price (e.g. ~4.5-5.0 with a
    tiny variance) is badly mismatched to that assumption and produces
    wildly miscalibrated (orders-of-magnitude-too-wide) confidence
    intervals without this step.

    DISCLOSED LIMITATIONS (read before trusting `ci_width` for anything):
    1. Look-ahead: fitting once on the trailing window and reading off
       predictions at every date in that same window means the learned
       trend/changepoint structure is informed by data throughout the
       window, not just what was available as-of each individual date -- a
       fold-by-fold walk-forward-clean refit is prohibitively expensive
       given AutoBNN's own compute profile.
    2. Calibration is NOT verified/tuned for financial return series. Even
       after standardization, this module's own experimentation found
       `ci_width` values still routinely in the hundreds-to-thousands-of-
       percent (annualized) range with the default `sum_of_stumps` model
       structure and `normal_likelihood_logistic_noise` likelihood --
       nowhere near usable for a `max_ci_width` gate expressed in normal
       return-magnitude terms (e.g. 0.30 = 30%) without further empirical
       tuning (trying other `model_or_name`/`likelihood_model` choices,
       more particles, or MCMC instead of MAP). This project ships the
       MECHANISM (fit/predict/rules/backtester integration) working
       end-to-end; it does NOT ship a verified-well-calibrated forecast.
       Inspect actual `ci_width` output before trusting `--max-ci-width`.
    """
    n = len(closes)
    y_log = np.log(closes.to_numpy(dtype=np.float64)).astype(np.float32)
    x_all = np.arange(n, dtype=np.float32).reshape(-1, 1)

    train_start = max(0, n - cfg.lookback_days)
    x_train, y_train_raw = x_all[train_start:], y_log[train_start:]

    mu, sigma = float(y_train_raw.mean()), float(y_train_raw.std())
    sigma = sigma if sigma > 0 else 1.0
    y_train = ((y_train_raw - mu) / sigma).astype(np.float32)

    est = _build_estimator(cfg)
    est.fit(x_train, y_train)

    x_targets = x_all + float(cfg.horizon_days)
    quantiles_norm = np.asarray(est.predict_quantiles(x_targets, q=(2.5, 50.0, 97.5)))  # shape (3, n)
    quantiles = quantiles_norm * sigma + mu  # back to log-price scale
    lower, median, upper = quantiles[0], quantiles[1], quantiles[2]

    annualize = 252.0 / cfg.horizon_days
    forecast_return = (median - y_log) * annualize
    ci_width = (upper - lower) * annualize

    return pd.DataFrame({"forecast_return": forecast_return, "ci_width": ci_width}, index=closes.index)


def _build_estimator(cfg: ForecasterConfig):
    seed = jax.random.PRNGKey(cfg.seed)
    if cfg.estimator == "map":
        return estimators.AutoBnnMapEstimator(
            model_or_name="sum_of_stumps", likelihood_model="normal_likelihood_logistic_noise",
            seed=seed, width=cfg.width, num_iters=cfg.num_iters, num_particles=cfg.num_particles,
        )
    if cfg.estimator == "mcmc":
        return estimators.AutoBnnMCMCEstimator(
            model_or_name="sum_of_stumps", likelihood_model="normal_likelihood_logistic_noise",
            seed=seed, width=cfg.width,
        )
    if cfg.estimator == "vi":
        return estimators.AutoBnnVIEstimator(
            model_or_name="sum_of_stumps", likelihood_model="normal_likelihood_logistic_noise",
            seed=seed, width=cfg.width,
        )
    raise ValueError(f"Unknown estimator {cfg.estimator!r} (expected 'map', 'mcmc', or 'vi')")
