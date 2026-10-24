"""Static anti-recurrence guard: never infer a liquidation from a zero weight.

`docs/development_plan.md` **L2.25** (with `L2.24` supplying the enum it relies on).

WHY THIS TEST EXISTS (the bug class, not a style preference)
============================================================
A live ticket row carries `target_weight`, the post-lifecycle target. Reading
`target_weight == 0.0` as "this row liquidated the position" is wrong for every
row family below (all verified against `common/position_lifecycle.py`):

  1. `MIN_ENTRY_THRESHOLD`  -- target too small to bother entering, nothing bought;
  2. `PENDING_ENTRY_CONFIRM` -- entry still inside its confirmation window;
  3. `MARKET_CLOSED_FREEZE`  -- market shut, NOTHING was evaluated at all
     (`target_weight` merely carries `state.current_weight`, i.e. 0.0 when flat);
  4. `COOLDOWN_BLOCKED` / `INERTIA` / `TENURE_LOCKED` / `LIMIT_UP_LOCKED` -- risk
     gates that emitted no order;
  5. `CANCEL_PENDING`        -- a never-held record of an aborted entry.

The 2026-10-22 post-mortem (`docs/version.md` §11) found **34 such rows across 25
archived tickets** being read as liquidations, each arming a 2-trading-day
re-entry cooldown one day before `as_of` and flattening the symbol right before
Phase 2 could buy it. The single honest predicate is
`common.position_lifecycle.is_liquidation_event` (`action == "SELL"`), optionally
qualified by `blocking_reason`. Consumers: the Phase 3 renderer, the audit block
(`detail_reason.is_liquidation`), and the web badges.

WHAT THIS FILE ASSERTS
======================
For every scanned production source file, a comparison that tests a *target
weight* (or target *shares*) value against zero must NOT appear in a context that
talks about liquidation / exit (`清仓`, `平仓`, `liquidat`, `last_exit_date`, ...).
The context is the innermost enclosing `if` / `while` statement, **including** the
inline comment on the comparison's own line -- so the forbidden shape is caught
whether the "exit" story is in the branch body or in a trailing comment.

Why this is not just a grep for the word "zero": the rule fires only when BOTH
halves co-occur (a zero test on a target weight **and** liquidation wording in
its enclosing branch). A legitimate weight comparison -- sizing, rounding,
inertia, `tgt_w > 0` sign checks, `raw_target_weight == 0.0` on the *model* input
-- carries no such wording and is not flagged. `test_detector_flags_...` /
`test_detector_ignores_...` pin both halves of that discrimination so the guard
cannot silently degrade into "always fails" or "never fails".

KNOWN-LEGITIMATE EXEMPTIONS (each with the reason it must stay)
==============================================================
See `EXEMPT_SITES` and `KNOWN_LEGITIMATE_NON_TRIGGERS` below. Every exemption is
asserted to still match a real site (a stale exemption fails the suite), so the
list cannot rot into a blanket allow-list.
"""

from __future__ import annotations

import ast
import pathlib
import re
from typing import Dict, Iterable, List, NamedTuple, Optional

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Directory names never scanned: virtualenvs, caches, vendored JS deps, and every
# project's `tests/` directory (tests deliberately construct the forbidden shape
# as fixtures -- e.g. `test_live_path_hardening.py` builds archived CSV rows with
# `target_weight: 0.0` to prove the reader ignores them).
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "build",
    "dist",
    ".ruff_cache",
    "tests",
}

WEIGHT_ZERO_NAME = re.compile(r"^(?:raw_)?(?:target_weight|tgt_w|tgt_weight)$")
SHARES_ZERO_NAME = re.compile(r"^(?:[a-z0-9]+_)*(?:target_shares|tgt_sh|tgt_shares)$")

