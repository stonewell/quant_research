"""Contract tests for canonical cross-sectional primitives (Track MS, MS11).

Enforces:
1. Reuse map completeness, symbol resolution, and docstring coverage.
2. Legacy fill pinning and strict NaN preservation across all 4 MS1 primitives.
3. AST scan of ``common/*.py`` (excluding tests) preventing duplicate winsorize /
   neutralize / Löwdin / Gaussian-rank implementations or stray ``1.4826`` literals.
4. Layer isolation between ``common/cross_sectional.py`` and the weight/backtester layer.
"""

from __future__ import annotations

import ast
import importlib
import os
import re
import sys
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
import pandas as pd
import pytest

import common.cross_sectional as cs_mod
from common.alpha101_operators import cs_rank, cs_zscore
from common.cross_sectional import (
    PRIMITIVE_REUSE_MAP,
    cs_lowdin_orthogonalize,
    cs_mad_winsorize,
    cs_neutralize_ols,
    cs_rank_gaussian_or_uniform,
)
from common.sector_metadata import sector_neutral_zscore

_COMMON_DIR = Path(_REPO_ROOT) / "common"
_BLEND_BASE = Path(_REPO_ROOT) / "pipeline" / "research" / "blend" / "blend_base.py"

_FORBIDDEN_FUNC_RE = re.compile(
    r"(?i)(winsor|neutral|lowdin|orthogonal|gauss_rank|rank_gauss)"
)

# Explicit allowlist of (relative_path_from_repo_root, func_name) -> justification.
_ALLOWED_FUNC_DEFS: dict[tuple[str, str], str] = {
    ("common/cross_sectional.py", "cs_mad_winsorize"): "MS1 canonical MAD winsorizer",
    (
        "common/cross_sectional.py",
        "cs_rank_gaussian_or_uniform",
    ): "MS1 canonical rank/Gaussian transform",
    ("common/cross_sectional.py", "cs_neutralize_ols"): "MS1 canonical OLS/WLS neutralizer",
    ("common/cross_sectional.py", "cs_lowdin_orthogonalize"): "MS1 canonical factor-level Löwdin",
    ("common/alpha101_operators.py", "cs_indneutralize"): "Frozen legacy sector-only demean",
    ("common/sector_metadata.py", "sector_neutral_zscore"): "Frozen legacy sector-only z-score",
    ("common/sector_metadata.py", "sector_neutral_demean"): "Frozen legacy sector-only demean",
    (
        "common/duckdb_cache.py",
        "is_factor_neutral_symbol",
    ): "Pre-existing price-adjustment-factor helper (index/ETF check, not cross-sectional)",
    (
        "common/financial_api.py",
        "is_factor_neutral_symbol",
    ): "Pre-existing price-adjustment-factor helper (index/ETF check, not cross-sectional)",
}

_ALLOWED_14826_FILES: set[str] = {
    "common/cross_sectional.py",
}


def test_reuse_map_symbols_and_docstring_coverage():
    expected_keys = {
        "cs_mad_winsorize",
        "cs_rank_gaussian_or_uniform",
        "cs_neutralize_ols",
        "cs_lowdin_orthogonalize",
    }
    assert set(PRIMITIVE_REUSE_MAP.keys()) == expected_keys

    required_fields = {
        "kind",
        "reuses",
        "delegates_sector_only_to",
        "nan_mask_reapplied",
        "distinct_from",
    }
    for name, entry in PRIMITIVE_REUSE_MAP.items():
        assert required_fields.issubset(entry.keys()), f"Missing fields in {name}"
        for field in ("reuses", "delegates_sector_only_to"):
            for dotted in entry[field]:
                if dotted.startswith("common."):
                    mod_name, attr_name = dotted.rsplit(".", 1)
                    mod = importlib.import_module(mod_name)
                    assert hasattr(mod, attr_name), f"Unresolved symbol {dotted}"

    doc = cs_mod.__doc__ or ""
    must_mention = [
        "cs_mad_winsorize",
        "cs_rank_gaussian_or_uniform",
        "cs_neutralize_ols",
        "cs_lowdin_orthogonalize",
        "cs_demean",
        "cs_scale",
        "cs_indneutralize",
        "sector_neutral_zscore",
        "sector_neutral_demean",
        "resolve_sector",
        "_resolve_group_labels",
    ]
    for sym in must_mention:
        assert sym in doc, f"{sym!r} missing from common.cross_sectional.__doc__"


