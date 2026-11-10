"""Read-only parameter provenance for a live-trading run (AT8).

WHAT THIS IS
============
A run-level observation layer: for every parameter the live runner RE-RESOLVES at
run time, record the triple ``{declared, resolved, source}`` so a run archive can
answer "what did this run really use?" without re-deriving anything.

It is a **read-only projection of ``strategy_dumps/*.json`` (the SSOT)**. This
module never writes anything back -- not to the dump, not to
``strategies_config.json``, not to any strategy/template instance -- and
``resolved`` MUST come from the resolver that actually ran, never from a second,
parallel re-derivation. A value that exists only as an in-memory resolution
(e.g. a CLI override) is described here, not persisted elsewhere.

Pure module: no file I/O, no clock, no network. The caller (``scripts/
run_live_strategy.py``) owns hashing/reading/writing.

SOURCE TAXONOMY
===============
* ``SOURCE_DEFAULT``  -- the resolver's own fallback; nothing declared it.
* ``SOURCE_CONFIG``   -- the value came from ``strategies_config.json:<key>``.
* ``SOURCE_DUMP``     -- the value came from a ``strategy_dumps/*.json`` artifact
  (including its blend-prefixed spelling, e.g. ``cfsb_inertia_relative_ratio``).
* ``SOURCE_OVERRIDE`` -- a CLI flag replaced/overrode what was declared.

WHY DECLARED IS FROZEN BY THE CALLER
====================================
``scripts/run_live_strategy.py`` mutates ``strategy_def["params"]`` in place
(``params["min_entry_bars"] = ...``). Reading "declared" after that mutation
would report the RESOLVED value as if it had been declared. The caller therefore
freezes ``declared_params = dict(params)`` before the first mutation and passes
that frozen mapping here. ``param_entry`` never substitutes a default for an
absent key: `declared` for an absent key stays literal ``None``.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

import numpy as np

SOURCE_DEFAULT = "default"
SOURCE_CONFIG = "config"
SOURCE_DUMP = "dump"
SOURCE_OVERRIDE = "override"

#: The closed source vocabulary. Anything else is a programming error.
VALID_SOURCES = (SOURCE_DEFAULT, SOURCE_CONFIG, SOURCE_DUMP, SOURCE_OVERRIDE)


def _validate_source(source: Any) -> str:
    """Return ``source`` iff it is one of :data:`VALID_SOURCES`, else raise."""
    if source not in VALID_SOURCES:
        raise ValueError(
            f"invalid effective-param source {source!r}; expected one of {VALID_SOURCES}"
        )
    return source


def finite(value: Any) -> Optional[float]:
    """``float`` iff finite, else ``None`` -- same rule as ``advisory_report._json_number``.

    A strict-JSON artifact never carries NaN/Infinity. ``bool`` is not a number
    here (mirrors `_json_number`): a flag belongs in ``bool``-typed params, not in
    a numeric field that a downstream reader would arithmetically compare.
    """
    if value is None or isinstance(value, (bool, np.bool_)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _coerce(value: Any) -> Any:
    """Explicit JSON-safe coercion -- never a bare ``np.float64``/``np.int64`` leak."""
    if value is None:
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return finite(value)
    if isinstance(value, str):
        return value
    return str(value)


def param_entry(declared: Any, resolved: Any, source: str) -> Dict[str, Any]:
    """One provenance triple: ``{"declared": ..., "resolved": ..., "source": ...}``.

    ``declared`` for an absent key is literal ``None`` -- never the default -- so
    "the strategy never wrote this" and "the strategy wrote null" stay
    distinguishable from "the strategy declared 2".
    """
    return {
        "declared": _coerce(declared),
        "resolved": _coerce(resolved),
        "source": _validate_source(source),
    }


def build_effective_params(
    *,
    strategy_id: str,
    strategy_version: Any,
    base_version: Any,
    config_source: Any,
    strategy_dump_sha256: str,
    as_of: Any,
    run_id: str,
    params: Mapping[str, Mapping[str, Any]],
    sub_strategy_classes: Optional[Mapping[str, str]] = None,
    effective_weighting_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the run-level ``effective_params.json`` payload.

    This file is a **read-only projection of ``strategy_dumps/*.json`` (SSOT)**; it
    is never written back, and every ``resolved`` value must come from the
    resolver that actually ran in this process -- never a re-derivation invented
    for the report.

    The two discrete facts are recorded once per run (not per symbol, not inside
    any ticket row): the instantiated sub-strategy class names
    (``sub_strategy_classes``) and the blend weighting mode the engine will
    actually use (``effective_weighting_mode``).
    """
    return {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "base_version": base_version,
        "config_source": config_source,
        "strategy_dump_sha256": strategy_dump_sha256,
        "as_of": as_of,
        "run_id": run_id,
        "params": {
            str(name): param_entry(
                entry.get("declared"), entry.get("resolved"), entry.get("source")
            )
            for name, entry in params.items()
        },
        "sub_strategy_classes": (
            None
            if sub_strategy_classes is None
            else {str(k): str(v) for k, v in sub_strategy_classes.items()}
        ),
        "effective_weighting_mode": effective_weighting_mode,
        "read_only_projection": True,
    }