# Wording that turns a zero comparison into a *liquidation inference*. Deliberately
# narrow: "卖出"/"SELL" are NOT here, because a row that merely mentions a sell is
# not necessarily inferring one from a weight.
LIQUIDATION_KEYWORDS = (
    "清仓",
    "平仓",
    "已取消开仓",
    "liquidat",
    "last_exit_date",
    "exit_date",
    "is_liquidation",
)

# Zero-like comparison constants. `1e-6`/`1e-9` matter: float weights are often
# compared against an epsilon instead of a literal 0.0, which is the same
# inference wearing a different constant.
ZERO_CONSTANTS = (0, 0.0, 1e-6, 1e-9, 1e-12)


class Hit(NamedTuple):
    path: str          # repo-relative POSIX path
    lineno: int
    kind: str          # "weight" | "shares"
    name: str          # operand identifier as written
    snippet: str       # the comparison's own source line (stripped)
    scope: str         # enclosing function name, "<module>" when top-level
    keywords: tuple


class ExemptSite(NamedTuple):
    path: str
    anchor: str        # exact stripped source line of the offending comparison
    reason: str


# ---------------------------------------------------------------------------
# Exemptions -- each one is a site where the zero test is NOT an inference about
# liquidation. Anything not listed here must be free of liquidation wording.
# ---------------------------------------------------------------------------
EXEMPT_SITES: tuple = (
    ExemptSite(
        path="common/position_lifecycle.py",
        anchor="if raw_target_weight == 0.0:",
        reason=(
            "The RAW model target, i.e. the pre-lifecycle input -- the SOURCE of a "
            "genuine exit request, not an inference from a post-lifecycle ticket "
            "weight. `step_position_lifecycle` turns this 0.0 into `SELL` via the "
            "exit-confirmation ladder, which is exactly what `is_liquidation_event` "
            "vouches for downstream. Removing this leg would delete real exits. "
            "File is also frozen by L2.24 (do not touch)."
        ),
    ),
    ExemptSite(
        path="common/order_netting.py",
        anchor="elif rounded_tgt_shares == 0 and cur_master_shares > 0:",
        reason=(
            "SHARE level, order-quantity truth: `rounded_tgt_shares` is the lot-"
            "rounded quantity the netting engine will actually send, so 0 vs >0 "
            "held shares IS a full exit by construction (and the branch then emits "
            "`action = SELL`). It is not guessing intent from a weight, and the "
            ">=109-line counterpart (line ~498, `cur_master_shares == 0`) is exempt "
            "for the same reason."
        ),
    ),
    ExemptSite(
        path="scripts/run_live_strategy.py",
        anchor="if sym != cash_proxy and current_shares > 0 and target_shares == 0:",
        reason=(
            "Lot-rounding reconciliation: `current_shares > 0` with a lot-rounded "
            "`target_shares == 0` means the generated order sells EVERY held share, "
            "so the branch legitimately sets `action = SELL` (thereby making "
            "`is_liquidation_event` true) and moves lifecycle state to COOLDOWN. "
            "Share arithmetic on the order, not weight introspection."
        ),
    ),
    # (L2.22, decided 2026-10-23) A FOURTH exemption used to live here for the
    # cooldown-anchoring site:
    #     if cur_sh > 0 and (act == "SELL" or tgt_sh == 0):
    # It was kept only because cooldown-vs-partial-trims was an undecided product
    # question. The ruling ("only a position going to zero is an exit; a partial
    # trim never resets the cooldown") REMOVED that whole comparison: the site now
    # delegates to `should_anchor_reentry_cooldown()` -- `SELL` + zero post-trade
    # shares + not a market freeze -- which compares no target weight/share against
    # zero at all. The exemption is therefore deleted rather than refreshed; do NOT
    # reintroduce it. If a zero-target comparison reappears next to
    # `last_exit_date` in that file, this suite must go red again.
)