def test_legacy_fills_pinned():
    """Pin legacy NaN fill behavior so it is explicit why MS1 wrappers re-apply masks."""
    df = pd.DataFrame([[1.0, np.nan, 3.0]], index=[0], columns=["A", "B", "C"])
    assert cs_rank(df).iloc[0, 1] == 0.5
    assert cs_zscore(df).iloc[0, 1] == 0.0

    s = pd.Series([1.0, np.nan, 3.0], index=["A", "B", "C"])
    sec = {"A": "T", "B": "T", "C": "T"}
    assert sector_neutral_zscore(s, sec).loc["B"] == 0.0


@pytest.mark.parametrize(
    "primitive",
    [
        "cs_mad_winsorize",
        "cs_rank_gaussian_or_uniform",
        "cs_neutralize_ols",
        "cs_lowdin_orthogonalize",
    ],
)
def test_all_primitives_preserve_nan_never_zero_fill(primitive: str):
    rng = np.random.default_rng(1234)
    dates = pd.bdate_range("2025-01-02", periods=5)
    cols = [f"S{i:02d}" for i in range(40)]

    f1 = pd.DataFrame(rng.normal(size=(5, 40)), index=dates, columns=cols)
    f2 = pd.DataFrame(rng.normal(size=(5, 40)), index=dates, columns=cols)
    e1 = pd.DataFrame(rng.normal(size=(5, 40)), index=dates, columns=cols)

    nan_positions = [(0, 0), (1, 5), (2, 10), (3, 15), (4, 20)]
    for r, c in nan_positions:
        f1.iloc[r, c] = np.nan

    if primitive == "cs_mad_winsorize":
        out = cs_mad_winsorize(f1)
    elif primitive == "cs_rank_gaussian_or_uniform":
        out = cs_rank_gaussian_or_uniform(f1, method="gaussian")
    elif primitive == "cs_neutralize_ols":
        out = cs_neutralize_ols(f1, {"e1": e1})
    else:
        out = cs_lowdin_orthogonalize({"f1": f1, "f2": f2})["f1"]

    for r, c in nan_positions:
        val = out.iloc[r, c]
        assert np.isnan(val), f"{primitive} wrote {val!r} instead of NaN at ({r}, {c})"

    valid_mask = f1.notna()
    assert np.isfinite(out.where(valid_mask).to_numpy()[valid_mask.to_numpy()]).all()


def test_ast_scan_no_duplicate_primitives_or_stray_14826():
    unexpected_defs: list[tuple[str, str, int]] = []
    unexpected_14826: list[tuple[str, int]] = []

    for py_path in sorted(_COMMON_DIR.glob("*.py")):
        rel = py_path.relative_to(_REPO_ROOT).as_posix()
        src = py_path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=rel)

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _FORBIDDEN_FUNC_RE.search(node.name):
                    if (rel, node.name) not in _ALLOWED_FUNC_DEFS:
                        unexpected_defs.append((rel, node.name, node.lineno))
            elif isinstance(node, ast.Constant) and isinstance(node.value, float):
                if abs(node.value - 1.4826) < 1e-12:
                    if rel not in _ALLOWED_14826_FILES:
                        unexpected_14826.append((rel, node.lineno))

    assert not unexpected_defs, f"Unlisted cross-sectional defs in common/: {unexpected_defs}"
    assert not unexpected_14826, f"Unlisted 1.4826 constant in common/: {unexpected_14826}"


def test_layer_isolation_and_forbidden_tokens():
    cs_src = (_COMMON_DIR / "cross_sectional.py").read_text(encoding="utf-8")
    for forbidden in (
        "fillna(0",
        "nan_to_num",
        "blend_base",
        "allocation_backtester",
        "allocation_templates",
        "pipeline",
    ):
        assert forbidden not in cs_src, (
            f"Forbidden token {forbidden!r} found in common/cross_sectional.py"
        )

    for target in (
        _COMMON_DIR / "allocation_backtester.py",
        _COMMON_DIR / "allocation_templates.py",
        _BLEND_BASE,
    ):
        tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "cross_sectional" not in alias.name, (
                        f"{target} must not import cross_sectional"
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert "cross_sectional" not in mod, (
                    f"{target} must not import from cross_sectional"
                )
