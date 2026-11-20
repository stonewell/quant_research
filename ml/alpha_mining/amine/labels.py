"""Forward-return label construction for ML1 Alpha Mining.

Academic & Quantitative Grounding:
- Execution-lagged forward returns (``exec_lag=1``) avoid same-close execution
  bias when signals are generated from day-t close prices and executed at day
  t+1 close.
- Residual return labels (``market_residual`` and ``specific_residual``)
  delegate to ``common.specific_alpha.specific_alpha_residual`` (Ross 1976;
  Blitz, Huij & Martens 2011).

HONEST CAVEATS (Disclosed):
1. ``common.specific_alpha.specific_alpha_residual`` is a SIMPLIFIED offline
   two-stage approximation (no industry block, only 2 of 10 Barra style
   descriptors) marked advisory-only. Consequently, ``raw_return`` is the
   default label mode; ``market_residual`` and ``specific_residual`` are
   opt-in research modes.
2. When ``sector_neutral=True``, sector neutralization delegates to
   ``common.alpha101_operators.cs_indneutralize``. Any symbol not present in
   the resolved group mapping passes through unneutralized (this happens when
   an explicit partial ``sector_groups`` dict is supplied; when using
   ``resolve_sector``, unmapped symbols fall back per ``sector_fallback``).
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from common.alpha101_operators import cs_indneutralize
from common.cross_sectional import cs_rank_gaussian_or_uniform
from common.sector_metadata import load_sector_map, resolve_sector
from common.specific_alpha import (
    DEFAULT_MIN_OBS,
    specific_alpha_residual,
    style_exposures_from_close,
)

VALID_LABEL_MODES = (
    "raw_return",
    "market_residual",
    "specific_residual",
    "cross_sectional_rank",
)


def build_forward_labels(
    close: pd.DataFrame,
    holding_period: int = 5,
    exec_lag: int = 1,
    *,
    mode: str = "raw_return",
    benchmark_returns: Optional[pd.Series] = None,
    min_obs: int = DEFAULT_MIN_OBS,
    sector_neutral: bool = False,
    sector_map: Optional[Dict[str, str]] = None,
    sector_map_path: Optional[str] = None,
    sector_fallback: Optional[str] = "auto",
    drop_trailing_nan: bool = False,
) -> pd.DataFrame:
    """Build leakage-free forward labels aligned to signal date ``t``.

    For signal date ``t``, holding period ``k = holding_period``, and execution
    lag ``lag = exec_lag``, the total forward dependency horizon is
    ``h = k + lag``:

    - ``raw_return``: ``close[t + k + lag] / close[t + lag] - 1.0``.
      With ``exec_lag=0``, this equals ``common.factor_analyzer.compute_forward_returns``.
    - ``market_residual``: sum of daily univariate market-model residual returns
      over ``(t + lag, t + k + lag]`` via ``specific_alpha_residual(..., None)``.
    - ``specific_residual``: sum of daily simplified specific-alpha residual
      returns over ``(t + lag, t + k + lag]`` using ``style_exposures_from_close``.

    Trailing ``h`` rows have incomplete forward windows and are strictly ``NaN``
    (or dropped if ``drop_trailing_nan=True``).

    Parameters
    ----------
    close : pd.DataFrame
        Close price panel (DatetimeIndex x symbol columns).
    holding_period : int, default 5
        Holding window ``k >= 1`` in trading bars.
    exec_lag : int, default 1
        Execution delay ``lag >= 0`` in trading bars (1 = enter at t+1 close).
    mode : str, default "raw_return"
        One of ``"raw_return"``, ``"market_residual"``, ``"specific_residual"``,
        or ``"cross_sectional_rank"`` (centered uniform rank in ``[-0.5, +0.5]``).
    benchmark_returns : Optional[pd.Series], default None
        Optional benchmark daily return series for residual modes. When None,
        ``specific_alpha_residual`` uses the panel's cross-sectional mean.
    min_obs : int, default 30
        Minimum cross-sectional observations passed to ``specific_alpha_residual``.
        Use a smaller value (e.g. ``5``) on small synthetic test universes.
    sector_neutral : bool, default False
        If True, cross-sectionally demean labels within each sector group via
        ``cs_indneutralize``.
    sector_map : Optional[Dict[str, str]], default None
        Explicit symbol -> sector mapping. If ``sector_fallback is None``,
        unmapped symbols are omitted from ``groups`` and pass through
        ``cs_indneutralize`` unneutralized.
    sector_map_path : Optional[str], default None
        Optional JSON file path loaded via ``load_sector_map`` when
        ``sector_map`` is None.
    sector_fallback : Optional[str], default "auto"
        Fallback mode passed to ``resolve_sector`` ("auto", "prefix", "other"),
        or ``None`` to pass raw ``sector_map`` directly to ``cs_indneutralize``
        (leaving unmapped symbols unneutralized).
    drop_trailing_nan : bool, default False
        If True, drop the trailing ``h = k + exec_lag`` rows instead of leaving
        them as ``NaN``.

    Returns
    -------
    pd.DataFrame
        Forward label panel with the same columns (and index, unless
        ``drop_trailing_nan=True``) as ``close``.
    """
    if not isinstance(close, pd.DataFrame):
        raise TypeError(f"close must be a pandas DataFrame, got {type(close).__name__}")
    k = int(holding_period)
    lag = int(exec_lag)
    if k < 1:
        raise ValueError(f"holding_period must be >= 1, got {holding_period}")
    if lag < 0:
        raise ValueError(f"exec_lag must be >= 0, got {exec_lag}")
    if mode not in VALID_LABEL_MODES:
        raise ValueError(f"mode must be one of {VALID_LABEL_MODES}, got {mode!r}")

    close_f = close.astype(float)
    h = k + lag

    if mode in ("raw_return", "cross_sectional_rank"):
        entry = close_f.shift(-lag) if lag > 0 else close_f
        exit_px = close_f.shift(-h)
        labels = exit_px / entry.where(entry != 0.0) - 1.0
    else:
        daily_ret = close_f.pct_change()
        if mode == "market_residual":
            exposures = None
        else:
            exposures = style_exposures_from_close(
                close_f, benchmark_returns=benchmark_returns
            )
        daily_resid = specific_alpha_residual(
            daily_ret,
            exposures,
            benchmark_returns=benchmark_returns,
            min_obs=min_obs,
        )
        # Sum k daily residuals across (t + lag, t + k + lag]; NaN if any day is NaN.
        labels = daily_resid.rolling(k, min_periods=k).sum().shift(-h)

    labels = labels.replace([np.inf, -np.inf], np.nan)

    # Enforce strict zero-look-ahead boundary on trailing h rows.
    if h >= len(labels):
        labels.iloc[:] = np.nan
    elif h > 0:
        labels.iloc[-h:] = np.nan

    if sector_neutral:
        resolved_map = sector_map if sector_map is not None else load_sector_map(sector_map_path)
        if sector_fallback is None:
            groups = {
                str(sym): str(resolved_map[sym])
                for sym in labels.columns
                if sym in resolved_map
            }
        else:
            groups = {
                str(sym): resolve_sector(str(sym), resolved_map, fallback=sector_fallback)
                for sym in labels.columns
            }
        labels = cs_indneutralize(labels, groups)

    if mode == "cross_sectional_rank":
        # MS8: cross-sectional rank target mapped to [-0.5, +0.5] via shared MS1 primitive.
        labels = cs_rank_gaussian_or_uniform(labels, method="centered_uniform")
        if h >= len(labels):
            labels.iloc[:] = np.nan
        elif h > 0:
            labels.iloc[-h:] = np.nan

    if drop_trailing_nan:
        if h >= len(labels):
            return labels.iloc[0:0]
        return labels.iloc[:-h]

    return labels
