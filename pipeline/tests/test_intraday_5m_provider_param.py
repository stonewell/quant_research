"""`intraday_5m_provider` -- a per-strategy pin for the provider that serves 5m BARS.

Why this knob exists (2026-11-13)
---------------------------------
The live strategy's 5m coverage is NOT uniform across providers. Measured on the live
cache: the 20 traded EQUITIES hold 5m bars only under `BaoStockDataProvider` (2,256 rows
each), while the 7 INDICES hold 5m bars only under `FuyaoDataProvider` /
`EastMoneyDataProvider` (BaoStock refuses index intraday queries outright). The 5m
provider used to be derived from the run-level `--data-provider` at three call sites, so
no single run-level value could serve both halves: whichever way the operator set it, one
half arrived only through `load_intraday_5m_universe`'s fallback chain by ACCIDENT.

The pin makes that explicit. It is deliberately scoped to the 5m layer only: the daily
provider, the Stage-1 cache refresh and every other fetch keep reading
`args.data_provider`.

What is pinned here (100% offline: JSON + AST + argument parsing, no data, no network)
--------------------------------------------------------------------------------------
1. `StrategyConfig.intraday_5m_provider` exists and defaults to `None` (= today's
   behaviour: run-level provider + fallback chain).
2. ONLY `chan_four_state_blend` pins it in `strategies_config.json`, and the documented
   deployment pins next to it (`regime_ic_ir_hybrid`, orthogonalization ON) are intact --
   the AGENTS.md 2026-10-22 exception that a `--dump-strategies` pass once silently broke.
3. The resolution chain is the SAME one `enable_intraday_5m` uses
   (`<prefix>_intraday_5m_provider` -> `blend_intraday_5m_provider` ->
   `intraday_5m_provider`), and `resolve_param` also reaches the `StrategyConfig` field.
4. Stage 1 forwards the resolved pin to Stage 2 (`--intraday-5m-provider`) only when it
   resolved one, so every existing argv stays byte-identical; Stage 2 parses it.
5. Wiring guard: both live runners pass the SECOND provider instance (`provider_5m`), not
   `args.data_provider`, to the 5m loader -- and `provider_5m` is built from the resolved
   pin. This is the assertion that fails if someone reverts the wiring while leaving the
   config key in place (a zombie knob).
6. The multi-strategy round-level combiner: no pin -> `None`, one pin -> that pin,
   disagreeing pins -> the FIRST requester's pin plus a loud warning (one 5m load per
   round can only use one provider).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "pipeline")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_CONFIG_PATH = os.path.join(_REPO_ROOT, "pipeline", "research_strategy", "strategies_config.json")
_CFSB_KEY = "chan_four_state_blend"


def _load_script(module_name, relative_path):
    """Loads a standalone script (not an importable package module) via importlib."""
    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def stage1():
    return _load_script(
        "check_live_portfolio_health_5m_provider", "scripts/check_live_portfolio_health.py"
    )


@pytest.fixture(scope="module")
def stage2():
    return _load_script("run_live_strategy_5m_provider", "scripts/run_live_strategy.py")


@pytest.fixture(scope="module")
def multi():
    return _load_script("run_live_multi_strategy_5m_provider", "scripts/run_live_multi_strategy.py")


@pytest.fixture(scope="module")
def cfg_json():
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1. The config field
# ---------------------------------------------------------------------------


def test_strategy_config_declares_the_param_and_defaults_to_none():
    """`None` is the documented "keep today's behaviour" value, not an empty string."""
    from pipeline.research_strategy.rs.config import StrategyConfig

    cfg = StrategyConfig()
    assert cfg.intraday_5m_provider is None
    assert StrategyConfig(intraday_5m_provider="baostock").intraday_5m_provider == "baostock"
    # It must NOT be an alias-synced blend field: there is exactly one spelling, because the
    # value selects a data source for ONE layer and has no `blend_*` twin to keep in sync.
    assert not hasattr(cfg, "blend_intraday_5m_provider")


# ---------------------------------------------------------------------------
# 2. The config pin -- exactly one strategy, next to the other pinned values
# ---------------------------------------------------------------------------


def test_only_chan_four_state_blend_pins_the_5m_provider(cfg_json):
    pinned = {
        key: (entry.get("parameters") or {})["intraday_5m_provider"]
        for key, entry in cfg_json.items()
        if "intraday_5m_provider" in (entry.get("parameters") or {})
    }
    assert pinned == {_CFSB_KEY: "baostock"}, (
        "the 5m provider pin must stay scoped to the deployed live strategy; "
        f"found {sorted(pinned.items())}"
    )
    # `baostock` is the canonical registry token (`common.data._PROVIDER_REGISTRY`), i.e. a
    # name `get_data_provider` can actually instantiate -- not a display label.
    from common.data import _ensure_financial_api_registered, _PROVIDER_REGISTRY

    _ensure_financial_api_registered()
    assert "baostock" in _PROVIDER_REGISTRY


def test_the_pin_did_not_disturb_the_deployed_blend_mode(cfg_json):
    """The 2026-10-22 exception: a dump/config pass once flipped these silently."""
    params = cfg_json[_CFSB_KEY]["parameters"]
    assert params["blend_weighting_mode"] == "regime_ic_ir_hybrid"
    assert params["blend_enable_orthogonalization"] is True
    assert params["cfsb_inertia_relative_ratio"] == 0.30


# ---------------------------------------------------------------------------
# 3. Resolution chain (same shape as `enable_intraday_5m`)
# ---------------------------------------------------------------------------


def test_resolution_chain_prefers_prefix_then_blend_then_bare(stage1, stage2, multi):
    for resolver in (stage1.resolve_blend_param, stage2.resolve_blend_param):
        def resolve(params):
            return resolver(params, "intraday_5m_provider", None, str)

        assert resolve({"cfsb_intraday_5m_provider": "baostock"}) == "baostock"
        assert resolve({"blend_intraday_5m_provider": "fuyao"}) == "fuyao"
        assert resolve({"intraday_5m_provider": "eastmoney"}) == "eastmoney"
        # Precedence: the strategy-prefixed key wins over the bare name.
        assert (
            resolver(
                {"cfsb_intraday_5m_provider": "baostock", "intraday_5m_provider": "fuyao"},
                "intraday_5m_provider",
                None,
                str,
            )
            == "baostock"
        )
        # Unset -> None (NOT "" and NOT the run-level provider: the caller decides).
        assert resolver({}, "intraday_5m_provider", None, str) is None


def test_blend_template_resolver_reaches_the_config_field():
    """`resolve_param`'s last precedence step must surface the StrategyConfig default."""
    from pipeline.research.blend.chan_four_state_blend import ChanFourStateBlendStrategy
    from pipeline.research_strategy.rs.config import StrategyConfig

    unpinned = ChanFourStateBlendStrategy()
    assert unpinned.resolve_param({}, "intraday_5m_provider", None, str) is None

    pinned = ChanFourStateBlendStrategy(StrategyConfig(intraday_5m_provider="baostock"))
    assert pinned.resolve_param({}, "intraday_5m_provider", None, str) == "baostock"
    # An explicit runtime param still outranks the config object.
    assert (
        pinned.resolve_param(
            {"cfsb_intraday_5m_provider": "fuyao"}, "intraday_5m_provider", None, str
        )
        == "fuyao"
    )


