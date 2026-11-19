"""Standalone AllocationTemplate for ML1 Alpha Mining score panels.

Dependency Isolation Contract:
- This module MUST NEVER import ``lightgbm`` (directly or transitively).
  It consumes a precomputed OOF or holdout ``score_panel`` (date x symbol
  DataFrame) and never trains a model, allowing ``common`` / ``backtester``
  or ``pipeline`` tests to import it cleanly without ``lightgbm`` installed.

Sparse Weights & NaN vs 0.0 Contract:
- Non-rebalance rows are strictly ``NaN`` (never forward-filled).
- On every rebalance row (every ``rebalance_every`` trading bars starting from
  the first bar with finite scores), top-N ranked tradable symbols receive
  equal weight ``1 / M`` (row sum <= 1.0), and **every excluded symbol is
  written as an explicit ``0.0``** so the backtester never silently carries
  forward a stale position.

Warmup Semantics (deliberately asymmetric -- do not "fix"):
- ``warmup`` ONLY feeds :meth:`warmup_bars`, which external callers (e.g.
  ``common.allocation_search.run_walkforward``) use to prepend a warmup fold
  buffer before scoring a fold.
- :meth:`generate_weights` deliberately NEVER consumes ``warmup`` / never skips
  leading bars: the score panel it consumes is already pre-warmed over the full
  history, and the caller has already buffered the fold. Skipping again here
  would double-count the warmup.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from common.allocation_templates import AllocationTemplate, _get_master_index


class AlphaMiningStrategy(AllocationTemplate):
    """Equal-weight top-N cross-sectional allocation driven by a precomputed ML1 score panel."""

    version: str = "1.1.0"

    def __init__(
        self,
        score_panel: Optional[pd.DataFrame] = None,
        *,
        top_n: int = 5,
        rebalance_every: int = 5,
        warmup: int = 0,
        name: str = "alpha_mining",
    ) -> None:
        if int(top_n) < 1:
            raise ValueError(f"top_n must be >= 1, got {top_n}")
        if int(rebalance_every) < 1:
            raise ValueError(f"rebalance_every must be >= 1, got {rebalance_every}")
        if int(warmup) < 0:
            raise ValueError(f"warmup must be >= 0, got {warmup}")

        self.score_panel = score_panel
        self.top_n = int(top_n)
        self.rebalance_every = int(rebalance_every)
        self._warmup_bars = int(warmup)
        super().__init__(
            name=name,
            param_grid={},
            factor_tags=["cross_sectional_ml", "gp_alpha"],
            version=self.version,
        )

    def generate_weights(
        self,
        universe: Dict[str, pd.DataFrame],
        params: Optional[dict] = None,
    ) -> pd.DataFrame:
        """Generate sparse target weights from a precomputed score panel.

        Parameters
        ----------
        universe : Dict[str, pd.DataFrame]
            Symbol -> OHLCV DataFrame mapping.
        params : Optional[dict]
            Optional overrides actually read here: ``score_panel``, ``top_n``,
            ``rebalance_every`` (or ``holding_period``). ``warmup_bars`` is NOT
            read -- see the note below.

        Notes
        -----
        ``warmup`` is intentionally a ``warmup_bars()``-only knob: it exists so
        external walkforward drivers can prepend a fold warmup buffer
        (``common/allocation_search.py``'s ``run_walkforward`` already does
        this). This method deliberately starts rebalancing at the first bar
        with finite tradable scores and never skips leading bars from
        ``warmup``, because the score panel is pre-warmed over full history and
        skipping again would double-count the warmup.

        Returns
        -------
        pd.DataFrame
            Sparse target weights (``NaN`` on non-rebalance days; explicit
            ``0.0`` for excluded symbols and ``1/M`` for selected top-N symbols
            on rebalance days; never forward-filled).
        """
        p = params or {}
        score_panel = p.get("score_panel", self.score_panel)
        top_n = int(p.get("top_n", self.top_n))
        rebalance_every = int(
            p.get("rebalance_every", p.get("holding_period", self.rebalance_every))
        )
        if top_n < 1:
            raise ValueError(f"top_n must be >= 1, got {top_n}")
        if rebalance_every < 1:
            raise ValueError(f"rebalance_every must be >= 1, got {rebalance_every}")

        symbols = list(universe.keys())
        if not symbols:
            return pd.DataFrame()

        master_index = _get_master_index(universe)
        if len(master_index) == 0:
            return pd.DataFrame(columns=symbols, dtype=float)

        weights = pd.DataFrame(np.nan, index=master_index, columns=symbols, dtype=float)
        if score_panel is None or score_panel.empty:
            return weights

        aligned_scores = score_panel.reindex(index=master_index, columns=symbols)
        closes = pd.DataFrame(
            {
                sym: universe[sym]["Close"].reindex(master_index)
                for sym in symbols
                if "Close" in universe[sym].columns
            },
            index=master_index,
        ).reindex(columns=symbols)

        score_arr = aligned_scores.to_numpy(dtype=float)
        close_arr = closes.to_numpy(dtype=float)
        tradable_scored = np.isfinite(score_arr) & np.isfinite(close_arr) & (close_arr > 0.0)

        # Start rebalancing on the first bar that has at least one tradable scored asset.
        # NOTE: ``self._warmup_bars`` is deliberately NOT applied here -- it only
        # feeds ``warmup_bars()`` for external walkforward fold buffering, and the
        # score panel is pre-warmed over full history. Skipping again would
        # double-count the warmup already prepended by ``run_walkforward``.
        valid_bar_positions = np.flatnonzero(tradable_scored.any(axis=1))
        if len(valid_bar_positions) == 0:
            return weights

        first_pos = int(valid_bar_positions[0])
        for pos in range(first_pos, len(master_index), rebalance_every):
            valid_cols = np.flatnonzero(tradable_scored[pos])
            if len(valid_cols) == 0:
                continue

            # Explicit 0.0 for EVERY symbol on a rebalance row (NaN vs 0.0 contract)
            row_w = np.zeros(len(symbols), dtype=float)

            # Deterministic top-N selection: highest score first, symbol name tie-break
            ranked = sorted(
                valid_cols,
                key=lambda c: (-float(score_arr[pos, c]), str(symbols[c])),
            )
            chosen = ranked[: min(top_n, len(ranked))]
            alloc = 1.0 / float(len(chosen))
            for c in chosen:
                row_w[c] = alloc

            weights.iloc[pos] = row_w

        return weights

    def explain_weights(self, params: Optional[dict] = None) -> str:
        p = params or {}
        top_n = int(p.get("top_n", self.top_n))
        rebalance_every = int(
            p.get("rebalance_every", p.get("holding_period", self.rebalance_every))
        )
        return (
            f"AlphaMiningStrategy v{self.version}: consumes a precomputed ML1 "
            f"(GP + LightGBM) cross-sectional score panel without in-backtest training. "
            f"Every {rebalance_every} trading bars, allocates equal weight (1/M, sum <= 1.0) "
            f"to the top {top_n} highest-scoring tradable symbols, writes explicit 0.0 for "
            f"all excluded symbols, and leaves non-rebalance rows NaN (strict sparse weights)."
        )

    def warmup_bars(self, params: Optional[dict] = None) -> int:
        p = params or {}
        return int(p.get("warmup_bars", self._warmup_bars))
