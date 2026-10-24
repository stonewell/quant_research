"""P1.2 -- AST "config wiring" (anti-zombie-config) static analyzer.

Why this exists
---------------
Six-defect parity audit, Defect 6: a `StrategyConfig` / `ScreenerConfig` field can be
declared, given a default, commented at length, persisted into
`strategies_config.json`, round-tripped into `results/strategy_dumps/*.json` ... and
still never be READ by any business code path. Nothing fails: the knob is simply
inert, and the live behaviour silently disagrees with the documentation the operator
tuned against. `--health-report-file` before T0.3 was the same disease on the CLI
surface. This module turns both into a build-time failure.

What is analysed
----------------
1. `StrategyConfig` (`pipeline/research_strategy/rs/config.py`, 615 fields) and
   `ScreenerConfig` (`pipeline/fundamental_screener/fscreen/config.py`, 22 fields):
   every annotated class-body attribute must have at least one *consumer* reference
   in business code.
2. CLI flags declared by the live/research main entries (`parser.add_argument`):
   every flag's `dest` must actually be read back.

What counts as a consumer reference (DELIBERATELY, DISCLOSED APPROXIMATIONS)
---------------------------------------------------------------------------
Business code = every `.py` file under `_SCAN_ROOTS` that is NOT a test
(`<project>/tests/`, `test_*.py`, `conftest.py`) and NOT the declaring config module
itself. The config module is excluded on purpose: `validate()` / `from_dict()` /
`__post_init__` referring to `self.<field>` proves the value is *validated* and
*serialised*, never that any trading path consumes it -- counting those would make
the detector blind to exactly the defect it is meant to catch.

Accepted reference forms:
  (a) attribute access                 -> `cfg.rebalance_freq_days`
  (b) constant-key subscript           -> `params["rebalance_freq_days"]`
  (c) dynamic-resolver call arguments  -> `p.get("x")`, `p.setdefault("x")`,
      `p.pop("x")`, `getattr(cfg, "x")`, `self.resolve_param(p, "x", ...)`
  (d) aspect-registry params keys      -> `EntrySignalAspect(symbol_param_key="rsi_symbol")`

(c) is how the 6 blend-strategy families actually read their knobs:
`BaseBlendAllocationStrategy.resolve_param(p, "<bare_name>", ...)` resolves
`getattr(cfg, f"{param_prefix}_{name}")` and `getattr(cfg, f"blend_{name}")`
dynamically, so `cfsb_dd_reduce_thresh` / `cfssb_target_vol` / `crb_stop_atr_mult` /
`blend_target_vol` are consumed by a `resolve_param(p, "dd_reduce_thresh")`-style call
that never spells the prefixed name. `_bare_field_variants()` maps a prefixed field
back to its bare name for this lookup; `test_resolve_param_dynamic_fallback_unchanged`
asserts that resolver still behaves the way this reasoning assumes, so the exemption
cannot rot silently if `blend_base.py` is refactored.

Known blind spots (explicitly NOT silently skipped -- see report in P1.2):
  * `<sym>_symbol` fields (`rsi_symbol`, `swing_symbol`, `chan_symbol`,
    `chan3_symbol`, `chanm_symbol`, `turtle_symbol`, `grid_symbol`,
    `ensemble_symbol`): the strategies resolve their traded symbol from the UNIVERSE,
    not from the cfg attribute. `symbol_param_key="rsi_symbol"` in
    `rs/timing_aspects.py` is a key into the per-run `params` dict, so the *name*
    survives on the dynamic surface while `StrategyConfig.rsi_symbol` itself is never
    read. Statically we can prove the name is referenced; we CANNOT prove which
    object (cfg vs. params dict) that dynamic key binds to. Classified as a
    `symbol_param_key` ref (form (d)), not as a cfg read -- which is why
    `grid_symbol` / `ensemble_symbol`, which have no aspect entry at all, land in the
    exemption ledger instead.
  * Prefixed-family fields are matched by bare name, so the analyzer cannot tell
    WHICH strategy reads `blend_target_vol` -- only that some `resolve_param` call
    makes it reachable. Proving per-strategy reachability would require symbolic
    execution of the params dict, which is out of scope for a static test.
  * A same-named attribute on a non-config object (`self.top_k` on some unrelated
    class) counts as a reference. Accepted: this analyzer is a zombie detector, not a
    type checker; a false "consumed" only weakens one field, while a false "zombie"
    would be a red build.

Nothing here deletes or mutates a config field: unread fields are either wired up
(recommended, out of P1.2 scope) or recorded in `_EXEMPT_FIELDS` with a reason that
names the exact defect, so a reviewer can act on it.

100% offline: pure `ast` parsing of files already in the repo, no imports of the
analysed modules, no network, no writes.
"""