# ---------------------------------------------------------------------------
# 4. Stage 1 -> Stage 2 forwarding
# ---------------------------------------------------------------------------


def test_stage1_forwards_the_pin_only_when_one_was_resolved(stage1, tmp_path):
    args = stage1.parse_args(["--output-dir", str(tmp_path / "run")])

    baseline = stage1.build_stage2_command(args)
    assert "--intraday-5m-provider" not in baseline, (
        "an unpinned run must produce the historical argv byte-for-byte"
    )

    forwarded = stage1.build_stage2_command(args, intraday_5m_provider="baostock")
    assert "--intraday-5m-provider" in forwarded
    value = forwarded[forwarded.index("--intraday-5m-provider") + 1]
    assert value == "baostock"
    # It is an ADDITION, not a replacement: the daily provider flag is untouched.
    assert forwarded[forwarded.index("--data-provider") + 1] == args.data_provider
    assert [a for a in forwarded if a != "--intraday-5m-provider" and a != "baostock"] == baseline


def test_stage2_parses_the_forwarded_flag_and_defaults_to_unset(stage2):
    plain = stage2.parse_args([])
    assert plain.intraday_5m_provider is None

    pinned = stage2.parse_args(["--intraday-5m-provider", "baostock"])
    assert pinned.intraday_5m_provider == "baostock"
    # Distinct from the daily provider: the flag never rewrites it.
    assert pinned.data_provider != "baostock" or plain.data_provider == pinned.data_provider


# ---------------------------------------------------------------------------
# 5. Wiring guard -- the 5m loader gets `provider_5m`, built from the pin
# ---------------------------------------------------------------------------


def _find_call(tree, func_name):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name == func_name:
                yield node


def _kwarg_value(call, keyword):
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    return None