# Files where the pattern deliberately does NOT appear, kept here with the reason
# so a future reader does not "helpfully" reintroduce it. Asserted to stay clean.
KNOWN_LEGITIMATE_NON_TRIGGERS: Dict[str, str] = {
    "common/allocation_backtester.py": (
        "The engine judges the SIGN of the raw target (`target_w > 0` -> BUY, "
        "`target_w < 0` -> SELL) on the weight it was handed by the strategy -- that "
        "is the definition of the target, not an inference about whether a trade "
        "happened. It never asks `target_weight == 0` to mean 'liquidated'. Must stay "
        "that way: the backtester must keep judging raw targets, and the live-path "
        "consumers must judge actions."
    ),
    "web/livetrading/storage.py": (
        "Uses `target_shares` (share level) to reconstruct assumed post-trade "
        "holdings (`derive_holdings_from_ticket`); it never reads a zero target "
        "weight as an exit. `blocking_reason` is passed through verbatim for the UI."
    ),
    "web/livetrading/execution_service.py": (
        "Counts trades with `action in ('BUY', 'SELL')` -- the honest predicate -- "
        "and never touches a zero weight."
    ),
}


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def _iter_source_files() -> Iterable[pathlib.Path]:
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel_parts = path.relative_to(REPO_ROOT).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        yield path


