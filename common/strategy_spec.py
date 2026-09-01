"""Shared, public implementation for reconstructing a live `AllocationTemplate`
from a `strategy.json` (the file `strategy_generator` exports and any project
that re-runs an already-generated, fixed strategy consumes -- `backtester`,
`live_signal`, and any future consumer) and for validating that file's shape.

Moved here from `backtester/run_backtest.py`'s own private `_get_template`/
`_load_strategy_file` so no project has to depend on another project's
private CLI internals to reuse this logic -- `get_template`/`load_strategy_file`
are the one shared implementation every consumer should import.

Because `get_template`'s `research_strategy_spec`/`composite_spec`/
`fundamental_spec`/`bnn_spec` branches reach into sibling projects under
`pipeline/`/`ml/` via bare `import research_strategy...`-style modules, this
module performs its own idempotent `sys.path` bootstrap at import time (same
bootstrap every `run_*.py` entry point in this workspace already does) --
a caller doesn't need to have set this up itself before importing this module.
"""

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
for _group in ("pipeline", "ml"):
    _group_dir = os.path.join(_REPO_ROOT, _group)
    if _group_dir not in sys.path:
        sys.path.insert(0, _group_dir)

from common.allocation_templates import ALLOCATION_TEMPLATES, PatternBasedAllocationTemplate


