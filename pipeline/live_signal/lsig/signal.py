"""Pure, I/O-free logic for `live_signal`: point-in-time truncation of a
universe, extracting the sparse target-weights DataFrame's real rebalance
rows, and turning a (target, reference) weight pair into a concrete buy/
sell/hold rebalance instruction. Kept separate from `run_live_signal.py`'s
CLI orchestration so every rule here is directly unit-testable without any
data loading or strategy reconstruction.
"""

from __future__ import annotations

import pandas as pd

_INSTRUCTION_COLUMNS = ["target_weight", "reference_weight", "delta", "is_new_position", "action"]


def as_of_universe(universe: dict, as_of_date) -> dict:
    """Truncates each symbol's DataFrame to rows with index <= as_of_date.

    This is the one guarantee that makes the tool's output point-in-time
    correct (no lookahead) -- independent of whatever end date the data
    provider actually returned (a provider can return a stray later bar,
    e.g. around a timezone/date-boundary edge case)."""
    return {symbol: df.loc[:as_of_date] for symbol, df in universe.items()}


def latest_rebalance_rows(sparse_weights: pd.DataFrame) -> pd.DataFrame:
    """Every real rebalance row, in order -- the sparse-weights contract
    (see common/README.md §3) puts NaN on every day EXCEPT an actual
    rebalance date, so `dropna(how="all")` isolates just those rows. Same
    idiom `common/reporting.py`'s `format_weights_pct` already uses."""
    return sparse_weights.dropna(how="all")


def compute_rebalance_instruction(
    target: pd.Series, reference: pd.Series, threshold: float = 1e-6
) -> pd.DataFrame:
    """Builds a per-symbol rebalance instruction from a target weight row
    (the strategy's current instruction) and a reference row (either the
    strategy's own previous rebalance, or the user's actual current
    holdings) -- both are symbol -> weight-fraction Series.

    Returns a DataFrame indexed by the union of symbols appearing in either
    input (missing on one side treated as 0.0), columns:
    - `target_weight`, `reference_weight`, `delta` (target - reference)
    - `is_new_position`: reference ~0 and target > threshold (a brand-new
      buy, not just adding to an existing position)
    - `action`: `"buy"` if delta > threshold, `"sell"` if delta <
      -threshold, `"hold"` if |delta| <= threshold and target_weight >
      threshold (an unchanged existing position) -- a symbol at ~0 on BOTH
      sides is dropped entirely (noise, not a signal).

    Sorted by target_weight descending for a stable, readable default order.
    """
    symbols = target.index.union(reference.index)
    t = target.reindex(symbols).fillna(0.0)
    r = reference.reindex(symbols).fillna(0.0)
    delta = t - r

    def _action(target_w: float, delta_w: float) -> str:
        if delta_w > threshold:
            return "buy"
        if delta_w < -threshold:
            return "sell"
        if target_w > threshold:
            return "hold"
        return "none"

    actions = [_action(tw, dw) for tw, dw in zip(t, delta)]
    is_new = (r.abs() <= threshold) & (t > threshold)

    instruction = pd.DataFrame(
        {
            "target_weight": t,
            "reference_weight": r,
            "delta": delta,
            "is_new_position": is_new,
            "action": actions,
        },
        index=symbols,
    )
    instruction = instruction[instruction["action"] != "none"]
    return instruction.sort_values("target_weight", ascending=False)[_INSTRUCTION_COLUMNS]


def top_n_buys(instruction: pd.DataFrame, n: int) -> pd.DataFrame:
    """The `n` buy rows with the largest target_weight (instruction is
    already sorted that way by `compute_rebalance_instruction`, so this is
    just a filter + head)."""
    return instruction[instruction["action"] == "buy"].head(n)
