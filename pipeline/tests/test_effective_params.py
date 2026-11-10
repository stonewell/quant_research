"""AT8 -- `effective_params.json` provenance layer (offline, synthetic, no network).

The artifact answers "what did this live run really use?" for the parameters the
runner RE-RESOLVES at run time, as a ``{declared, resolved, source}`` triple per
parameter, plus two run-level discrete facts (instantiated sub-strategy classes,
effective blend weighting mode). It is a READ-ONLY projection of
``strategy_dumps/*.json``: these tests pin the pure builder, the strict-JSON rule,
the dump-hash helper (including its explicit ``"MISSING"`` sentinel) and the fact
that building a snapshot never writes the dump it projects.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import pathlib

import numpy as np
import pytest

from common.effective_params import (
    SOURCE_CONFIG,
    SOURCE_DEFAULT,
    SOURCE_DUMP,
    SOURCE_OVERRIDE,
    VALID_SOURCES,
    build_effective_params,
    finite,
    param_entry,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

EXPECTED_TOP_LEVEL_KEYS = {
    "strategy_id",
    "strategy_version",
    "base_version",
    "config_source",
    "strategy_dump_sha256",
    "as_of",
    "run_id",
    "params",
    "sub_strategy_classes",
    "effective_weighting_mode",
    "read_only_projection",
}


def _payload(**overrides):
    kwargs = dict(
        strategy_id="chan_four_state_blend",
        strategy_version="1.8.0",
        base_version="2.0.0",
        config_source="pipeline/research_strategy/results/strategy_dumps/chan_four_state_blend_strategy.json",
        strategy_dump_sha256="0" * 64,
        as_of="2026-11-06",
        run_id="2026-11-06_101500",
        params={},
        sub_strategy_classes={"core": "ChanFourStateExecutionStrategy"},
        effective_weighting_mode="regime_ic_ir_hybrid",
    )
    kwargs.update(overrides)
    return build_effective_params(**kwargs)


def test_build_effective_params_shape():
    payload = _payload(
        params={"min_holding_bars": param_entry(2, 3, SOURCE_DUMP)},
    )
    assert set(payload) == EXPECTED_TOP_LEVEL_KEYS
    assert payload["read_only_projection"] is True
    assert payload["strategy_id"] == "chan_four_state_blend"
    assert payload["params"]["min_holding_bars"] == {
        "declared": 2,
        "resolved": 3,
        "source": "dump",
    }
    assert payload["sub_strategy_classes"] == {"core": "ChanFourStateExecutionStrategy"}
    assert payload["effective_weighting_mode"] == "regime_ic_ir_hybrid"


def test_param_entry_declared_neq_resolved_both_nonempty():
    entry = param_entry(0.25, 0.30, SOURCE_DUMP)
    assert entry["declared"] == 0.25
    assert entry["resolved"] == 0.30
    assert entry["declared"] != entry["resolved"]
    # The point of the artifact: the two are preserved SEPARATELY, never collapsed.
    assert isinstance(entry["declared"], float) and isinstance(entry["resolved"], float)


def test_source_enum_validity():
    for source in (SOURCE_DEFAULT, SOURCE_CONFIG, SOURCE_DUMP, SOURCE_OVERRIDE):
        assert param_entry(None, 1, source)["source"] == source
    assert set(VALID_SOURCES) == {"default", "config", "dump", "override"}
    for bad in ("", "DEFAULT", "dumps", None, 7):
        with pytest.raises(ValueError):
            param_entry(None, 1, bad)
    with pytest.raises(ValueError):
        build_effective_params(
            strategy_id="x",
            strategy_version="1.0.0",
            base_version=None,
            config_source=None,
            strategy_dump_sha256="MISSING",
            as_of="2026-11-06",
            run_id="r",
            params={"bogus": {"declared": None, "resolved": 1, "source": "dumps"}},
        )


# ---------------------------------------------------------------------------
# Dump hashing -- the helper lives in the runner because it READS a file; the
# projection module itself stays pure.
# ---------------------------------------------------------------------------
def _dump_sha_helper():
    from scripts.run_live_strategy import effective_params_dump_sha256

    return effective_params_dump_sha256


def test_sha256_matches_actual_dump_bytes(tmp_path):
    dump_bytes = b'{"template_name": "chan_four_state_blend", "params": {"cfsb_inertia_relative_ratio": 0.30}}'
    dump = tmp_path / "chan_four_state_blend_strategy.json"
    dump.write_bytes(dump_bytes)
    expected = hashlib.sha256(dump_bytes).hexdigest()

    helper = _dump_sha_helper()
    assert helper(str(dump)) == expected
    # A repo-relative descriptor resolves against `repo_root` the same way.
    assert helper(str(dump), str(tmp_path.parent)) != "MISSING"
    assert helper(dump.name, str(tmp_path)) == expected

    payload = _payload(config_source=str(dump), strategy_dump_sha256=helper(str(dump)))
    assert payload["strategy_dump_sha256"] == expected
    assert len(payload["strategy_dump_sha256"]) == 64


def test_projection_never_writes_dump(tmp_path):
    dump_bytes = b'{"template_name": "x", "params": {"cfsb_inertia_relative_ratio": 0.30}}'
    dump = tmp_path / "x_strategy.json"
    dump.write_bytes(dump_bytes)
    before_stat = os.stat(dump)

    helper = _dump_sha_helper()
    payload = _payload(config_source=str(dump), strategy_dump_sha256=helper(str(dump)))
    out = tmp_path / "effective_params.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    after_stat = os.stat(dump)
    assert dump.read_bytes() == dump_bytes
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_size == before_stat.st_size


def test_missing_dump_explicit_missing(tmp_path):
    helper = _dump_sha_helper()
    assert helper(None) == "MISSING"
    assert helper("") == "MISSING"
    # `strategies_config.json:` entries and static templates have no dump artifact.
    assert helper("strategies_config.json:chan_four_state_blend") == "MISSING"
    assert helper("static_template:equal_weight") == "MISSING"
    # A path that does not exist is MISSING, never a fabricated hash.
    assert helper(str(tmp_path / "nope.json")) == "MISSING"
    assert helper("pipeline/research_strategy/results/strategy_dumps/nope.json") == "MISSING"

    payload = _payload(config_source="static_template:equal_weight", strategy_dump_sha256="MISSING")
    assert payload["strategy_dump_sha256"] == "MISSING"


def test_storage_whitelist_contains_filename():
    """Parse `web/livetrading/storage.py` with `ast`.

    Importing the web module would pull `fastapi`/`pydantic` into the pipeline venv
    (a different environment by design), so the two literals are read from source.

    AT8 fix: provenance is written BEFORE the ticket, so it proves only that Stage 2
    STARTED. It must keep the directory listable (`is_run_dir`) while staying OUT of
    `has_stage2_artifact` -- otherwise a run that refuses/crashes after the provenance
    write would be trusted as a completed Stage-2 round.
    """
    source = REPO_ROOT / "web" / "livetrading" / "storage.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("is_run_dir", "has_stage2_artifact"):
            found[node.name] = {
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            }
    assert found["is_run_dir"], "is_run_dir not found in storage.py"
    assert found["has_stage2_artifact"], "has_stage2_artifact not found in storage.py"
    assert "effective_params.json" in found["is_run_dir"]
    assert "effective_params.json" not in found["has_stage2_artifact"]


def test_strict_json_roundtrip():
    payload = _payload(
        params={
            "min_holding_bars": param_entry(2, 3, SOURCE_DUMP),
            # Non-finite numerics are coerced to None by `param_entry`/`finite`, so the
            # strict dump below cannot raise on NaN/Infinity.
            "vol_model": param_entry(None, np.nan, SOURCE_DEFAULT),
            "inertia_relative_ratio": param_entry(None, np.inf, SOURCE_DEFAULT),
        },
        strategy_dump_sha256="MISSING",
    )
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text) == payload
    assert payload["params"]["vol_model"]["resolved"] is None
    assert payload["params"]["inertia_relative_ratio"]["resolved"] is None


def test_wq_alpha_id_regression_shape():
    """The bare-name `resolve_param` collision: global default, NOT `config` provenance.

    `StrategyConfig.wq_alpha_id=101` leaks through the bare-name fallback, so the
    declaration is absent (`None`) and the origin is the global DEFAULT -- calling it
    `config` would misattribute it to `strategies_config.json`.
    """
    entry = param_entry(None, 101, SOURCE_DEFAULT)
    assert entry == {"declared": None, "resolved": 101, "source": "default"}
    assert json.dumps(entry) == '{"declared": null, "resolved": 101, "source": "default"}'
    assert type(entry["resolved"]) is int


def test_inertia_relative_ratio_declared_null_resolved():
    """Incident-2 anchor: declared bare key is null; the prefixed dump key supplied 0.30."""
    entry = param_entry(None, 0.30, SOURCE_DUMP)
    assert entry["declared"] is None
    assert entry["resolved"] == 0.30
    assert entry["source"] == "dump"
    text = json.dumps(entry, allow_nan=False)
    assert json.loads(text) == {"declared": None, "resolved": 0.30, "source": "dump"}
    assert "null" in text


def test_numpy_scalars_coerced():
    entry = param_entry(np.float64(0.25), np.float64(0.30), SOURCE_DUMP)
    assert type(entry["declared"]) is float
    assert type(entry["resolved"]) is float
    int_entry = param_entry(np.int64(2), np.int64(3), SOURCE_DUMP)
    assert type(int_entry["declared"]) is int
    assert type(int_entry["resolved"]) is int
    # The whole payload must be strict-JSON safe after coercion.
    payload = _payload(params={"p": entry, "q": int_entry})
    json.dumps(payload, allow_nan=False)


def test_finite_guard_nonfinite_to_none():
    assert finite(np.nan) is None
    assert finite(np.inf) is None
    assert finite(-np.inf) is None
    assert finite(float("nan")) is None
    assert finite(float("inf")) is None
    assert finite(0.30) == 0.30
    assert param_entry(np.nan, np.inf, SOURCE_DEFAULT) == {
        "declared": None,
        "resolved": None,
        "source": "default",
    }
    assert param_entry(-np.inf, np.nan, SOURCE_DUMP)["declared"] is None


# ---------------------------------------------------------------------------
# End-to-end: the ONE mandatory ordering invariant of the caller.
# `scripts/run_live_strategy.main()` freezes `declared_params = dict(params)` BEFORE
# it writes the resolved `params["min_entry_bars"]` / `params["guard_latency_bars"]`
# back into the same dict. Reading "declared" after that mutation would report the
# RESOLVED value as if the strategy had declared it -- the exact failure this test
# pins. It drives the real `main()` offline (synthetic provider, pinned past session,
# no network) with a static template whose strategy file declares 5 and a
# `--min-entry-bars 3` override.
# ---------------------------------------------------------------------------
def test_main_declared_is_frozen_not_mutated(tmp_path, monkeypatch):
    import scripts.run_live_strategy as runner

    # `equal_weight` is a static basket template: no dump-version axis to satisfy and
    # no blend sub-strategies, so the run is ~1s and fully deterministic.
    strategy_file = tmp_path / "strategy.json"
    strategy_file.write_text(
        json.dumps(
            {"template_name": "equal_weight", "params": {"min_entry_bars": 5}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    # A pinned PAST session is a replay (window gate skipped by design), which keeps the
    # test independent of the host clock and of the market session.
    monkeypatch.delenv("LIVETRADING_PREFER_INSTANCE_STATE", raising=False)
    runner.main(
        [
            "--strategy-file",
            str(strategy_file),
            "--data-provider",
            "synthetic",
            "--as-of-date",
            "2026-11-06",
            "--output-dir",
            str(out_dir),
            "--min-entry-bars",
            "3",
        ]
    )

    artifact = out_dir / "effective_params.json"
    assert artifact.is_file(), "the provenance artifact must still be written"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    entry = payload["params"]["min_entry_bars"]
    # Under the bug (`declared` read AFTER `params["min_entry_bars"] = 3`) both would be 3.
    assert entry["declared"] == 5
    assert entry["resolved"] == 3
    assert entry["source"] == SOURCE_OVERRIDE
    # Atomic write (Fix 3): the temp file must never survive a successful write.
    assert not (out_dir / "effective_params.json.tmp").exists()
