"""`strategy_dumps/*` round-trip invariant: `params` must be the CONFIG's `parameters`.

Why this file exists
--------------------
The dumps under `pipeline/research_strategy/results/strategy_dumps/` are not
decorative: `scripts/run_live_strategy.py`'s `DEFAULT_STRATEGY_FILE` *is*
`chan_four_state_blend_strategy.json`, and nothing on that live path re-reads
`strategies_config.json`. Whatever a dump says is what a live order ticket is sized
by.

`build_and_dump_strategies_as_backtester_files()` writes that dump straight out of
the config entry (`params = entry_data["parameters"]`, mirrored into
`research_strategy_spec.entry_data.parameters`), so a *clean* dump is exact by
construction. The drift this file guards against is post-processing: on 2026-10-24
`pipeline/scripts/backtest_blend_strategies.sh` Step 2 unconditionally stamped
`blend_weighting_mode=<WEIGHTING_MODE>` (default `rolling_ic_ir`) plus the
unprefixed aliases `weighting_mode` / `enable_orthogonalization` into every
`*blend*.json` after the dump, overwriting the pinned
`chan_four_state_blend` value `regime_ic_ir_hybrid`. Effect on the live path:
`blend_base.resolve_weighting_mode`-equivalent logic at `blend_base.py:445` takes
the `rolling_ic_ir` branch and returns *before* the `regime_*` branch, so the
`bucket_renorm_max_delta` clamp and the thrust hysteresis go inert -- exactly the
AGENTS.md §8 documented catastrophe (and the reason
`test_cfsb_deployed_regime_mode_and_inertia_ratio_config` pins the *config* side;
this file pins the *dump* side, which is the file the live runner actually reads).

What is asserted here
---------------------
1. Every config entry produces a dump, and that dump's `params` is **exactly
   equal** to the config entry's `parameters` -- same keys, same values, no
   injected extras (this is the invariant today's drift violated).
2. `research_strategy_spec.entry_data.parameters` mirrors `params` exactly.
3. No blend dump carries an unprefixed `weighting_mode` /
   `enable_orthogonalization` alias that the config does not declare. Consumers
   (`blend_base.resolve_param` precedence 3, `run_live_strategy.py:1543`,
   `check_live_portfolio_health.py:1144-1152`) all read the PREFIXED
   `blend_*` keys, so the aliases are not just redundant, they are a second
   source of truth that already lied once.
4. For `chan_four_state_blend` specifically: the dumped `blend_weighting_mode` is
   the config-pinned `regime_ic_ir_hybrid` (literal, so a config+shell pair that
   drifts together still fails), `cfsb_inertia_relative_ratio` is 0.30, and the
   dumped `strategy_version` / `base_version` equal the strategy class's own
   `version` / `base_version`.

Determinism / offline: the dumps are REGENERATED into `tmp_path` via the same
function the CLI's `--dump-strategies` calls (never read from the gitignored
`results/strategy_dumps/` directory, so this passes in a clean clone), no market
data, no network, nothing written into the repo.

SCOPE / deliberate limitation: because the assertions run against a freshly
regenerated set, they catch drift in the WRITER (`build_and_dump_strategies_as_backtester_files`)
or in `strategies_config.json` -- not post-hoc stamping applied to the real files
by a shell step. That is on purpose: `backtest_blend_strategies.sh` legitimately
re-stamps `*blend*.json` with variable weighting modes for ablation runs, so
asserting on the live dumps directory would break a supported workflow. The
stamping path itself is guarded by `test_drift_checker_catches_the_shell_step2_stamp`,
which replays the exact 2026-10-24 drift and proves the detectors flag it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_MODULE_PATH = os.path.join(_REPO_ROOT, "pipeline", "research_strategy", "run_research_strategy.py")
_CONFIG_PATH = os.path.join(_REPO_ROOT, "pipeline", "research_strategy", "strategies_config.json")
_DUMP_DIR = os.path.join(_REPO_ROOT, "pipeline", "research_strategy", "results", "strategy_dumps")

# Keys the shell script's Step 2 injected post-dump (both spellings, in `params`
# and in `research_strategy_spec.entry_data.parameters`). None of them is declared
# in `strategies_config.json` for any blend entry, so "present" == "injected".
_UNPREFIXED_BLEND_ALIASES = ("weighting_mode", "enable_orthogonalization")

_CFSB_KEY = "chan_four_state_blend"
_CFSB_PINNED_WEIGHTING_MODE = "regime_ic_ir_hybrid"


def _load_dumper():
    """Import the dump writer by path -- it is a CLI script, not an importable package."""
    spec = importlib.util.spec_from_file_location("_run_research_strategy_under_test", _MODULE_PATH)
    assert spec is not None and spec.loader is not None, f"cannot import {_MODULE_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dumper():
    return _load_dumper()


@pytest.fixture(scope="module")
def config(dumper):
    """Pristine config, deep-copied BEFORE any instantiation touches it.

    A second `load_strategies_config()` call (not a shared reference) so that a
    hypothetical in-place mutation inside `instantiate_strategy_from_config_entry`
    would show up as a difference instead of mutating the expectation too.
    """
    return dumper.load_strategies_config()


@pytest.fixture(scope="module")
def dumps(dumper, config, tmp_path_factory):
    """Regenerate every dump into tmp (never the gitignored real dumps dir)."""
    out_dir = tmp_path_factory.mktemp("strategy_dumps_regenerated")
    dump_cfg = dumper.load_strategies_config()  # separate object from `config`
    written = dumper.build_and_dump_strategies_as_backtester_files(dump_cfg, str(out_dir))
    assert written, "build_and_dump_strategies_as_backtester_files wrote nothing"

    loaded = {}
    for path in written:
        with open(path, "r", encoding="utf-8") as fh:
            loaded[os.path.basename(path)] = json.load(fh)
    return loaded


def _params_drift(config: dict, dumps: dict) -> list:
    """Return one human-readable line per dump whose params drift from the config."""
    problems = []
    for key, entry in config.items():
        dump = dumps[f"{key}_strategy.json"]
        dump_path = os.path.join(_DUMP_DIR, f"{key}_strategy.json")
        expected_params = entry.get("parameters", {})
        actual_params = dump.get("params")

        if actual_params != expected_params:
            missing = sorted(set(expected_params) - set(actual_params or {}))
            extra = sorted(set(actual_params or {}) - set(expected_params))
            changed = sorted(
                k for k in set(expected_params) & set(actual_params or {}) if expected_params[k] != actual_params[k]
            )
            problems.append(
                f"{dump_path} params != {_CONFIG_PATH} parameters "
                f"(missing={missing}, injected={extra}, changed={changed})"
            )
        spec_params = dump.get("research_strategy_spec", {}).get("entry_data", {}).get("parameters")
        if spec_params != expected_params:
            problems.append(
                f"{dump_path} research_strategy_spec.entry_data.parameters does not mirror the config parameters"
            )
        if spec_params != actual_params:
            problems.append(f"{dump_path} research_strategy_spec.entry_data.parameters != params")
    return problems


def _alias_drift(config: dict, dumps: dict) -> list:
    """Return one line per blend dump carrying an unprefixed alias the config never declared."""
    offenders = []
    for key in (k for k in config if "blend" in k):
        declared = set(config[key].get("parameters", {}))
        dump = dumps[f"{key}_strategy.json"]
        injected = [a for a in _UNPREFIXED_BLEND_ALIASES if a in dump["params"] and a not in declared]
        injected_spec = [
            a
            for a in _UNPREFIXED_BLEND_ALIASES
            if a in dump["research_strategy_spec"]["entry_data"]["parameters"] and a not in declared
        ]
        if injected or injected_spec:
            offenders.append(f"{key}: params={injected} entry_data.parameters={injected_spec}")
    return offenders


def _stamp_like_shell_step2(dumps: dict, weighting_mode: str = "rolling_ic_ir", ortho: bool = True) -> dict:
    """Reproduce the post-dump stamping that produced the 2026-10-24 drift.

    Mirrors `pipeline/scripts/backtest_blend_strategies.sh` Step 2 verbatim minus
    `extra_params`: every `*blend*` dump gets `blend_weighting_mode` /
    `blend_enable_orthogonalization` overwritten and the unprefixed aliases added,
    in BOTH `params` and `research_strategy_spec.entry_data.parameters`.
    """
    drifted = json.loads(json.dumps(dumps))  # deep copy
    for name, dump in drifted.items():
        if "blend" not in name:
            continue
        for container in (
            dump.setdefault("params", {}),
            dump.setdefault("research_strategy_spec", {}).setdefault("entry_data", {}).setdefault("parameters", {}),
        ):
            container["blend_weighting_mode"] = weighting_mode
            container["blend_enable_orthogonalization"] = ortho
            container["weighting_mode"] = weighting_mode
            container["enable_orthogonalization"] = ortho
    return drifted


def test_every_config_entry_is_dumped(dumper, config, dumps):
    expected = {f"{key}_strategy.json" for key in config}
    assert set(dumps) == expected, (
        f"dump set mismatch: missing={sorted(expected - set(dumps))} unexpected={sorted(set(dumps) - expected)}"
    )
    assert len(dumps) == len(config)


def test_params_are_exactly_the_config_parameters(dumper, config, dumps):
    """THE invariant: dumped `params` == config `parameters`, key-for-key.

    One test that loops over the config (instead of ~58 parametrized cases) keeps
    collection output readable; the failure message names the offending strategy
    and the exact differing keys.
    """
    problems = _params_drift(config, dumps)
    assert not problems, "strategy dump drift:\n  " + "\n  ".join(problems)


def test_drift_checker_catches_the_shell_step2_stamp(dumper, config, dumps):
    """Proof this file has teeth: replay the exact 2026-10-24 drift and see it caught.

    Without this, the invariant above is only as strong as "the dump function is
    the only writer", which is exactly what was false. The replayed state is the
    real pre-restore one: `chan_four_state_blend` pinned to
    `regime_ic_ir_hybrid` in the config but stamped `rolling_ic_ir` in the dump,
    plus both unprefixed aliases.
    """
    drifted = _stamp_like_shell_step2(dumps)

    params_problems = _params_drift(config, drifted)
    assert params_problems, "checker failed to flag the injected/stamped params -- the invariant has no teeth"
    cfsb_lines = [p for p in params_problems if _CFSB_KEY in p]
    assert cfsb_lines, "checker did not name chan_four_state_blend"
    assert any("blend_weighting_mode" in line for line in cfsb_lines), (
        f"checker did not name the drifted blend_weighting_mode: {cfsb_lines}"
    )

    alias_problems = _alias_drift(config, drifted)
    assert len(alias_problems) == len([k for k in config if "blend" in k]), alias_problems
    assert any(_CFSB_KEY in line for line in alias_problems)


def test_blend_dumps_carry_no_unprefixed_alias_keys(dumper, config, dumps):
    """The shell script's injected aliases must be absent (single source of truth)."""
    assert [k for k in config if "blend" in k], "expected blend entries in strategies_config.json"
    offenders = _alias_drift(config, dumps)
    assert not offenders, (
        "unprefixed blend alias key(s) injected into dumps (post-dump stamping -- likely "
        "pipeline/scripts/backtest_blend_strategies.sh Step 2):\n  " + "\n  ".join(offenders)
    )