from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ---------------------------------------------------------------------------
# Analyzer configuration
# ---------------------------------------------------------------------------

_SCAN_ROOTS = ("pipeline", "common", "backtester", "scripts", "web")
_SKIP_DIR_NAMES = {"__pycache__", ".venv", ".git", "node_modules", ".pytest_cache"}

# Prefix families read through `resolve_param`'s dynamic `getattr(cfg, f"..."...)`
# fallback (`blend_base.py`). Longest first, so `cfssb_foo` is not truncated to
# `cfsb_foo`-style mismatches.
_DYNAMIC_PARAM_PREFIXES = ("cfssb_", "cfsb_", "ccsb_", "cdhb_", "crb_", "blend_")

# (call name) -> positional index of the string argument that names a config field.
_DYNAMIC_RESOLVER_ARG_INDEX = {"get": 0, "setdefault": 0, "pop": 0, "getattr": 1, "resolve_param": 1}

# Keyword arguments whose string value is a params-dict key rather than a cfg read.
_DYNAMIC_KEY_KEYWORDS = {"symbol_param_key"}

# CLI entry points whose flags must all be consumed. (P1.2 scope: live + research
# main entries; the shared `common/cli_utils.py` flag builders are consumed by every
# entry script and are out of scope.)
_CLI_ENTRY_POINTS = (
    "scripts/check_live_portfolio_health.py",
    "scripts/run_live_strategy.py",
    "pipeline/research_strategy/run_research_strategy.py",
    "pipeline/fundamental_screener/run_fundamental_screener.py",
)

# Shared CLI helpers whose own `getattr(args, "<dest>", ...)` reads count as
# consuming a flag declared by an entry script (see `_read_cli_dests`).
_SHARED_CLI_CONSUMERS = ("common/cli_utils.py",)


@dataclass(frozen=True)
class ConfigSpec:
    class_name: str
    relative_path: str
    min_expected_fields: int


_CONFIG_SPECS = (
    # min_expected_fields is an anti-vacuity guard: if a refactor moves the class or
    # changes the annotation style, the analyzer would otherwise "pass" with zero
    # fields found -- a green test that checks nothing.
    ConfigSpec("StrategyConfig", "pipeline/research_strategy/rs/config.py", 500),
    ConfigSpec("ScreenerConfig", "pipeline/fundamental_screener/fscreen/config.py", 20),
)


# ---------------------------------------------------------------------------
# Zombie / exemption ledger  (P1.2 -- read the reasons, do not "just" delete)
#
# Every entry below was produced by the analyzer, then verified by hand:
# `grep -rn <field>` over the repo. Format:
#   kind      -- "zombie" (no business read site at all)
#   refs      -- every reference that DOES exist, with file:line
#   verdict   -- KEEP+WIRE or DELETE, i.e. what should happen to the field
#   reason    -- why that verdict follows from the reference evidence
# ---------------------------------------------------------------------------