def _operand_name(node: ast.AST) -> Optional[str]:
    """Best-effort identifier behind a comparison operand.

    Handles the three shapes a target weight is actually written in:
    `tgt_w`, `row.target_weight`, and `r["target_weight"]` (a DataFrame row).
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return key.value
        return None
    return None


def _kind(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    if WEIGHT_ZERO_NAME.match(name):
        return "weight"
    if SHARES_ZERO_NAME.match(name):
        return "shares"
    return None


def _is_zero_constant(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value) in ZERO_CONSTANTS
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _is_zero_constant(node.operand)
    return False


# "is this value zero?" tests only. `>` / `<` sign checks are a different question
# (they ask "long or short / wanted or not wanted", which is legitimate), so they
# are excluded rather than swept into an allow-list.
_ZERO_TEST_OPS_RIGHT = (ast.Eq, ast.LtE, ast.Lt)
_ZERO_TEST_OPS_LEFT = (ast.Eq, ast.GtE, ast.Gt)


def _zero_operand(compare: ast.Compare) -> Optional[tuple]:
    """Return `(kind, name)` when `compare` is a zero test on a target weight/shares."""
    operands = [compare.left] + list(compare.comparators)
    for idx, op in enumerate(compare.ops):
        left, right = operands[idx], operands[idx + 1]
        if _is_zero_constant(right) and isinstance(op, _ZERO_TEST_OPS_RIGHT):
            kind = _kind(_operand_name(left))
            if kind:
                return kind, _operand_name(left)
        if _is_zero_constant(left) and isinstance(op, _ZERO_TEST_OPS_LEFT):
            kind = _kind(_operand_name(right))
            if kind:
                return kind, _operand_name(right)
    return None


def _scope_name(tree: ast.AST, lineno: int) -> str:
    best = "<module>"
    best_span = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                span = (node.end_lineno or node.lineno) - node.lineno
                if best_span is None or span < best_span:
                    best, best_span = node.name, span
    return best


def _enclosing_branch(tree: ast.AST, lineno: int) -> Optional[ast.AST]:
    """Innermost enclosing `if` / `while` statement (the unit of 'context')."""
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While)):
            end = node.end_lineno or node.lineno
            if node.lineno <= lineno <= end:
                if best is None or (end - node.lineno) < ((best.end_lineno or best.lineno) - best.lineno):
                    best = node
    return best


def scan_source(source: str, path: str = "<memory>") -> List[Hit]:
    """Find zero-weight/share tests that live in liquidation-wording context."""
    import warnings

    try:
        with warnings.catch_warnings():
            # Some production sources contain `\%` inside print() f-strings; parsing
            # them for this audit must not emit SyntaxWarning/DeprecationWarning noise.
            warnings.simplefilter("ignore")
            tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - production sources always parse
        return []
    lines = source.splitlines()
    hits: List[Hit] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        zero = _zero_operand(node)
        if zero is None:
            continue
        kind, name = zero
        branch = _enclosing_branch(tree, node.lineno)
        context = ast.get_source_segment(source, branch) if branch is not None else ""
        own_line = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
        haystack = ((context or "") + "\n" + own_line).lower()
        keywords = tuple(k for k in LIQUIDATION_KEYWORDS if k.lower() in haystack)
        if not keywords:
            continue
        hits.append(
            Hit(
                path=path,
                lineno=node.lineno,
                kind=kind,
                name=name or "<expr>",
                snippet=own_line.strip(),
                scope=_scope_name(tree, node.lineno),
                keywords=keywords,
            )
        )
    return hits


def scan_repo() -> List[Hit]:
    hits: List[Hit] = []
    for path in _iter_source_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        hits.extend(scan_source(path.read_text(encoding="utf-8"), path=rel))
    return hits


def _exemption_for(hit: Hit) -> Optional[ExemptSite]:
    for site in EXEMPT_SITES:
        if site.path == hit.path and site.anchor == hit.snippet:
            return site
    return None


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def test_no_zero_weight_liquidation_inference_outside_the_allow_list():
    """A zero-weight comparison may not sit in liquidation wording unless exempted.

    FALSIFICATION EVIDENCE (run by hand, not by the suite): inserting
    `if tgt_w == 0.0:  # 已清仓` into a live-path branch and re-running this file
    turns it red with the inserted line quoted; removing the line turns it green.
    """
    offenders = [hit for hit in scan_repo() if _exemption_for(hit) is None]
    assert not offenders, (
        "Forbidden pattern: a `target_weight` / `tgt_w` / `target_shares` zero test "
        "used in liquidation/exit wording. A row's exit must be decided by "
        "`action == \"SELL\"` (`common.position_lifecycle.is_liquidation_event`) "
        "plus `blocking_reason` -- never by a 0.0 weight. See "
        "`docs/development_plan.md` L2.25 and `docs/version.md` §11.\n\n"
        + "\n".join(
            f"  {h.path}:{h.lineno} ({h.kind}:{h.name}, scope={h.scope}, "
            f"keywords={list(h.keywords)})\n    {h.snippet}"
            for h in offenders
        )
    )


def test_every_exemption_still_matches_a_real_site():
    """No rotting allow-list: each exemption must still point at live code.

    A stale exemption is itself a hazard -- it silently widens the allow-list for
    a line that no longer exists, so the next real violation at that spot would be
    waved through.
    """
    hits = scan_repo()
    matched: Dict[str, int] = {f"{s.path}::{s.anchor}": 0 for s in EXEMPT_SITES}
    for hit in hits:
        site = _exemption_for(hit)
        if site is not None:
            matched[f"{site.path}::{site.anchor}"] += 1

    stale = [key for key, count in matched.items() if count == 0]
    assert not stale, (
        "These exemptions no longer match any detected site -- the code moved or "
        "was fixed. Delete/refresh the entry instead of leaving a hole:\n  "
        + "\n  ".join(stale)
    )
    for site in EXEMPT_SITES:
        assert site.reason.strip(), f"exemption {site.path}::{site.anchor} has no documented reason"


@pytest.mark.parametrize("rel_path", sorted(KNOWN_LEGITIMATE_NON_TRIGGERS))
def test_documented_legitimate_files_stay_clean(rel_path):
    """Files whose legitimate pattern is documented above must not drift into the trap."""
    path = REPO_ROOT / rel_path
    assert path.is_file(), f"{rel_path} moved -- refresh KNOWN_LEGITIMATE_NON_TRIGGERS"
    hits = scan_source(path.read_text(encoding="utf-8"), path=rel_path)
    assert not hits, (
        f"{rel_path} now contains a zero-target-weight liquidation inference "
        f"({[h.snippet for h in hits]}). The documented reason for its exemption "
        f"is: {KNOWN_LEGITIMATE_NON_TRIGGERS[rel_path]}"
    )


# ---------------------------------------------------------------------------
# Detector self-tests -- prove the guard has teeth (and is not a blanket failure)
# ---------------------------------------------------------------------------


def test_detector_flags_an_inferred_liquidation_from_a_zero_weight():
    bad = (
        "def render(row):\n"
        "    tgt_w = row['target_weight']\n"
        "    if tgt_w == 0.0:  # 已清仓\n"
        "        return 'EXITED'\n"
        "    return 'HELD'\n"
    )
    hits = scan_source(bad, path="<synthetic>")
    assert len(hits) == 1
    assert hits[0].kind == "weight"
    assert hits[0].name == "tgt_w"
    assert hits[0].scope == "render"
    assert "清仓" in hits[0].keywords


def test_detector_flags_the_subscript_and_epsilon_variants():
    """`r["target_weight"] <= 1e-6` is the same inference with a different spelling."""
    for expr in ('r["target_weight"] == 0', "r.target_weight <= 1e-6"):
        src = (
            "def audit(r):\n"
            f"    if {expr}:\n"
            "        return 'liquidated'\n"
            "    return 'ok'\n"
        )
        hits = scan_source(src, path="<synthetic>")
        assert hits, f"detector missed the zero test in: {expr}"


def test_detector_ignores_a_plain_weight_comparison():
    """Discrimination check: a legitimate weight comparison must NOT be flagged."""
    good = (
        "def rebalance(tgt_w, cur_w):\n"
        "    if tgt_w == 0.0:\n"
        "        return 0.0\n"
        "    if tgt_w > cur_w:\n"
        "        return tgt_w - cur_w\n"
        "    return cur_w - tgt_w\n"
    )
    assert scan_source(good, path="<synthetic>") == []


def test_detector_ignores_liquidation_wording_that_is_not_a_zero_test():
    """Discrimination check (other direction): wording alone must not trigger it."""
    good = (
        "def describe(act):\n"
        "    if act == 'SELL':\n"
        "        return '已清仓'\n"
        "    return '维持'\n"
    )
    assert scan_source(good, path="<synthetic>") == []


# ---------------------------------------------------------------------------
# Front-end half: the browser badge must not parse a weight either
# ---------------------------------------------------------------------------

_JS_ZERO_INFERENCE = re.compile(
    r"target_weight\s*(?:===?|!==?|<=|<)\s*0(?![.\d])"
)


def _strip_js_comments(source: str) -> str:
    """Drop `//` line comments and `/* ... */` blocks before scanning for code.

    Prose is not a violation: this guard's own explanatory comments quote
    `target_weight === 0`. Only executable comparisons matter.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return "\n".join(re.sub(r"//.*$", "", line) for line in source.splitlines())


def test_app_js_never_compares_target_weight_against_zero():
    """`app.js` must key its badges off `action` + `blocking_reason`.

    The old badge code did not compare the weight either -- it silently fell
    through to `⚪ HOLD (维持)` for `CANCEL_PENDING` / market-closed rows -- but
    this regex is the cheap structural half of the guard: if a future refactor
    decides "a 0.0 target means we exited", the front end is where that
    regression would land first.
    """
    app_js = (REPO_ROOT / "web" / "livetrading" / "static" / "js" / "app.js").read_text(
        encoding="utf-8"
    )
    offenders = [
        line.strip()
        for line in _strip_js_comments(app_js).splitlines()
        if _JS_ZERO_INFERENCE.search(line)
    ]
    assert not offenders, (
        "app.js compares `target_weight` against zero -- the web badge must read "
        "`action` / `blocking_reason` instead (L2.25):\n  " + "\n  ".join(offenders)
    )
