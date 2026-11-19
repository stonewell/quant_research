"""Typed Genetic Programming engine for formulaic alpha discovery (Stage 1).

Academic & Quantitative Grounding:
- Formulaic alpha operator algebra: Kakushadze (2016), "101 Formulaic Alphas",
  reused directly from ``common/alpha101_operators.py`` without reimplementation.
- Strongly-typed Genetic Programming (STGP): Montana (1995), separating
  ``panel`` (date x symbol DataFrame) and ``window`` (integer lookback from
  ``{3, 5, 10, 20, 40, 60}``) types so every generated tree is syntactically
  and dimensionally valid by construction.

Critical Safeguards:
1. **Strict NaN & Inf masking after EVERY node**: ``cs_rank`` fills 0.5 and
   ``cs_zscore``/``ts_corr``/``signed_power`` fill 0.0; ``ts_corr`` can also
   emit ``inf`` on constant windows. Every node evaluation immediately maps
   ``±inf -> NaN`` and re-applies the tradability availability mask
   (``NaN`` where ``Close`` is ``NaN``) so untradable assets never get
   fabricated values.
2. **Causal prefix-invariance**: every terminal and operator is strictly
   backward-looking or same-day cross-sectional, so perturbing future rows
   never alters past node outputs.
3. **Deterministic RNG**: uses an explicit ``np.random.default_rng(seed)`` so
   identical seeds produce identical halls of fame.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from common.alpha101_operators import (
    adv,
    cs_demean,
    cs_indneutralize,
    cs_rank,
    cs_scale,
    cs_zscore,
    sigmoid,
    signed_log,
    signed_power,
    ts_argmax,
    ts_argmin,
    ts_corr,
    ts_cov,
    ts_decay_linear,
    ts_delay,
    ts_delta,
    ts_max,
    ts_mean,
    ts_min,
    ts_rank,
    ts_std,
    ts_sum,
    vwap_proxy,
)
from amine.config import DEFAULT_GP_WINDOWS, GPConfig
from amine.fitness import (
    FitnessResult,
    SelectedFactor,
    evaluate_factor_fitness,
    select_top_k_factors,
)

TERMINAL_NAMES: Tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "returns",
    "adv20",
)

# Operator signature types:
# - "u_p"   : (panel) -> panel
# - "u_pw"  : (panel, window) -> panel
# - "b_pp"  : (panel, panel) -> panel
# - "b_ppw" : (panel, panel, window) -> panel
COMMUTATIVE_OPS = frozenset({"add", "mul", "ts_corr", "ts_cov"})


@dataclass(frozen=True)
class OpSpec:
    """Metadata and callable for a typed GP operator."""

    name: str
    arg_types: Tuple[str, ...]
    fn: Callable


def _safe_div(x: pd.DataFrame, y: pd.DataFrame) -> pd.DataFrame:
    return x / y.where(y.abs() > 1e-12)


def _signed_power_half(x: pd.DataFrame) -> pd.DataFrame:
    return signed_power(x, 0.5)


def _signed_power_sq(x: pd.DataFrame) -> pd.DataFrame:
    return signed_power(x, 2.0)


def _build_operator_table(
    sector_groups: Optional[Dict[str, str]] = None,
) -> Dict[str, OpSpec]:
    """Construct the typed operator table reusing ``common.alpha101_operators``."""
    specs = [
        # 12 core ML1 REUSE operators
        OpSpec("ts_sum", ("panel", "window"), ts_sum),
        OpSpec("ts_std", ("panel", "window"), ts_std),
        OpSpec("ts_corr", ("panel", "panel", "window"), ts_corr),
        OpSpec("ts_cov", ("panel", "panel", "window"), ts_cov),
        OpSpec("ts_rank", ("panel", "window"), ts_rank),
        OpSpec("ts_argmax", ("panel", "window"), ts_argmax),
        OpSpec("ts_argmin", ("panel", "window"), ts_argmin),
        OpSpec("ts_decay_linear", ("panel", "window"), ts_decay_linear),
        OpSpec("cs_rank", ("panel",), cs_rank),
        OpSpec("cs_demean", ("panel",), cs_demean),
        OpSpec("cs_zscore", ("panel",), cs_zscore),
        OpSpec("signed_power_half", ("panel",), _signed_power_half),
        OpSpec("signed_power_sq", ("panel",), _signed_power_sq),
        # Additional reused alpha101 operators
        OpSpec("ts_delay", ("panel", "window"), ts_delay),
        OpSpec("ts_delta", ("panel", "window"), ts_delta),
        OpSpec("ts_mean", ("panel", "window"), ts_mean),
        OpSpec("ts_min", ("panel", "window"), ts_min),
        OpSpec("ts_max", ("panel", "window"), ts_max),
        OpSpec("cs_scale", ("panel",), cs_scale),
        OpSpec("cs_indneutralize", ("panel",), lambda x: cs_indneutralize(x, sector_groups)),
        OpSpec("sigmoid", ("panel",), sigmoid),
        OpSpec("signed_log", ("panel",), signed_log),
        # Protected element-wise arithmetic
        OpSpec("add", ("panel", "panel"), lambda x, y: x + y),
        OpSpec("sub", ("panel", "panel"), lambda x, y: x - y),
        OpSpec("mul", ("panel", "panel"), lambda x, y: x * y),
        OpSpec("div", ("panel", "panel"), _safe_div),
        OpSpec("neg", ("panel",), lambda x: -x),
        OpSpec("abs", ("panel",), lambda x: x.abs()),
    ]
    return {s.name: s for s in specs}


OPERATOR_TABLE: Dict[str, OpSpec] = _build_operator_table()


@dataclass(frozen=True)
class GPNode:
    """Immutable node in a strongly-typed GP expression tree.

    - Panel terminal: ``op="term"``, ``value="<terminal_name>"``, ``children=()``
    - Window literal: ``op="win"``, ``value=<int>``, ``children=()``
    - Operator call: ``op="<op_name>"``, ``value=None``, ``children=(...)``
    """

    op: str
    children: Tuple["GPNode", ...] = ()
    value: Optional[object] = None

    def is_terminal(self) -> bool:
        return self.op == "term"

    def is_window(self) -> bool:
        return self.op == "win"

    def depth(self) -> int:
        if not self.children:
            return 1
        return 1 + max(c.depth() for c in self.children)

    def size(self) -> int:
        if not self.children:
            return 1
        return 1 + sum(c.size() for c in self.children)

    def max_window(self) -> int:
        if self.is_window():
            return int(self.value)  # type: ignore[arg-type]
        if self.is_terminal():
            return 20 if self.value == "adv20" else 1
        return max((c.max_window() for c in self.children), default=1)

    def canonical(self) -> str:
        """Deterministic canonical string for memoization and deduplication."""
        if self.is_terminal():
            return str(self.value)
        if self.is_window():
            return str(self.value)
        child_strs = [c.canonical() for c in self.children]
        if self.op in COMMUTATIVE_OPS and len(child_strs) >= 2:
            # First two args are commutative panels (even for ts_corr/ts_cov).
            if child_strs[0] > child_strs[1]:
                child_strs[0], child_strs[1] = child_strs[1], child_strs[0]
        return f"{self.op}({', '.join(child_strs)})"


def build_terminals(ohlcv: Mapping[str, pd.DataFrame]) -> Tuple[Dict[str, pd.DataFrame], pd.DataFrame]:
    """Compute the 8 standard GP terminal panels and the tradability mask.

    Returns
    -------
    Tuple[Dict[str, pd.DataFrame], pd.DataFrame]
        ``(terminals, availability_mask)`` where ``availability_mask`` is True
        wherever ``close`` is finite (not NaN).
    """
    close = ohlcv["close"].astype(float)
    open_ = ohlcv.get("open", close).astype(float).reindex_like(close)
    high = ohlcv.get("high", close).astype(float).reindex_like(close)
    low = ohlcv.get("low", close).astype(float).reindex_like(close)
    volume = ohlcv.get("volume", pd.DataFrame(1.0, index=close.index, columns=close.columns)).astype(float).reindex_like(close)

    avail = pd.DataFrame(
        np.isfinite(close.to_numpy(dtype=float)),
        index=close.index,
        columns=close.columns,
    )

    def _mask(df: pd.DataFrame) -> pd.DataFrame:
        arr = df.to_numpy(dtype=float, copy=True)
        arr[~np.isfinite(arr)] = np.nan
        arr[~avail.to_numpy(dtype=bool)] = np.nan
        return pd.DataFrame(arr, index=close.index, columns=close.columns)

    terminals: Dict[str, pd.DataFrame] = {
        "open": _mask(open_),
        "high": _mask(high),
        "low": _mask(low),
        "close": _mask(close),
        "volume": _mask(volume),
        "vwap": _mask(vwap_proxy(high, low, close)),
        "returns": _mask(close.pct_change()),
        "adv20": _mask(adv(volume, close, 20)),
    }
    return terminals, avail


def _sanitize_panel(
    df: pd.DataFrame,
    avail_arr: np.ndarray,
    index: pd.Index,
    columns: pd.Index,
) -> pd.DataFrame:
    """Map ±inf -> NaN and re-apply the tradability availability mask."""
    arr = df.to_numpy(dtype=float, copy=True)
    arr[~np.isfinite(arr)] = np.nan
    arr[~avail_arr] = np.nan
    return pd.DataFrame(arr, index=index, columns=columns)


def evaluate_node(
    node: GPNode,
    terminals: Mapping[str, pd.DataFrame],
    availability_mask: pd.DataFrame,
    *,
    op_table: Optional[Mapping[str, OpSpec]] = None,
) -> pd.DataFrame:
    """Evaluate a GP expression tree, sanitizing NaN/inf after every node."""
    table = op_table if op_table is not None else OPERATOR_TABLE
    avail_arr = availability_mask.to_numpy(dtype=bool)
    index = availability_mask.index
    columns = availability_mask.columns

    def _eval(n: GPNode):
        if n.is_window():
            return int(n.value)  # type: ignore[arg-type]
        if n.is_terminal():
            return terminals[str(n.value)]
        spec = table[n.op]
        args = [_eval(c) for c in n.children]
        raw = spec.fn(*args)
        return _sanitize_panel(raw, avail_arr, index, columns)

    return _eval(node)


# ---------------------------------------------------------------------------
# Tree generation & genetic operators
# ---------------------------------------------------------------------------

def _random_tree(
    rng: np.random.Generator,
    *,
    max_depth: int,
    windows: Tuple[int, ...],
    op_names: Tuple[str, ...],
    op_table: Mapping[str, OpSpec],
    grow: bool = True,
    force_non_terminal: bool = False,
) -> GPNode:
    """Recursively generate a random typed GP tree."""
    if max_depth <= 1 and not force_non_terminal:
        term = TERMINAL_NAMES[int(rng.integers(0, len(TERMINAL_NAMES)))]
        return GPNode(op="term", value=term)

    if grow and not force_non_terminal and rng.random() < 0.25:
        term = TERMINAL_NAMES[int(rng.integers(0, len(TERMINAL_NAMES)))]
        return GPNode(op="term", value=term)

    op_name = op_names[int(rng.integers(0, len(op_names)))]
    spec = op_table[op_name]
    children: List[GPNode] = []
    for arg_type in spec.arg_types:
        if arg_type == "window":
            w = int(windows[int(rng.integers(0, len(windows)))])
            children.append(GPNode(op="win", value=w))
        else:
            children.append(
                _random_tree(
                    rng,
                    max_depth=max_depth - 1,
                    windows=windows,
                    op_names=op_names,
                    op_table=op_table,
                    grow=grow,
                    force_non_terminal=False,
                )
            )
    return GPNode(op=op_name, children=tuple(children))


def _collect_panel_paths(node: GPNode, path: Tuple[int, ...] = ()) -> List[Tuple[Tuple[int, ...], GPNode]]:
    """Collect (path, subnode) for all panel-typed nodes in ``node``."""
    if node.is_window():
        return []
    result: List[Tuple[Tuple[int, ...], GPNode]] = [(path, node)]
    for idx, child in enumerate(node.children):
        result.extend(_collect_panel_paths(child, path + (idx,)))
    return result


def _collect_all_paths(node: GPNode, path: Tuple[int, ...] = ()) -> List[Tuple[Tuple[int, ...], GPNode]]:
    """Collect (path, subnode) for every node (panel and window) in ``node``."""
    result: List[Tuple[Tuple[int, ...], GPNode]] = [(path, node)]
    for idx, child in enumerate(node.children):
        result.extend(_collect_all_paths(child, path + (idx,)))
    return result


def _replace_at_path(node: GPNode, path: Tuple[int, ...], replacement: GPNode) -> GPNode:
    """Return a new tree with the subtree at ``path`` replaced."""
    if not path:
        return replacement
    head, tail = path[0], path[1:]
    new_children = list(node.children)
    new_children[head] = _replace_at_path(node.children[head], tail, replacement)
    return GPNode(op=node.op, children=tuple(new_children), value=node.value)


def _crossover(
    parent_a: GPNode,
    parent_b: GPNode,
    rng: np.random.Generator,
    *,
    max_depth: int,
    max_nodes: int,
) -> GPNode:
    """Subtree crossover between two panel-typed nodes respecting bloat caps."""
    paths_a = _collect_panel_paths(parent_a)
    paths_b = _collect_panel_paths(parent_b)
    for _ in range(5):
        pa, _ = paths_a[int(rng.integers(0, len(paths_a)))]
        _, sub_b = paths_b[int(rng.integers(0, len(paths_b)))]
        child = _replace_at_path(parent_a, pa, sub_b)
        if not child.is_terminal() and child.depth() <= max_depth and child.size() <= max_nodes:
            return child
    return parent_a


def _mutate(
    node: GPNode,
    rng: np.random.Generator,
    *,
    max_depth: int,
    max_nodes: int,
    windows: Tuple[int, ...],
    op_names: Tuple[str, ...],
    op_table: Mapping[str, OpSpec],
    ops_by_sig: Mapping[Tuple[str, ...], Tuple[str, ...]],
) -> GPNode:
    """Apply subtree, point, or hoist mutation respecting bloat caps."""
    mode = float(rng.random())
    if mode < 0.45:
        # Subtree mutation
        paths = _collect_panel_paths(node)
        path, _ = paths[int(rng.integers(0, len(paths)))]
        rem_depth = max(1, max_depth - len(path))
        sub = _random_tree(
            rng,
            max_depth=min(3, rem_depth),
            windows=windows,
            op_names=op_names,
            op_table=op_table,
            grow=True,
            force_non_terminal=(len(path) == 0),
        )
        cand = _replace_at_path(node, path, sub)
    elif mode < 0.85:
        # Point mutation
        paths = _collect_all_paths(node)
        path, target = paths[int(rng.integers(0, len(paths)))]
        if target.is_window():
            w = int(windows[int(rng.integers(0, len(windows)))])
            cand = _replace_at_path(node, path, GPNode(op="win", value=w))
        elif target.is_terminal():
            t = TERMINAL_NAMES[int(rng.integers(0, len(TERMINAL_NAMES)))]
            cand = _replace_at_path(node, path, GPNode(op="term", value=t))
        else:
            sig = op_table[target.op].arg_types
            same_sig = ops_by_sig[sig]
            new_op = same_sig[int(rng.integers(0, len(same_sig)))]
            cand = _replace_at_path(
                node, path, GPNode(op=new_op, children=target.children)
            )
    else:
        # Hoist mutation: promote a non-terminal subtree to root
        non_term = [
            sub for path, sub in _collect_panel_paths(node)
            if len(path) > 0 and not sub.is_terminal()
        ]
        cand = non_term[int(rng.integers(0, len(non_term)))] if non_term else node

    if not cand.is_terminal() and cand.depth() <= max_depth and cand.size() <= max_nodes:
        return cand
    return node


@dataclass
class _EvalCacheEntry:
    node: GPNode
    canonical: str
    result: FitnessResult
    oriented_panel: Optional[pd.DataFrame]


class GPFactorMiner:
    """Strongly-typed Genetic Programming miner for cross-sectional alpha factors."""

    def __init__(
        self,
        config: Optional[GPConfig] = None,
        *,
        sector_groups: Optional[Dict[str, str]] = None,
    ) -> None:
        self.config = config if config is not None else GPConfig()
        self.op_table = _build_operator_table(sector_groups=sector_groups)
        self.op_names = tuple(self.op_table.keys())
        ops_by_sig: Dict[Tuple[str, ...], List[str]] = {}
        for name, spec in self.op_table.items():
            ops_by_sig.setdefault(spec.arg_types, []).append(name)
        self.ops_by_sig: Dict[Tuple[str, ...], Tuple[str, ...]] = {
            sig: tuple(names) for sig, names in ops_by_sig.items()
        }

    def mine(
        self,
        ohlcv: Mapping[str, pd.DataFrame],
        forward_returns: pd.DataFrame,
        *,
        discovery_dates: Optional[pd.Index] = None,
        seed_library: Optional[Mapping[str, pd.DataFrame]] = None,
    ) -> List[SelectedFactor]:
        """Run GP evolution strictly on ``discovery_dates`` and return top-K factors.

        Parameters
        ----------
        ohlcv : Mapping[str, pd.DataFrame]
            OHLCV panels (must contain ``"close"``; optional ``"open"``,
            ``"high"``, ``"low"``, ``"volume"``).
        forward_returns : pd.DataFrame
            Forward label panel aligned to signal dates.
        discovery_dates : Optional[pd.Index], default None
            Dates allowed for GP fitness evaluation. If provided, both OHLCV
            and ``forward_returns`` are sliced to ``discovery_dates`` BEFORE
            evaluating fitness so no future information can enter selection.
        seed_library : Optional[Mapping[str, pd.DataFrame]], default None
            Existing seed factors used for the correlation penalty and greedy
            top-K novelty filter.

        Returns
        -------
        List[SelectedFactor]
            Up to ``config.top_k`` oriented factors, each carrying its full
            discovery panel and ``factor_ic_summary`` diagnostics.
        """
        cfg = self.config
        rng = np.random.default_rng(cfg.seed)

        if discovery_dates is not None:
            ohlcv_disc = {
                k: v.loc[v.index.intersection(discovery_dates)]
                for k, v in ohlcv.items()
            }
            fwd_disc = forward_returns.loc[forward_returns.index.intersection(discovery_dates)]
            lib_disc = (
                {
                    k: v.loc[v.index.intersection(discovery_dates)]
                    for k, v in seed_library.items()
                }
                if seed_library
                else {}
            )
        else:
            ohlcv_disc = dict(ohlcv)
            fwd_disc = forward_returns
            lib_disc = dict(seed_library) if seed_library else {}

        terminals, avail_mask = build_terminals(ohlcv_disc)
        cache: Dict[str, _EvalCacheEntry] = {}

        def _evaluate(node: GPNode) -> _EvalCacheEntry:
            canon = node.canonical()
            if canon in cache:
                return cache[canon]

            if node.is_terminal() or node.size() <= 1:
                res = FitnessResult(
                    fitness=-np.inf,
                    mean_ic=0.0,
                    std_ic=0.0,
                    daily_ir=0.0,
                    sign=1.0,
                    turnover=1.0,
                    max_corr=0.0,
                    node_count=node.size(),
                    n_obs=0,
                    rejected=True,
                    reject_reason="terminal_only",
                )
                entry = _EvalCacheEntry(node=node, canonical=canon, result=res, oriented_panel=None)
                cache[canon] = entry
                return entry

            try:
                raw_panel = evaluate_node(
                    node, terminals, avail_mask, op_table=self.op_table
                )
            except Exception:  # noqa: BLE001
                res = FitnessResult(
                    fitness=-np.inf,
                    mean_ic=0.0,
                    std_ic=0.0,
                    daily_ir=0.0,
                    sign=1.0,
                    turnover=1.0,
                    max_corr=0.0,
                    node_count=node.size(),
                    n_obs=0,
                    rejected=True,
                    reject_reason="eval_error",
                )
                entry = _EvalCacheEntry(node=node, canonical=canon, result=res, oriented_panel=None)
                cache[canon] = entry
                return entry

            res = evaluate_factor_fitness(
                raw_panel,
                fwd_disc,
                node_count=node.size(),
                library=lib_disc,
                availability_mask=avail_mask,
                lambda_turnover=cfg.lambda_turnover,
                lambda_corr=cfg.lambda_corr,
                lambda_length=cfg.lambda_length,
                max_abs_corr=cfg.max_abs_corr,
                max_nodes=cfg.max_nodes,
                min_assets=cfg.min_assets,
                max_nan_fraction=cfg.max_nan_fraction,
                max_zero_std_fraction=cfg.max_zero_std_fraction,
            )
            oriented = (raw_panel * res.sign) if np.isfinite(res.fitness) else None
            entry = _EvalCacheEntry(node=node, canonical=canon, result=res, oriented_panel=oriented)
            cache[canon] = entry
            return entry

        # Initial population: ramped half-and-half with bloat caps
        population: List[GPNode] = []
        init_max_d = min(4, cfg.max_depth)
        attempts = 0
        while len(population) < cfg.population_size and attempts < cfg.population_size * 10:
            attempts += 1
            d = int(rng.integers(2, max(3, init_max_d + 1)))
            grow = bool(len(population) % 2 == 0)
            tree = _random_tree(
                rng,
                max_depth=d,
                windows=cfg.windows,
                op_names=self.op_names,
                op_table=self.op_table,
                grow=grow,
                force_non_terminal=True,
            )
            if tree.depth() <= cfg.max_depth and tree.size() <= cfg.max_nodes:
                population.append(tree)

        n_elite = max(1, int(round(cfg.population_size * cfg.elitism_ratio)))

        for _gen in range(cfg.n_generations):
            evaluated = [_evaluate(ind) for ind in population]
            evaluated.sort(key=lambda e: (-e.result.fitness, e.canonical))

            next_pop: List[GPNode] = [e.node for e in evaluated[:n_elite]]
            t_size = min(cfg.tournament_size, len(evaluated))

            def _tournament() -> GPNode:
                idxs = rng.integers(0, len(evaluated), size=t_size)
                best = evaluated[int(idxs[0])]
                for idx in idxs[1:]:
                    cand = evaluated[int(idx)]
                    if cand.result.fitness > best.result.fitness:
                        best = cand
                return best.node

            while len(next_pop) < cfg.population_size:
                p1 = _tournament()
                if rng.random() < cfg.crossover_prob:
                    p2 = _tournament()
                    child = _crossover(
                        p1, p2, rng, max_depth=cfg.max_depth, max_nodes=cfg.max_nodes
                    )
                else:
                    child = p1

                if rng.random() < cfg.mutation_prob:
                    child = _mutate(
                        child,
                        rng,
                        max_depth=cfg.max_depth,
                        max_nodes=cfg.max_nodes,
                        windows=cfg.windows,
                        op_names=self.op_names,
                        op_table=self.op_table,
                        ops_by_sig=self.ops_by_sig,
                    )
                next_pop.append(child)

            population = next_pop

        # Evaluate final population to populate cache completely
        for ind in population:
            _evaluate(ind)

        # Build deduplicated candidate list from cache
        candidates: List[SelectedFactor] = []
        for idx, entry in enumerate(
            sorted(cache.values(), key=lambda e: (-e.result.fitness, e.canonical))
        ):
            if entry.oriented_panel is None or not np.isfinite(entry.result.fitness):
                continue
            candidates.append(
                SelectedFactor(
                    name=f"gp_{idx:03d}",
                    expression=entry.canonical,
                    sign=entry.result.sign,
                    fitness=entry.result.fitness,
                    node_count=entry.node.size(),
                    max_window=entry.node.max_window(),
                    panel=entry.oriented_panel,
                    fitness_result=entry.result,
                    node=entry.node,
                )
            )

        selected = select_top_k_factors(
            candidates,
            top_k=cfg.top_k,
            max_abs_corr=cfg.max_abs_corr,
            seed_library=lib_disc,
            min_assets=cfg.min_assets,
            forward_returns_df=fwd_disc,
        )
        # Re-number accepted factors cleanly gp_00 .. gp_{K-1}
        for rank_idx, factor in enumerate(selected):
            factor.name = f"gp_{rank_idx:02d}"
        return selected

    def evaluate_expression_on_panel(
        self,
        node: GPNode,
        ohlcv: Mapping[str, pd.DataFrame],
        *,
        sign: float = 1.0,
    ) -> pd.DataFrame:
        """Evaluate a discovered ``GPNode`` on any OHLCV panel (e.g. combiner/holdout)."""
        terminals, avail_mask = build_terminals(ohlcv)
        raw = evaluate_node(node, terminals, avail_mask, op_table=self.op_table)
        return raw * float(sign)