_EXEMPT_FIELDS: Dict[Tuple[str, str], Dict[str, str]] = {
    ("StrategyConfig", "cfsb_inertia_relative_ratio"): {
        "kind": "zombie",
        "refs": (
            "declaration pipeline/research_strategy/rs/config.py:630 (12-line 'Fix 7' comment); "
            "pipeline/research_strategy/strategies_config.json:401; "
            "results/strategy_dumps/chan_four_state_blend_strategy.json:56,108; "
            "tests/test_blend_track_c.py:472-488 (test-only: asserts the default and does "
            "the ratio*cap arithmetic ITSELF -- never exercises a reader); "
            "one PROSE comment in pipeline/research/blend/chan_four_state_blend.py:35"
        ),
        "verdict": "KEEP the field, IMPLEMENT the read (do NOT delete)",
        "reason": (
            "Real defect, not clutter: the declaration and strategies_config.json both "
            "advertise a SELL-trim hurdle of max(min_weight_change, ratio*current_weight) "
            "(0.30*0.18 = 0.054, meant to filter the 0.0501 micro-trim from the 2026-10-22 "
            "incident), but NO code multiplies by it. `common/allocation_templates.py:"
            "apply_asset_inertia` only applies `min_weight_change` plus a hard-coded 0.6 "
            "asymmetric-sell factor, and `chan_four_state_blend` only reads "
            "`min_weight_change`. Live micro-trim filtering is therefore WEAKER than the "
            "config documentation claims. Deleting the field would silently drop a "
            "documented live safety knob, so it stays (wiring it up is out of P1.2 scope)."
        ),
    },
    ("StrategyConfig", "grid_symbol"): {
        "kind": "zombie",
        "refs": (
            "declaration pipeline/research_strategy/rs/config.py:171; "
            "pipeline/research_strategy/tests/test_strategy.py:1093 "
            "(`test_grid_ignores_stale_symbol_param_and_trades_universe_instead` asserts "
            "the field is IGNORED)"
        ),
        "verdict": "DELETE the field (recommended), keep the regression assertion as a comment",
        "reason": (
            "No read site and no params-key path: `AdaptiveGridStrategy` resolves its "
            "traded symbol from the universe, and unlike `rsi_symbol`/`chan_symbol` this "
            "name is not even registered as a `symbol_param_key`, so it is not reachable "
            "through aspect composition either. The single reference is a test that pins "
            "the field's USELESSNESS -- i.e. the test documents a dead field instead of a "
            "behaviour. Deletion is out of P1.2 scope (no config-field deletions), so it "
            "is exempted with this verdict rather than silently tolerated."
        ),
    },
    ("StrategyConfig", "ensemble_symbol"): {
        "kind": "zombie",
        "refs": (
            "declaration pipeline/research_strategy/rs/config.py:193; "
            "pipeline/research_strategy/tests/test_strategy.py:1150 "
            "(`test_ensemble_ignores_stale_symbol_param_and_trades_universe_instead` "
            "asserts the field is IGNORED)"
        ),
        "verdict": "DELETE the field (recommended), keep the regression assertion as a comment",
        "reason": (
            "Same shape as `grid_symbol`: `EnsembleRegimeSwitchingStrategy` symbols come "
            "from the universe, no `symbol_param_key` registers this name, and the only "
            "reference is a test asserting the value has no effect. A field whose sole "
            "documented property is 'ignored' is a trap for an operator editing "
            "strategies_config.json; delete it rather than keep the illusion. Out of "
            "P1.2 scope (no deletions), hence exempted."
        ),
    },
}


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    path: str  # repo-relative POSIX-ish path
    lineno: int
    kind: str  # "attr" | "subscript" | "resolver" | "symbol_param_key"


def _iter_python_files(roots: Sequence[str]) -> Iterable[str]:
    for root in roots:
        base = os.path.join(_REPO_ROOT, root)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIR_NAMES)
            for filename in sorted(filenames):
                if filename.endswith(".py"):
                    yield os.path.join(dirpath, filename)


def _rel(path: str) -> str:
    return os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/")


def _is_test_path(rel_path: str) -> bool:
    parts = rel_path.split("/")
    if "tests" in parts:
        return True
    name = parts[-1]
    return name.startswith("test_") or name == "conftest.py"


def _parse(path: str) -> Optional[ast.AST]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return ast.parse(fh.read(), filename=path)
    except (SyntaxError, UnicodeDecodeError):
        return None


def _declared_fields(tree: ast.AST, class_name: str) -> Dict[str, int]:
    """Annotated class-body attributes of `class_name` -> {field: lineno}."""
    fields: Dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields[stmt.target.id] = stmt.lineno
    return fields