def get_template(template_name: str, pattern_spec: dict = None, research_strategy_spec: dict = None,
                  composite_spec: dict = None, params: dict = None, fundamental_spec: dict = None,
                  bnn_spec: dict = None):
    """Looks up a static template by name, UNLESS `pattern_spec`,
    `research_strategy_spec`, `composite_spec`, `fundamental_spec`, or
    `bnn_spec` is given (all five are mutually exclusive -- a winning
    strategy.json only ever came from one source).

    `pattern_spec`: a PatternBasedAllocationTemplate (pipeline/pattern_mining/
    pmine/pattern_mining.py) is universe-specific and not zero-arg
    constructible, so it's never in the static ALLOCATION_TEMPLATES registry;
    a strategy.json produced from a winning mined pattern carries its own
    `pattern_spec` (see run_strategygen.py) so it can be reconstructed here
    instead.

    `research_strategy_spec`: a strategy.json produced from a winning
    research_strategy candidate (strategy_generator search) carries its own
    `research_strategy_spec` (`strategy_key` + `entry_data`) so the exact
    research_strategy strategy instance -- class-based or natural-language-
    parsed alike -- can be reconstructed here via research_strategy's own
    `instantiate_strategy_from_config_entry`, instead of being in the static
    ALLOCATION_TEMPLATES registry.

    `composite_spec`: a strategy.json produced from a winning aspect-composed
    hybrid (see pipeline/strategy_generator/stratgen/generator.py's
    GeneratorConfig.enable_aspect_composition) carries its own
    `composite_spec` (`track` plus either `selection_key`/`weighting_key` or
    `entry_key`/`exit_key`) so the exact CompositeAllocationTemplate/
    CompositeTimingTemplate instance can be rebuilt here from
    common.strategy_aspects/research_strategy.rs.timing_aspects's registries.
    `params` (the strategy.json's own top-level saved params) is passed
    through as the reconstructed composite's `default_params` -- required so
    `--optimize`'s fresh grid search (which degenerates to a single `{}`
    trial for a Track B composite, or omits the shared `rebalance_freq_days`
    for a Track A one) still falls back to the actually-tuned values instead
    of each aspect's own generic hardcoded defaults.

    `fundamental_spec`: a strategy.json produced by the separate
    `fundamental_screener` project (real ROE/dividend/earnings-growth/
    leverage buy-sell screening, see `pipeline/fundamental_screener/README.md`)
    carries a trivial `fundamental_spec` marker (`{"source":
    "fundamental_screener"}`) so its `FundamentalMarginOfSafetyStrategy`
    can be reconstructed here -- it's zero-arg constructible, with every
    actual behavior configured via `params` (already loaded from
    strategy.json regardless), so the marker exists only to identify the
    origin/track unambiguously and trigger the right import, not to carry
    any reconstruction data of its own.

    `bnn_spec`: a strategy.json produced by the separate `bnn_forecaster`
    project (AutoBNN probabilistic price forecasting, see
    `ml/bnn_forecaster/README.md`) carries a trivial `bnn_spec` marker
    (`{"source": "bnn_forecaster"}`) so its `BnnForecastStrategy` can be
    reconstructed here -- same zero-arg/params-driven shape as
    `fundamental_spec` above. Reconstructing this template requires
    `bnn_forecaster`'s own isolated `uv` environment (its `autobnn`/`jax`
    dependency chain is NOT installed in this workspace's root venv) -- run
    the caller with `bnn_forecaster`'s venv python for a bnn_spec strategy
    file, not the root one."""
    if research_strategy_spec is not None:
        from research_strategy.rs.strategy import instantiate_strategy_from_config_entry
        return instantiate_strategy_from_config_entry(
            research_strategy_spec["strategy_key"], research_strategy_spec["entry_data"]
        )
    if composite_spec is not None:
        if composite_spec["track"] == "allocation":
            from common.strategy_aspects import SELECTION_ASPECTS, WEIGHTING_ASPECTS, CompositeAllocationTemplate
            return CompositeAllocationTemplate(
                SELECTION_ASPECTS[composite_spec["selection_key"]],
                WEIGHTING_ASPECTS[composite_spec["weighting_key"]],
                default_params=params,
            )
        elif composite_spec["track"] == "timing":
            from research_strategy.rs.timing_aspects import (
                ENTRY_SIGNAL_ASPECTS, EXIT_RISK_ASPECTS, CompositeTimingTemplate,
            )
            return CompositeTimingTemplate(
                ENTRY_SIGNAL_ASPECTS[composite_spec["entry_key"]],
                EXIT_RISK_ASPECTS[composite_spec["exit_key"]],
                default_params=params,
            )
        raise ValueError(f"Unknown composite_spec track: {composite_spec['track']!r} (expected 'allocation' or 'timing')")
    if fundamental_spec is not None:
        from fundamental_screener.fscreen.strategy import FundamentalMarginOfSafetyStrategy
        return FundamentalMarginOfSafetyStrategy()
    if bnn_spec is not None:
        from bnn_forecaster.bnnf.strategy import BnnForecastStrategy
        return BnnForecastStrategy()
    if pattern_spec is not None:
        if not template_name.startswith("pattern_"):
            raise ValueError(
                f"pattern_spec provided but template_name '{template_name}' does not "
                f"start with 'pattern_' -- a pattern_spec block only makes sense for a "
                f"mined pattern-based template; refusing to silently reinterpret "
                f"'{template_name}' as one."
            )
        return PatternBasedAllocationTemplate(
            feature_name=pattern_spec["feature_name"],
            feature_lookback=pattern_spec["feature_lookback"],
            threshold=pattern_spec["threshold"],
            comparison=pattern_spec["comparison"],
            event_type=pattern_spec["event_type"],
            mined_p_value=pattern_spec.get("mined_p_value"),
            mined_n_events=pattern_spec.get("mined_n_events"),
        )
    for cls in ALLOCATION_TEMPLATES:
        if cls.name == template_name:
            return cls()
    raise ValueError(f"Unknown template name: {template_name}")


