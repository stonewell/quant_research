"""Shared output-writing conventions used by every project's `run_*.py`
entrypoint: converting a sparse target-weights DataFrame (see
`common/README.md` §3) to the dense, forward-filled CSV form every project
exports, console preview formatting for the same, and JSON report writing
with a generic non-finite-float guard.
"""

import json
from datetime import datetime
from typing import Union

import numpy as np
import pandas as pd


def to_dense_weights(sparse_weights: pd.DataFrame) -> pd.DataFrame:
    """Sparse target-weights DataFrame (common/README.md §3) -> the dense,
    forward-filled daily series every weights CSV in this workspace exports."""
    return sparse_weights.ffill().fillna(0.0)


def write_dense_weights_csv(sparse_weights: pd.DataFrame, path: str) -> pd.DataFrame:
    """`to_dense_weights` then `.to_csv(path)`. Returns the dense frame written."""
    dense = to_dense_weights(sparse_weights)
    dense.to_csv(path)
    return dense


def format_weights_pct(sparse_weights: pd.DataFrame, n: int, suffix: str = "%") -> pd.DataFrame:
    """Last `n` actual-rebalance rows (dropping all-NaN rows) of a sparse
    target-weights DataFrame, formatted as percentage strings for console
    preview, e.g. '12.3%'. `suffix` is appended to every cell verbatim -- a
    caller may pass '%\\n' to bake a trailing newline into each cell string."""
    recent = sparse_weights.dropna(how="all").tail(n)
    return (recent * 100).round(1).astype(str) + suffix


def utc_timestamp() -> str:
    """Current UTC time as an ISO-8601 string with a trailing 'Z', e.g.
    '2026-08-19T00:00:00.000000Z'."""
    return datetime.utcnow().isoformat() + "Z"


def _sanitize(value):
    """Recursively replace non-finite floats with None so json.dump never
    raises on a NaN/inf anywhere in a nested report structure."""
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


def write_json_report(data: dict, path: str, *, indent: int = 2) -> None:
    """json.dump with every non-finite float anywhere in the structure
    sanitized to null, and default=str for anything else non-JSON-native
    (e.g. a date/Timestamp). For a report where every field is already
    finite, output is identical to a plain `json.dump(data, f, indent=2)`."""
    with open(path, "w") as f:
        json.dump(_sanitize(data), f, indent=indent, default=str)