@pytest.mark.parametrize(
    "relative_path, loader_caller",
    [
        # Stage 1 / Stage 2 call the loader directly; the multi-strategy round goes
        # through its own seam (`apply_intraday_5m_features` -> `load_intraday_5m_panel`,
        # which forwards the provider it was handed), so the assertion point differs.
        ("scripts/check_live_portfolio_health.py", "load_intraday_5m_universe"),
        ("scripts/run_live_strategy.py", "load_intraday_5m_universe"),
        ("scripts/run_live_multi_strategy.py", "apply_intraday_5m_features"),
    ],
)
def test_every_live_runner_feeds_the_5m_loader_a_dedicated_provider(relative_path, loader_caller):
    """The 5m loading path receives `provider_5m` -- never `args.data_provider` directly.

    DISCLOSED APPROXIMATION: a static wire check. A runtime assertion would have to run a
    full live `main()` against a real cache; this proves only that the call site names the
    dedicated instance, which is the exact regression this knob exists to prevent
    (`provider_5m` silently reverting to the run-level provider while the config key stays
    declared and inert -- the "zombie config" shape P1.2 exists to catch).
    """
    with open(os.path.join(_REPO_ROOT, relative_path), encoding="utf-8") as f:
        tree = ast.parse(f.read())

    calls = list(_find_call(tree, loader_caller))
    assert calls, f"{relative_path} no longer calls {loader_caller}"
    for call in calls:
        value = _kwarg_value(call, "data_provider")
        assert isinstance(value, ast.Name) and value.id == "provider_5m", (
            f"{relative_path}: {loader_caller} must receive `provider_5m`, "
            f"got {ast.dump(value) if value is not None else 'no data_provider kwarg'}"
        )

    if loader_caller == "apply_intraday_5m_features":
        # The seam inside the multi runner forwards whatever provider it was handed.
        for call in _find_call(tree, "load_intraday_5m_universe"):
            value = _kwarg_value(call, "data_provider")
            assert isinstance(value, ast.Name) and value.id == "data_provider", (
                "the multi-strategy seam must forward the provider it was handed, "
                f"got {ast.dump(value) if value is not None else 'no data_provider kwarg'}"
            )

    # ... and `provider_5m` itself must be built from the resolved pin, not a constant.
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "provider_5m" for t in node.targets)
    ]
    assert len(assignments) == 1, f"{relative_path}: expected one `provider_5m = ...`"
    build = assignments[0].value
    assert isinstance(build, ast.Call) and getattr(build.func, "id", None) == "get_data_provider"

    # Follow ONE level of indirection: Stage 1 names the resolved value in an intermediate
    # `_5m_provider_name` (it is reused by the console/anomaly messages) before building the
    # provider from it. The pin must appear on that chain either way.
    rendered_chain = [ast.unparse(build.args[0])]
    simple_assigns = {
        t.id: node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    for name, expr in simple_assigns.items():
        if name in rendered_chain[0]:
            rendered_chain.append(ast.unparse(expr))
    chain = " ".join(rendered_chain)
    assert "intraday_5m_provider" in chain or "round_5m_provider" in chain, (
        f"{relative_path}: provider_5m must be built from the resolved pin, got `{chain}`"
    )
    assert "args.data_provider" in chain, (
        f"{relative_path}: the run-level provider must remain the fallback, got `{chain}`"
    )


# ---------------------------------------------------------------------------
# 6. Multi-strategy round-level combiner
# ---------------------------------------------------------------------------


def test_multi_strategy_combiner_never_silently_picks_between_pins(multi, capsys):
    assert multi.combine_intraday_5m_providers([]) is None
    assert multi.combine_intraday_5m_providers([None, None]) is None
    assert multi.combine_intraday_5m_providers([None, "baostock"]) == "baostock"
    assert multi.combine_intraday_5m_providers(["baostock", " BAOSTOCK "]) == "baostock"

    picked = multi.combine_intraday_5m_providers(["baostock", "fuyao"])
    out = capsys.readouterr().out
    assert picked == "baostock"  # the FIRST requester, never a silent average
    assert "[WARN]" in out and "fuyao" in out and "baostock" in out


def test_multi_strategy_resolver_reports_none_for_templates_without_a_resolver(multi):
    class _StaticTemplate:  # a static allocation template has no `resolve_param`
        pass

    static = _StaticTemplate()
    assert multi.resolve_intraday_5m_provider(static, {"intraday_5m_provider": "baostock"}) is None


def test_multi_strategy_resolver_reads_the_strategy_param(multi):
    from pipeline.research.blend.chan_four_state_blend import ChanFourStateBlendStrategy

    strategy = ChanFourStateBlendStrategy()
    assert multi.resolve_intraday_5m_provider(strategy, {}) is None
    assert (
        multi.resolve_intraday_5m_provider(strategy, {"cfsb_intraday_5m_provider": "baostock"})
        == "baostock"
    )