def load_strategy_file(path: str) -> dict:
    """Loads and validates a strategy.json file exported by strategy_generator.

    Only `template_name` and `params` are truly required (everything else is
    read via `.get()` downstream); validating both up front turns a bare
    `KeyError` deep in a caller's own logic into one clear error naming every
    missing/malformed key at once.
    """
    with open(path, "r") as f:
        strategy_def = json.load(f)

    missing = [key for key in ("template_name", "params") if key not in strategy_def]
    if missing:
        raise ValueError(
            f"Malformed strategy file '{path}': missing required key(s) {missing}. "
            f"Expected keys: template_name (str), params (dict), and optionally "
            f"explanation/trusted/ers_passed/ers_percentile -- see strategy_generator's "
            f"run_strategygen.py output for the expected shape."
        )
    if not isinstance(strategy_def["params"], dict):
        raise ValueError(
            f"Malformed strategy file '{path}': 'params' must be a JSON object, "
            f"got {type(strategy_def['params']).__name__}."
        )

    pattern_spec = strategy_def.get("pattern_spec")
    if pattern_spec is not None:
        required_pattern_keys = ("feature_name", "feature_lookback", "threshold", "comparison", "event_type")
        missing_pattern_keys = [key for key in required_pattern_keys if key not in pattern_spec]
        if missing_pattern_keys:
            raise ValueError(
                f"Malformed strategy file '{path}': 'pattern_spec' is missing required "
                f"key(s) {missing_pattern_keys}. Expected keys: {list(required_pattern_keys)} "
                f"(plus optional mined_p_value/mined_n_events) -- see strategy_generator's "
                f"run_strategygen.py pattern-mining output for the expected shape."
            )

    research_strategy_spec = strategy_def.get("research_strategy_spec")
    if research_strategy_spec is not None:
        missing_research_strategy_keys = [
            key for key in ("strategy_key", "entry_data") if key not in research_strategy_spec
        ]
        if missing_research_strategy_keys:
            raise ValueError(
                f"Malformed strategy file '{path}': 'research_strategy_spec' is missing "
                f"required key(s) {missing_research_strategy_keys}. Expected keys: "
                f"strategy_key (str), entry_data (dict) -- see strategy_generator's "
                f"research_strategy-candidate output for the expected shape."
            )
        if not isinstance(research_strategy_spec["strategy_key"], str):
            raise ValueError(
                f"Malformed strategy file '{path}': 'research_strategy_spec.strategy_key' "
                f"must be a string, got {type(research_strategy_spec['strategy_key']).__name__}."
            )
        if not isinstance(research_strategy_spec["entry_data"], dict):
            raise ValueError(
                f"Malformed strategy file '{path}': 'research_strategy_spec.entry_data' "
                f"must be a JSON object, got {type(research_strategy_spec['entry_data']).__name__}."
            )

    composite_spec = strategy_def.get("composite_spec")
    if composite_spec is not None:
        if composite_spec.get("track") == "allocation":
            required_composite_keys = ("selection_key", "weighting_key")
        elif composite_spec.get("track") == "timing":
            required_composite_keys = ("entry_key", "exit_key")
        else:
            raise ValueError(
                f"Malformed strategy file '{path}': 'composite_spec.track' must be 'allocation' or "
                f"'timing', got {composite_spec.get('track')!r}."
            )
        missing_composite_keys = [key for key in required_composite_keys if key not in composite_spec]
        if missing_composite_keys:
            raise ValueError(
                f"Malformed strategy file '{path}': 'composite_spec' (track={composite_spec.get('track')!r}) "
                f"is missing required key(s) {missing_composite_keys}. Expected keys: track, "
                f"{list(required_composite_keys)} -- see strategy_generator's aspect-composition output "
                f"for the expected shape."
            )

    fundamental_spec = strategy_def.get("fundamental_spec")
    if fundamental_spec is not None and not isinstance(fundamental_spec, dict):
        raise ValueError(
            f"Malformed strategy file '{path}': 'fundamental_spec' must be a JSON object, "
            f"got {type(fundamental_spec).__name__}."
        )

    bnn_spec = strategy_def.get("bnn_spec")
    if bnn_spec is not None and not isinstance(bnn_spec, dict):
        raise ValueError(
            f"Malformed strategy file '{path}': 'bnn_spec' must be a JSON object, "
            f"got {type(bnn_spec).__name__}."
        )

    specs_given = [
        s for s in (pattern_spec, research_strategy_spec, composite_spec, fundamental_spec, bnn_spec)
        if s is not None
    ]
    if len(specs_given) > 1:
        raise ValueError(
            f"Malformed strategy file '{path}': 'pattern_spec', 'research_strategy_spec', "
            f"'composite_spec', 'fundamental_spec', and 'bnn_spec' are mutually exclusive -- a "
            f"strategy.json can only have come from one source, never more than one. Refusing to "
            f"guess which one this file actually means."
        )

    return strategy_def
