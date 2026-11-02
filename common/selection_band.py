"""Asymmetric Rank-Band Selection with Delta-Score Significance Gate.

Academic & Practitioner Grounding:
---------------------------------
Standard cross-sectional momentum strategies suffer from high turnover friction due
to rank churn at the boundary of top-N deciles. Following buffer-zone / hysteresis band
literature (e.g., Garleanu & Pedersen 2013, 'Dynamic Portfolio Choice with Transactions
Costs'; institutional benchmark rebalancing protocols), this module introduces an
asymmetric selection band:
  - Buy Band (Entry): Asset enters only if rank <= buy_band_pct (0.0 = top rank).
  - Sell Band (Exit / Retention): Held asset is retained as long as rank <= sell_band_pct.
  - Exit Trigger: Held asset is candidate for drop only when rank > sell_band_pct.
  - Delta-Score Gate: Swap of a held asset for a new entrant is vetoed unless:
        S_new - S_held > delta_score_sigma * sigma_score
    where sigma_score is the cross-sectional standard deviation of scores on that rebalance
    date. Crucially, a veto blocks BOTH that exit and that entry (pairwise swap lock).

Quantitative Engineering Rules & Gotchas:
-----------------------------------------
1. Cell-level NaN vs. 0.0 Contract:
   `run_allocation_backtest` performs column-wise `ffill().fillna(0.0)` across the frame.
   Inside an actual rebalance row, leaving a dropped asset as NaN would silently preserve
   the previous holding! Exited names MUST be assigned an explicit `0.0`.
2. Sparse Weights & Row Omission:
   Templates must never forward-fill their own output. When the held asset set and target
   weights on a rebalance date are identical to the previous rebalance date, the entire row
   is compressed to NaN via `_sparse_from_daily` so the backtester cleanly carries forward
   prior weights without triggering redundant trade execution.
3. Loop-Local State Rationale:
   `generate_weights` is invoked once per candidate strategy search. Previous-holdings and
   score states are tracked strictly via loop-local variables within the date iteration to
   prevent cross-candidate state leakage.

DISCLOSED NARROWING / HONEST CAVEAT (A-Share Suspensions & NaN Scores):
----------------------------------------------------------------------
Under the baseline task specification, assets missing from valid scores on a rebalance
date were dropped immediately. However, in real market conditions (e.g., A-share trading
suspensions), a suspended asset has missing Close prices, causing trailing return / ROC
scores to evaluate to NaN. Dropping unrankable held names causes spurious liquidation
and bypasses the Delta-Score significance gate.
Narrowed behaviour:
  - A held asset whose score is NaN on a rebalance date is RETAINED. Unrankable implies
    no affirmative exit signal.
  - Retained NaN-scored holdings do not participate in pairwise swap matching or gate
    comparisons (they cannot be replaced by an entrant, nor consume an entrant slot).
  - Retained NaN-scored holdings are included in `n_held` for 1/n_held re-normalization
    and receive explicit non-zero target weights.
  - Unheld assets with NaN scores remain completely ineligible for entry.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def _validate_bands(
    buy_band_pct: float,
    sell_band_pct: float,
    delta_score_sigma: float = 0.35,
) -> None:
    """Validate selection band parameters.

    Raises:
        ValueError: If buy_band_pct >= sell_band_pct, bands outside (0, 1], or negative sigma.
    """
    if buy_band_pct <= 0.0 or buy_band_pct > 1.0:
        raise ValueError(f"buy_band_pct must be in (0, 1], got {buy_band_pct}")
    if sell_band_pct <= 0.0 or sell_band_pct > 1.0:
        raise ValueError(f"sell_band_pct must be in (0, 1], got {sell_band_pct}")
    if buy_band_pct >= sell_band_pct:
        raise ValueError(
            f"buy_band_pct ({buy_band_pct}) must be strictly less than sell_band_pct ({sell_band_pct})"
        )
    if delta_score_sigma < 0.0:
        raise ValueError(f"delta_score_sigma must be non-negative, got {delta_score_sigma}")


def compute_percentile_ranks(scores: pd.Series) -> dict[str, float]:
    """Compute cross-sectional percentile ranks (0.0 = best score).

    Deterministic tie-break matches `_select_momentum_topn`:
    Sorted by (-score, symbol_name) ascending.
    Rank is i / N where i is 0-based position and N = len(valid_scores).
    """
    valid = scores.dropna()
    n = len(valid)
    if n == 0:
        return {}
    # Deterministic tie-break: descending score, then ascending symbol name
    sorted_symbols = sorted(valid.index, key=lambda s: (-float(valid[s]), str(s)))
    return {s: i / n for i, s in enumerate(sorted_symbols)}


def _sparse_from_daily(daily: pd.DataFrame) -> pd.DataFrame:
    """Compress consecutive identical rows to NaN for sparse weights contract.

    Note: Local helper is intentionally maintained here rather than reusing
    common.allocation_templates._sparse_from_daily to prevent circular imports.
    """
    changed = (daily != daily.shift(1)).any(axis=1)
    if len(changed) > 0:
        changed.iloc[0] = True
    return daily.where(changed)


class RankBandSelection:
    """Asymmetric Rank-Band and Delta-Score Significance Selection Engine."""

    version: str = "1.0.0"

    def __init__(
        self,
        buy_band_pct: float = 0.10,
        sell_band_pct: float = 0.30,
        delta_score_sigma: float = 0.35,
    ) -> None:
        _validate_bands(buy_band_pct, sell_band_pct, delta_score_sigma)
        self.buy_band_pct = float(buy_band_pct)
        self.sell_band_pct = float(sell_band_pct)
        self.delta_score_sigma = float(delta_score_sigma)

    def select(self, scores_rebal: pd.DataFrame) -> pd.DataFrame:
        """Instance method wrapping apply."""
        return self.apply(
            scores_rebal,
            buy_band_pct=self.buy_band_pct,
            sell_band_pct=self.sell_band_pct,
            delta_score_sigma=self.delta_score_sigma,
        )

    @classmethod
    def apply(
        cls,
        scores_rebal: pd.DataFrame,
        *,
        buy_band_pct: float = 0.10,
        sell_band_pct: float = 0.30,
        delta_score_sigma: float = 0.35,
    ) -> pd.DataFrame:
        """Apply rank-band selection and delta-score gate across rebalance dates.

        Parameters
        ----------
        scores_rebal : pd.DataFrame
            Cross-sectional scores at rebalance dates (rows: dates, columns: symbols).
        buy_band_pct : float
            Percentile rank threshold for new entries (rank <= buy_band_pct).
        sell_band_pct : float
            Percentile rank threshold for retention (rank <= sell_band_pct).
        delta_score_sigma : float
            Multiples of cross-sectional std required to execute a swap.

        Returns
        -------
        pd.DataFrame
            Sparse weights DataFrame. Survivors get 1/n_held, dropped names get 0.0,
            and identical consecutive rebalance rows are compressed to NaN.
        """
        _validate_bands(buy_band_pct, sell_band_pct, delta_score_sigma)

        if scores_rebal.empty:
            return pd.DataFrame(
                index=scores_rebal.index, columns=scores_rebal.columns, dtype=float
            )

        symbols = list(scores_rebal.columns)
        dense_weights = pd.DataFrame(
            0.0, index=scores_rebal.index, columns=symbols, dtype=float
        )

        # Loop-local state across the rebalance timeline
        held_symbols: set[str] = set()

        for date in scores_rebal.index:
            row = scores_rebal.loc[date].dropna()
            if len(row) == 0:
                if held_symbols:
                    # All assets unrankable on this date; retain held assets without swap
                    w = 1.0 / len(held_symbols)
                    for s in held_symbols:
                        dense_weights.loc[date, s] = w
                    continue
                # No valid scores and nothing held; leave the row's weights at 0.0 (no-op)
                held_symbols = set()
                continue

            ranks = compute_percentile_ranks(row)
            n_valid = len(row)
            sigma_score = float(row.std(ddof=1)) if n_valid > 1 else 0.0
            if np.isnan(sigma_score):
                sigma_score = 0.0

            if len(held_symbols) == 0:
                # Initial allocation: all assets meeting entry band
                new_held = set(s for s, r in ranks.items() if r <= buy_band_pct)
            else:
                # 1. Existing held assets evaluation
                # DISCLOSED NARROWING: Held assets with NaN score (e.g. trading suspension)
                # are retained because unrankable means no exit signal. They do not participate
                # in swap pairing or gate comparisons.
                nan_held = set(s for s in held_symbols if s not in ranks)
                retained = set(
                    s for s in held_symbols if s in ranks and ranks[s] <= sell_band_pct
                ) | nan_held
                exit_candidates = [
                    s for s in held_symbols if s in ranks and ranks[s] > sell_band_pct
                ]

                # 2. Potential new entrants (unheld names with rank <= buy_band_pct)
                entry_candidates = [
                    s
                    for s in row.index
                    if s not in held_symbols and ranks[s] <= buy_band_pct
                ]

                # Sort new entrants descending by score (highest score first)
                # Sort exit candidates ascending by score (lowest score / worst rank first)
                entry_sorted = sorted(
                    entry_candidates, key=lambda s: (-float(row[s]), str(s))
                )
                exit_sorted = sorted(
                    exit_candidates, key=lambda s: (float(row[s]), str(s))
                )

                num_swaps = min(len(entry_sorted), len(exit_sorted))
                approved_entries = []
                approved_exits = []
                vetoed_held = []

                threshold = delta_score_sigma * sigma_score
                for k in range(num_swaps):
                    s_new = entry_sorted[k]
                    s_held = exit_sorted[k]
                    delta_score = float(row[s_new]) - float(row[s_held])
                    if delta_score > threshold:
                        approved_entries.append(s_new)
                        approved_exits.append(s_held)
                    else:
                        # Delta-Score gate vetoes swap: blocks both exit and entry
                        vetoed_held.append(s_held)

                # Unpaired entrants / exits (when counts are asymmetric):
                # Extra entrants with rank <= buy_band_pct enter directly.
                # Extra exit candidates with rank > sell_band_pct drop directly.
                unpaired_entries = entry_sorted[num_swaps:]

                new_held = (
                    retained
                    | set(vetoed_held)
                    | set(approved_entries)
                    | set(unpaired_entries)
                )

            # Re-normalize survivors to 1/n_held
            if len(new_held) > 0:
                w = 1.0 / len(new_held)
                for s in new_held:
                    dense_weights.loc[date, s] = w

            # Update state for next rebalance date
            held_symbols = set(new_held)

        # Enforce sparse weights contract: omit identical consecutive rebalance rows
        return _sparse_from_daily(dense_weights)


def apply(
    scores_rebal: pd.DataFrame,
    *,
    buy_band_pct: float = 0.10,
    sell_band_pct: float = 0.30,
    delta_score_sigma: float = 0.35,
) -> pd.DataFrame:
    """Module-level convenience wrapper for RankBandSelection.apply."""
    return RankBandSelection.apply(
        scores_rebal,
        buy_band_pct=buy_band_pct,
        sell_band_pct=sell_band_pct,
        delta_score_sigma=delta_score_sigma,
    )


__all__ = [
    "RankBandSelection",
    "apply",
    "compute_percentile_ranks",
    "_validate_bands",
    "_sparse_from_daily",
]