def test_blend_weighting_mode_matches_config_pin(dumper, config, dumps):
    """Every blend dump's `blend_weighting_mode` == its config-pinned value."""
    mismatches = []
    for key, entry in config.items():
        if "blend" not in key:
            continue
        pinned = entry.get("parameters", {}).get("blend_weighting_mode")
        dumped = dumps[f"{key}_strategy.json"]["params"].get("blend_weighting_mode")
        if dumped != pinned:
            mismatches.append(f"{key}: config={pinned!r} dumped={dumped!r}")
    assert not mismatches, "blend_weighting_mode drift:\n  " + "\n  ".join(mismatches)


def test_cfsb_dump_pins_deployed_regime_mode(dumper, config, dumps):
    """§8 guard on the DUMP side: the live default file must say `regime_ic_ir_hybrid`."""
    assert _CFSB_KEY in config, f"{_CFSB_KEY} missing from strategies_config.json"
    dump = dumps[f"{_CFSB_KEY}_strategy.json"]

    assert dump["params"]["blend_weighting_mode"] == _CFSB_PINNED_WEIGHTING_MODE, (
        "the dumped chan_four_state_blend file (scripts/run_live_strategy.py's DEFAULT_STRATEGY_FILE) "
        f"carries blend_weighting_mode={dump['params'].get('blend_weighting_mode')!r}; the deployed engine "
        f"must run {_CFSB_PINNED_WEIGHTING_MODE!r}, otherwise blend_base.py returns in the rolling_ic_ir "
        "branch and the bucket_renorm_max_delta clamp / thrust hysteresis are inert"
    )
    assert dump["params"]["blend_weighting_mode"] == config[_CFSB_KEY]["parameters"]["blend_weighting_mode"]
    assert float(dump["params"]["cfsb_inertia_relative_ratio"]) == pytest.approx(0.30)


def test_cfsb_dump_versions_match_class_attributes(dumper, config, dumps):
    """Version metadata in the dump must be the class's own, not a stale/guessy literal."""
    strat_obj = dumper.instantiate_strategy_from_config_entry(_CFSB_KEY, json.loads(json.dumps(config[_CFSB_KEY])))
    dump = dumps[f"{_CFSB_KEY}_strategy.json"]

    assert dump["strategy_version"] == str(strat_obj.version)
    assert dump["base_version"] == str(strat_obj.base_version)
    entry_data = dump["research_strategy_spec"]["entry_data"]
    assert entry_data["version"] == str(strat_obj.version)
    assert entry_data["base_version"] == str(strat_obj.base_version)
    version_info = dump.get("version_info") or {}
    assert version_info.get("version") == str(strat_obj.version)
    assert version_info.get("base_version") == str(strat_obj.base_version)