def _call_name(node: ast.Call) -> Optional[str]:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _str_const(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _bare_field_variants(field: str) -> List[str]:
    """`cfsb_dd_reduce_thresh` -> ["cfsb_dd_reduce_thresh", "dd_reduce_thresh"].

    `resolve_param` looks a bare name up as `p["<prefix>_<name>"]`, `p["blend_<name>"]`,
    `p["<name>"]`, then `getattr(cfg, f"{param_prefix}_{name}")`, `getattr(cfg,
    f"blend_{name}")`, `getattr(cfg, name)` -- so a bare-name call IS a read of every
    prefix variant of that name.
    """
    variants = [field]
    for prefix in _DYNAMIC_PARAM_PREFIXES:
        if field.startswith(prefix):
            variants.append(field[len(prefix):])
            break
    return variants


def _collect_name_refs(
    files: Iterable[str], names: Set[str], decl_path: str
) -> Tuple[Dict[str, Set[Ref]], Dict[str, Set[Ref]]]:
    """Splits every reference to `names` into (business refs, test/decl refs).

    `attr` / `subscript` references must spell the field name EXACTLY. Dynamic
    resolver calls and aspect-registry keys are additionally matched against each
    field's bare variant (`_bare_field_variants`), because that is literally how
    `resolve_param` resolves them; such a hit is recorded under the PREFIXED field
    name, so the caller sees "this field is read" without having to re-derive it.
    """
    business: Dict[str, Set[Ref]] = {}
    non_business: Dict[str, Set[Ref]] = {}
    decl_abs = os.path.abspath(os.path.join(_REPO_ROOT, decl_path))

    # bare/alias name -> prefixed field names reachable through the dynamic lookup
    variant_index: Dict[str, Set[str]] = {}
    for field in names:
        for variant in _bare_field_variants(field):
            variant_index.setdefault(variant, set()).add(field)

    for path in files:
        rel = _rel(path)
        is_decl = os.path.abspath(path) == decl_abs
        bucket = non_business if (is_decl or _is_test_path(rel)) else business
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            hits: List[Tuple[str, str]] = []
            if isinstance(node, ast.Attribute):
                if node.attr in names:
                    hits.append((node.attr, "attr"))
            elif isinstance(node, ast.Subscript):
                key = _str_const(node.slice)  # py>=3.9: Constant / Index-free
                if key in names:
                    hits.append((key, "subscript"))
            elif isinstance(node, ast.Call):
                call = _call_name(node)
                index = _DYNAMIC_RESOLVER_ARG_INDEX.get(call)  # type: ignore[arg-type]
                if index is not None and len(node.args) > index:
                    key = _str_const(node.args[index])
                    # dynamic: a bare name reaches every prefixed variant of it
                    for field in variant_index.get(key, ()):
                        hits.append((field, "resolver"))
                for keyword in node.keywords:
                    if keyword.arg in _DYNAMIC_KEY_KEYWORDS:
                        key = _str_const(keyword.value)
                        for field in variant_index.get(key, ()):
                            hits.append((field, "symbol_param_key"))
            for field, kind in hits:
                bucket.setdefault(field, set()).add(Ref(rel, node.lineno, kind))
    return business, non_business


def _consumed_fields(fields: Iterable[str], business_refs: Dict[str, Set[Ref]]) -> Set[str]:
    return {field for field in fields if field in business_refs}


def _analyze_config(spec: ConfigSpec) -> Tuple[Dict[str, int], Set[str], Dict[str, Set[Ref]], Dict[str, Set[Ref]]]:
    decl_abs = os.path.join(_REPO_ROOT, spec.relative_path)
    tree = _parse(decl_abs)
    assert tree is not None, f"cannot parse declaring module {spec.relative_path}"
    fields = _declared_fields(tree, spec.class_name)
    assert len(fields) >= spec.min_expected_fields, (
        f"{spec.class_name}: parsed only {len(fields)} annotated fields from "
        f"{spec.relative_path} (expected >= {spec.min_expected_fields}). The analyzer "
        f"would otherwise pass vacuously -- fix the field parser, do not lower this bound."
    )
    files = list(_iter_python_files(_SCAN_ROOTS))
    business, non_business = _collect_name_refs(files, set(fields), spec.relative_path)
    return fields, _consumed_fields(fields, business), business, non_business


# ---------------------------------------------------------------------------
# 1. Config fields must be wired
# ---------------------------------------------------------------------------


def _unwired_config_fields(spec: ConfigSpec) -> Dict[str, Dict[str, object]]:
    """CONFIG -> {unwired field: {lineno, non-business refs}} (exemptions NOT applied)."""
    fields, consumed, _business, non_business = _analyze_config(spec)
    unwired: Dict[str, Dict[str, object]] = {}
    for name, lineno in fields.items():
        if name in consumed:
            continue
        unwired[name] = {
            "lineno": lineno,
            "other_refs": sorted(f"{r.path}:{r.lineno}({r.kind})" for r in non_business.get(name, ())),
        }
    return unwired


@pytest.mark.parametrize("spec", _CONFIG_SPECS, ids=lambda s: s.class_name)
def test_every_config_field_has_a_business_read_site(spec: ConfigSpec):
    """No zombie config fields: every declared field must be read by business code.

    Adding a `StrategyConfig` field that nothing consumes fails HERE (see the
    synthetic control below, which proves the failure mode is reachable).
    """
    unwired = _unwired_config_fields(spec)
    exempt = {name for (cls, name) in _EXEMPT_FIELDS if cls == spec.class_name}
    zombies = {name: info for name, info in unwired.items() if name not in exempt}

    assert not zombies, (
        f"{spec.class_name}: {len(zombies)} config field(s) are declared but never read by "
        f"any business code path (Defect 6 -- zombie config). Either wire the field into "
        f"the logic that is supposed to consume it, delete it, or add it to "
        f"_EXEMPT_FIELDS with a reason naming the exact reference evidence:\n"
        + "\n".join(
            f"  - {name} (declared at {spec.relative_path}:{info['lineno']}); "
            f"non-business refs: {info['other_refs'] or 'none'}"
            for name, info in sorted(zombies.items())
        )
    )


def test_exemption_ledger_is_auditable_and_not_stale():
    """Every exemption must name a still-declared field and carry real evidence."""
    for (class_name, field), entry in _EXEMPT_FIELDS.items():
        spec = next((s for s in _CONFIG_SPECS if s.class_name == class_name), None)
        assert spec is not None, f"exemption for unknown class {class_name!r}"
        tree = _parse(os.path.join(_REPO_ROOT, spec.relative_path))
        fields = _declared_fields(tree, class_name)
        assert field in fields, (
            f"exemption for {class_name}.{field} is STALE: the field no longer exists. "
            "Remove the exemption (an exemption must never mask a deleted field)."
        )
        for key in ("kind", "refs", "verdict", "reason"):
            assert entry.get(key), f"exemption {class_name}.{field} is missing {key!r}"
        assert len(entry["reason"]) > 80, (
            f"exemption {class_name}.{field} needs a concrete reason (which business path "
            "is / is not consuming it), not a placeholder"
        )
        # An exempt field must be genuinely unwired -- otherwise the exemption is
        # dead weight that hides nothing and should be deleted.
        assert field in _unwired_config_fields(spec), (
            f"exemption for {class_name}.{field} is obsolete: the field is now consumed. "
            "Delete the exemption."
        )


# ---------------------------------------------------------------------------
# 2. CLI flags must be read back
# ---------------------------------------------------------------------------


def _declared_cli_flags(path: str) -> Dict[str, int]:
    tree = _parse(os.path.join(_REPO_ROOT, path))
    assert tree is not None, f"cannot parse CLI entry point {path}"
    flags: Dict[str, int] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "add_argument":
            continue
        option_strings = [v for v in (_str_const(a) for a in node.args) if v and v.startswith("--")]
        if not option_strings:
            continue  # positional argument
        dest = None
        for keyword in node.keywords:
            if keyword.arg == "dest":
                dest = _str_const(keyword.value)
        dest = dest or option_strings[0][2:].replace("-", "_")
        flags[dest] = node.lineno
    return flags


def _read_cli_dests(path: str, extra_sources: Sequence[str] = ()) -> Set[str]:
    """`dest` names actually read back: `args.x`, `getattr(args, "x")`, `args["x"]`.

    `extra_sources` covers the disclosed delegation: a flag declared in an entry
    script can be consumed by a shared helper the entry script hands `args` to --
    e.g. `--allow-stale-prices` is read by
    `common/cli_utils.resolve_live_clock_context()` (`getattr(args,
    "allow_stale_prices", False)`), not by the runner itself. Without this, the check
    would report a false dangling flag.
    """
    read: Set[str] = set()
    for source in (path, *extra_sources):
        tree = _parse(os.path.join(_REPO_ROOT, source))
        assert tree is not None, f"cannot parse {source}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                read.add(node.attr)
            elif isinstance(node, ast.Subscript):
                key = _str_const(node.slice)
                if key:
                    read.add(key)
            elif isinstance(node, ast.Call) and _call_name(node) in {"getattr", "get", "setdefault", "pop"}:
                index = _DYNAMIC_RESOLVER_ARG_INDEX[_call_name(node)]  # type: ignore[index]
                if len(node.args) > index:
                    key = _str_const(node.args[index])
                    if key:
                        read.add(key)
    return read


@pytest.mark.parametrize("entry_point", _CLI_ENTRY_POINTS)
def test_cli_flags_are_consumed(entry_point: str):
    """No dangling CLI flags (the pre-T0.3 `--health-report-file` failure mode)."""
    flags = _declared_cli_flags(entry_point)
    assert len(flags) >= 10, (
        f"{entry_point}: parsed only {len(flags)} flags -- the flag parser is broken and "
        "this test would pass vacuously"
    )
    read = _read_cli_dests(entry_point, extra_sources=_SHARED_CLI_CONSUMERS)
    unread = sorted(dest for dest in flags if dest not in read)
    assert not unread, (
        f"{entry_point}: {len(unread)} CLI flag(s) are declared but never read back "
        f"(dangling arguments):\n"
        + "\n".join(f"  - --{d.replace('_', '-')} (dest={d}, line {flags[d]})" for d in unread)
    )


# ---------------------------------------------------------------------------
# 3. The analyzer's own discriminating power (synthetic control)
# ---------------------------------------------------------------------------


def test_analyzer_flags_a_freshly_declared_unused_field(tmp_path):
    """Control: a brand-new field with no reader must be detected, not tolerated.

    This keeps the guard honest without mutating the real config module: a synthetic
    declaring module + a synthetic consumer is fed through the same
    `_collect_name_refs` / `_consumed_fields` pipeline the production assertion uses.
    """
    decl = tmp_path / "decl.py"
    decl.write_text(
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class FakeConfig:\n"
        "    wired_knob: int = 1\n"
        "    brand_new_knob: int = 2  # declared, never read\n",
        encoding="utf-8",
    )
    consumer = tmp_path / "consumer.py"
    consumer.write_text("def use(cfg):\n    return cfg.wired_knob\n", encoding="utf-8")

    tree = _parse(str(decl))
    fields = _declared_fields(tree, "FakeConfig")
    assert set(fields) == {"wired_knob", "brand_new_knob"}

    business, _nb = _collect_name_refs([str(consumer)], set(fields), str(decl))
    consumed = _consumed_fields(fields, business)
    assert consumed == {"wired_knob"}
    assert "brand_new_knob" not in consumed, "analyzer FAILED to flag an unreferenced new field"


def test_resolve_param_dynamic_fallback_unchanged():
    """Pins the assumption behind exempting the `blend_*`/`cfsb_*` families.

    `_consumed_fields` treats `resolve_param(p, "<bare name>")` as a read of every
    prefixed variant, because `BaseBlendAllocationStrategy.resolve_param` ends in
    `getattr(cfg, f"{param_prefix}_{name}")` / `getattr(cfg, f"blend_{name}")`. If that
    fallback is ever removed, this exemption becomes a lie -- so assert it.
    """
    blend_base = os.path.join(_REPO_ROOT, "pipeline", "research", "blend", "blend_base.py")
    with open(blend_base, "r", encoding="utf-8") as fh:
        source = fh.read()
    start = source.index("def resolve_param(")
    body = source[start:start + 3000]
    assert "getattr(cfg, f\"{param_prefix}_{name}\")" in body, (
        "resolve_param no longer resolves `getattr(cfg, f\"{param_prefix}_{name}\")`; the "
        "prefix-family exemption in _consumed_fields must be re-derived"
    )
    assert "getattr(cfg, f\"blend_{name}\")" in body
