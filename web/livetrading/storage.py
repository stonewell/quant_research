"""Storage and configuration persistence layer for the Live Trading Web Application.

Enforces zero hardcoded paths by prioritizing:
  Explicit Override / CLI > Environment Variable > settings.local.json (operator
  override) > settings.json (committed TEMPLATE, placeholders only) > Dynamically
  Discovered Relative Fallback (always under ``repo_root``).

The committed ``web/livetrading/data/settings.json`` must NEVER carry machine-specific
absolute paths: it is a template whose path keys are empty.  Real deployments supply
their paths through environment variables or through the untracked
``web/livetrading/data/settings.local.json``; that local file is also the write target
of ``update_settings`` so the UI can never rewrite the template.  See ``L2.16`` in
``docs/development_plan.md``.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd


def find_repo_root() -> str:
    """Dynamically discover the quantitative workspace repository root."""
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(10):
        if os.path.isfile(os.path.join(d, "AGENTS.md")) or os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    # Fallback to two levels up if marker not found
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


try:
    from common.universe import get_stock_name, is_placeholder_stock_name
except ImportError:
    try:
        sys.path.insert(0, find_repo_root())
        from common.universe import get_stock_name, is_placeholder_stock_name
    except Exception:
        def get_stock_name(sym: str, default: Optional[str] = None) -> str:
            return default if default is not None else sym

        def is_placeholder_stock_name(name: object, symbol: object = None) -> bool:
            n = "" if name is None else str(name).strip()
            return n.lower() in ("custom", "universe", "none", "unknown", "nan", "") or (
                symbol is not None and n == str(symbol).strip()
            )


# (L2.3) The bar-date-vs-expected-session comparison has exactly one owner. The UI quote
# path may not re-derive it (design §7), so it asks `common/freshness.py` and merely
# reports the verdict alongside the price.
try:
    from common.freshness import is_stale_bar
except ImportError:  # pragma: no cover - repo root not yet importable
    sys.path.insert(0, find_repo_root())
    from common.freshness import is_stale_bar


# Directory the multi-strategy netting CLI (`scripts/run_live_multi_strategy.py`)
# writes its netted ticket / sub-ledgers into when the operator does not pass
# `--output-dir`. The reader below falls back to it when a matrix has no dated archive
# run yet -- i.e. when the ticket was produced from the CLI rather than by this web
# app's own deploy path (which always passes `--output-dir <archive>/<date>/multi_netted_<matrix>`).
#
# Why this is imported, not spelled: until 2026-10-23 both ends named
# `docs/ruleset/multi_strategy_netted` independently. `docs/ruleset/` is the
# hand-authored operating-manual tree and is being deleted; a fallback that still
# pointed there would be a silent-empty-read (the CLI now writes elsewhere), and a
# second literal would let "CLI writes here, web reads there" split again. So the path
# and the `multi_strategy_netted` key both come from `scripts/live_paths.py`, the same
# resolver the writer imports.
#
# `default_live_output_dir()` is self-locating (`live_paths.__file__` -> repo root), so
# this follows the checkout that actually CONTAINS the writer script rather than this
# StorageManager's configurable `repo_root`. That is the correct anchor: a
# `repo_root` that does not contain `scripts/run_live_multi_strategy.py` has no default
# run output to read in the first place.
try:
    from scripts.live_paths import MULTI_STRATEGY_KEY, default_live_output_dir
except ImportError:  # pragma: no cover - repo root not yet importable
    sys.path.insert(0, find_repo_root())
    from scripts.live_paths import MULTI_STRATEGY_KEY, default_live_output_dir

#: Resolved fallback location for `get_multi_netting_run`. One owner above; nothing in
#: this module may re-spell it.
MULTI_NETTING_FALLBACK_DIR = default_live_output_dir(MULTI_STRATEGY_KEY)


def normalize_universe_key(key: Optional[str]) -> str:
    """Normalize a universe key for robust comparison across custom universe conventions.

    Treats custom universe conventions equivalently:
      'custom_astock_202609_20' <-> 'astock_202609_20_custom' <-> 'astock_202609_20'
    """
    if not key:
        return ""
    k = str(key).strip().lower()
    k = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fa5]+", "_", k).strip("_")
    if k.startswith("custom_"):
        k = k[7:]
    if k.endswith("_custom"):
        k = k[:-7]
    return k


def keys_match(key_a: Optional[str], key_b: Optional[str]) -> bool:
    """Compare two keys (strategy or universe) for equality, casing, or custom naming equivalence."""
    if key_a is None or key_b is None:
        return key_a == key_b
    ka = str(key_a).strip().lower()
    kb = str(key_b).strip().lower()
    if ka == kb:
        return True
    return normalize_universe_key(ka) == normalize_universe_key(kb)


def get_universe_key_variants(universe_key: Optional[str]) -> List[str]:
    """Generate all common filesystem naming variants for a universe key."""
    if not universe_key:
        return []
    variants: List[str] = [universe_key]
    low = str(universe_key).strip().lower()
    if low not in variants:
        variants.append(low)

    if low.startswith("custom_"):
        slug = low[7:]
        cand_suffix = f"{slug}_custom"
        if cand_suffix not in variants:
            variants.append(cand_suffix)
        if slug not in variants:
            variants.append(slug)
    elif low.endswith("_custom"):
        slug = low[:-7]
        cand_prefix = f"custom_{slug}"
        if cand_prefix not in variants:
            variants.append(cand_prefix)
        if slug not in variants:
            variants.append(slug)
    else:
        cand_prefix = f"custom_{low}"
        cand_suffix = f"{low}_custom"
        if cand_prefix not in variants:
            variants.append(cand_prefix)
        if cand_suffix not in variants:
            variants.append(cand_suffix)

    return variants



_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9_\-\u4e00-\u9fa5]+")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Ticket/cash names that never denote a real share position. BIL is included deliberately: the
# Stage 2 runner (`resolve_holdings_weights_and_shares` in scripts/run_live_strategy.py)
# treats BIL/CASH/<cash_proxy> as the IMPLIED cash residual (NAV - equity value), never as a
# share count, so carrying a BIL "share" quantity forward would be meaningless.
_CASH_SYMBOLS = {"BIL", "CASH"}


def sanitize_key(key: Optional[str]) -> Optional[str]:
    """Make a strategy/universe key safe to use as a single path component.

    Keys arrive from query strings / request bodies and are joined into archive paths, so any
    separator or '..' must be neutralized (path traversal). Allowed: letters, digits, '_', '-', CJK.
    """
    if key is None:
        return None
    cleaned = _SAFE_KEY_RE.sub("_", str(key).strip()).strip("_")
    return cleaned or None


def validate_date_str(date_str: Optional[str]) -> Optional[str]:
    """Validate an ISO YYYY-MM-DD date string (also a path component); raise ValueError if malformed."""
    if date_str is None or date_str == "":
        return None
    s = str(date_str).strip()
    if not _DATE_RE.match(s):
        raise ValueError(f"Invalid date '{date_str}': expected YYYY-MM-DD")
    return s


def is_cash_symbol(sym: Optional[str]) -> bool:
    s = str(sym or "").strip().upper()
    return s in _CASH_SYMBOLS or s.startswith("CASH")


# --------------------------------------------------------------------------- #
# (L2.4 / L2.5) Market clock: the ONE source of an archive date key
# --------------------------------------------------------------------------- #
# FORENSIC CONTEXT (why this lives here, not in each caller):
#   `web/livetrading` used to key its daily archive on `date.today()` -- the HOST's
#   calendar day.  On a Saturday that names a non-trading day, and during the session
#   it names a bar that does not exist yet, so the Stage-1/Stage-2 run directory, the
#   `summary.json` `date`, the quotes basis and the `/api/deploy/multi/latest` lookup
#   could each be keyed by a different day.  `L2.3` moved the *decision* into
#   `common/market_clock.expected_last_session`; `L2.4`/`L2.5` finish the job for the
#   ARCHIVE KEY by routing every date-key resolution in this trio
#   (`storage.py`, `server.py`, `execution_service.py`) through the two methods below.
#   Never reintroduce `date.today()` as a fallback: a missing date is "ask the
#   market clock", not "ask the host".
_CLOCK_CASH_PROXIES = frozenset({"BIL", "CASH"})


def is_clock_cash_proxy(symbol: Optional[str]) -> bool:
    """Cash/liquidity proxy: priced at 1.0, no calendar, must not vote on the date key."""
    s = str(symbol or "").strip().upper()
    return s in _CLOCK_CASH_PROXIES or s.startswith("CASH")


def read_universe_symbols(path: Optional[str]) -> List[str]:
    """Symbols of a universe file (one per line, `#` comments allowed); `[]` if unreadable."""
    if not path:
        return []
    symbols: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                token = line.split("#", 1)[0].strip()
                if not token:
                    continue
                symbols.extend(tok.upper() for tok in token.replace(",", " ").split())
    except OSError:
        return []
    return symbols


def post_holdings_from_ticket(ticket: Any) -> Optional[Dict[str, int]]:
    """Derive assumed post-trade share holdings from a ticket's `target_shares` column.

    Returns None if the ticket carries no target_shares information at all, and a (possibly
    EMPTY) dict otherwise -- an empty dict means "fully liquidated to cash", which callers must
    not confuse with "unknown". Cash-proxy rows (BIL/CASH, or any SWEEP_CASH / RELEASE_CASH row,
    which is how the Stage 2 runner tags its configured cash_proxy such as 511880.SH) are skipped
    because cash is the implied residual, not a share position.
    """
    if not ticket or not isinstance(ticket, list):
        return None
    holdings: Dict[str, int] = {}
    saw_target = False
    for r in ticket:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol", "")).strip().upper()
        action = str(r.get("action", "")).upper()
        if not sym or is_cash_symbol(sym) or "CASH" in action:
            continue
        ts = r.get("target_shares")
        if ts is None or ts == "":
            continue
        try:
            s_int = int(float(ts))
        except (ValueError, TypeError):
            continue
        saw_target = True
        if s_int > 0:
            holdings[sym] = s_int
    return holdings if saw_target else None


def parse_ticket_csv(csv_path: str) -> List[Dict[str, Any]]:
    """Parse live_trading_ticket.csv into a structured list of ticket records if JSON is missing."""
    if not os.path.exists(csv_path):
        return []
    records: List[Dict[str, Any]] = []
    try:
        import csv
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sym = row.get("symbol", "").strip()
                name = row.get("name", "").strip()
                if is_placeholder_stock_name(name, sym):
                    name = get_stock_name(sym, sym)
                row["name"] = name
                for num_key in ("price", "current_weight", "target_weight", "delta_weight", "trade_value", "trade_value_rmb"):
                    if num_key in row and row[num_key] != "":
                        try:
                            row[num_key] = float(row[num_key])
                        except ValueError:
                            pass
                for int_key in ("current_shares", "target_shares", "delta_shares"):
                    if int_key in row and row[int_key] != "":
                        try:
                            row[int_key] = int(float(row[int_key]))
                        except ValueError:
                            pass
                if "detail_reason" in row and row["detail_reason"]:
                    try:
                        row["detail_reason"] = json.loads(row["detail_reason"])
                    except Exception:
                        pass
                records.append(row)
    except Exception:
        pass
    return records


def is_run_dir(path: Optional[str]) -> bool:
    """Check if path is a directory containing at least one real run artifact."""
    if not path or not os.path.isdir(path):
        return False
    try:
        files = set(os.listdir(path))
    except OSError:
        return False
    return any(
        f in files
        for f in (
            "stage1_health_report.json",
            "live_trading_ticket.json",
            "live_trading_ticket.csv",
            "netted_trading_ticket.json",
            "netted_trading_ticket.csv",
            "summary.json",
            "post_holdings.json",
            "holdings.json",
            "account_state.json",
        )
    )


# ---------------------------------------------------------------------------
# Pytest-only safety net: live production paths must not be resolvable by accident
# ---------------------------------------------------------------------------
# FORENSIC CONTEXT (why this net exists -- it is not defensive paranoia):
#   A bare `StorageManager(repo_root=<repo>)` resolves `cache_dir` to `<repo>/data`,
#   the production directory holding the real `cache.duckdb`.  Historically it also
#   loaded the committed `web/livetrading/data/settings.json`, which pinned
#   `data_dir` / `archive_dir` to a LIVE portfolio directory OUTSIDE the repo, so any
#   unit test, ad-hoc script, or new feature that forgot to pass an explicit
#   `cache_dir` silently wrote into production state (observed in practice).
#   `L2.16` removed that structural default: the committed settings file is now a
#   placeholder-only template and every resolved default lands under `repo_root`.
#   `web/livetrading/tests/conftest.py` isolates this package through the env-var
#   layer; the net below closes the hole at the RESOLVER, so a scratch test in any
#   other package, a doctest, or a future test module is covered too.
#
#   ZERO production behaviour change: everything below is gated on
#   `PYTEST_CURRENT_TEST`, a variable only pytest sets.  When it is absent nothing is
#   read, compared, logged, or raised -- resolution is exactly what it always was,
#   with the same parameter > environment > local override > template > dynamic
#   default order.  The guard inspects the FINAL resolved path (after
#   `os.path.abspath`), so `./data`, `data/../data`, and symlinked aliases all collide
#   with the real target instead of bypassing the check.
#
#   The refusal roots are HARDCODED in code (`<repo_root>/data`), deliberately NOT
#   inferred from the committed settings template -- that template is placeholder-only
#   after `L2.16`, so trusting its contents would silently disable the net.  The
#   operator's untracked `settings.local.json` is still inspected, because that is
#   where a real deployment records its live portfolio directory.
#
#   Escape hatch: `LIVETRADING_ALLOW_PRODUCTION_PATHS=1` for the rare test that
#   genuinely must point at the real cache / live portfolio (deliberate read-only
#   inspection tests).  The variable is honoured so an operator can always unblock
#   themselves instead of deleting this net.

_PYTEST_MARKER_ENV = "PYTEST_CURRENT_TEST"
_ALLOW_PRODUCTION_ENV = "LIVETRADING_ALLOW_PRODUCTION_PATHS"
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on", "y", "t"})
#: Environment variables StorageManager consults for each guarded directory, so the
#: actionable error can name the exact knob instead of a guessed variable name.
_PATH_ENV_VARS = {
    "data_dir": "LIVETRADING_DATA_DIR",
    "archive_dir": "LIVETRADING_ARCHIVE_DIR",
    "cache_dir": "LIVETRADING_CACHE_DIR",
}
#: Where the settings files live.  Resolved against `repo_root` (NOT against the
#: resolved `data_dir`), so that a local override can itself pin `data_dir`.
_SETTINGS_DIR_ENV = "LIVETRADING_SETTINGS_DIR"
_SETTINGS_DIR_REL = os.path.join("web", "livetrading", "data")
#: The committed TEMPLATE: placeholder-only, never machine-specific (L2.16).
_SETTINGS_TEMPLATE_NAME = "settings.json"
#: The operator's untracked override (git-ignored via `web/livetrading/.gitignore`).
_SETTINGS_LOCAL_NAME = "settings.local.json"


def settings_dir_for(repo_root: str) -> str:
    """Directory holding the settings template + local override for `repo_root`.

    `LIVETRADING_SETTINGS_DIR` relocates it (tests use this to stay hermetic; an
    operator can point it at an external deployment directory).  It is resolved from
    `repo_root` rather than from `data_dir` on purpose: the local override must be
    able to pin `data_dir` itself, which would be a chicken-and-egg otherwise.
    """
    override = os.environ.get(_SETTINGS_DIR_ENV)
    if override and str(override).strip():
        return os.path.abspath(override)
    return os.path.join(os.path.abspath(repo_root), _SETTINGS_DIR_REL)


class ProductionPathError(RuntimeError):
    """A pytest process tried to resolve a writable path onto live production state."""


def _canonical_path(path: Any) -> str:
    """Absolute, symlink- and `..`-resolved form of `path` for alias-proof comparison."""
    return os.path.realpath(os.path.abspath(os.fspath(path)))


def _is_within(path: Any, root: Any) -> bool:
    """True when `path` is `root` or a descendant of it (both canonicalized first)."""
    path_c = _canonical_path(path)
    root_c = _canonical_path(root)
    if path_c == root_c:
        return True
    try:
        return os.path.commonpath([path_c, root_c]) == root_c
    except ValueError:  # different drives (Windows): cannot possibly be nested
        return False


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in _TRUE_ENV_VALUES


def production_guard_active() -> bool:
    """True only when running under pytest without an explicit operator opt-in.

    Single source of truth for the gate: `LIVETRADING_ALLOW_PRODUCTION_PATHS=1`
    disables it, and outside pytest (`PYTEST_CURRENT_TEST` unset) it is always
    inactive, which is what keeps production resolution untouched.
    """
    return bool(os.environ.get(_PYTEST_MARKER_ENV)) and not _truthy_env(_ALLOW_PRODUCTION_ENV)


def production_path_roots(repo_root: str) -> List[Tuple[str, str]]:
    """(label, directory) pairs a pytest process must never resolve a writable path into.

    Two categories:
      1. `<repo_root>/data` -- the repo's production market cache (`cache.duckdb`).
         HARDCODED here on purpose: after L2.16 the committed settings template is
         placeholder-only, so deriving this root from file content would be a
         self-disabling guard.
      2. Any absolute directory the operator's untracked local override
         (`settings.local.json`) pins OUTSIDE the repo -- i.e. the live portfolio
         directory (and therefore its `<dir>/runs` archive, which the containment
         check catches as a descendant).  The committed template is never consulted.
    In-repo pins are skipped: they are either covered by (1) or ordinary workspace
    directories (e.g. `docs/universe`), which tests are allowed to read.
    """
    roots: List[Tuple[str, str]] = [
        (
            "the production market cache (<repo_root>/data, holds cache.duckdb)",
            os.path.join(repo_root, "data"),
        ),
    ]

    settings_dir = settings_dir_for(repo_root)
    local_path = os.path.join(settings_dir, _SETTINGS_LOCAL_NAME)
    try:
        with open(local_path, "r", encoding="utf-8") as f:
            local_settings = json.load(f)
    except Exception:
        local_settings = {}
    if not isinstance(local_settings, dict):
        return roots

    # Collect the out-of-repo pins, merging keys that live under the same root
    # (`data_dir=/live` and `archive_dir=/live/runs` are one location).
    pinned: List[Tuple[str, List[str]]] = []
    for key in ("data_dir", "archive_dir", "cache_dir"):
        value = local_settings.get(key)
        if not isinstance(value, str) or not value.strip() or not os.path.isabs(value):
            continue
        value_abs = os.path.abspath(value)
        if _is_within(value_abs, repo_root):
            continue
        if any(_is_within(value_abs, root) for _, root in roots):
            continue
        for known_root, keys in pinned:
            if _is_within(value_abs, known_root):
                keys.append(key)
                break
        else:
            pinned.append((value_abs, [key]))

    for known_root, keys in pinned:
        roots.append((
            f"the live portfolio directory pinned by the operator's local override "
            f"({local_path}, settings key(s) {', '.join(keys)})",
            known_root,
        ))
    return roots


def refuse_production_path(
    kind: str,
    path: str,
    source: str,
    roots: List[Tuple[str, str]],
) -> None:
    """Raise `ProductionPathError` if `path` lands on a live production location.

    `source` names the leg of the precedence chain that produced the path, so the
    message can tell the reader which knob to turn (`data_dir=` / env var /
    `settings.json`) instead of only that something is wrong.
    """
    for label, root in roots:
        if _is_within(path, root):
            raise ProductionPathError(
                f"Refusing to use {kind}={_canonical_path(path)} while running under pytest "
                f"({_PYTEST_MARKER_ENV} is set): it resolves into {label} "
                f"({_canonical_path(root)}), which is LIVE production state -- a test must "
                f"never read or write it.\n"
                f"  resolved from: {source}\n"
                f"  fix: pass an explicit {kind}=<temporary directory> to StorageManager "
                f"(or set the {_PATH_ENV_VARS.get(kind, 'LIVETRADING_' + kind.upper() + '_DIR')} "
                f"environment variable), e.g. "
                f"StorageManager({kind}=tmp_path / '{kind}'), and keep test data under "
                f"the OS temporary root.\n"
                f"  If this test genuinely must use production paths, opt in explicitly "
                f"with {_ALLOW_PRODUCTION_ENV}=1."
            )


def _first_defined(*candidates: Tuple[str, Any]) -> Tuple[Any, str]:
    """First truthy (value, source-label) pair -- the historical `a or b or c` chain.

    Returns the source label too, purely so the pytest guard can name the exact leg
    that resolved a bad path.  Semantics are identical to `or`-chaining (falsy
    values such as None/""/0 are skipped).
    """
    for label, value in candidates:
        if value:
            return value, label
    return None, ""


class StorageManager:
    """Manages paths, settings, custom universes, holdings, account states, and daily archives."""

    def __init__(
        self,
        repo_root: Optional[str] = None,
        data_dir: Optional[str] = None,
        archive_dir: Optional[str] = None,
        strategy_dir: Optional[str] = None,
        universe_dir: Optional[str] = None,
        cache_dir: Optional[str] = None,
        stage1_script: Optional[str] = None,
        stage2_script: Optional[str] = None,
        python_executable: Optional[str] = None,
        settings_dir: Optional[str] = None,
    ):
        # 1. Resolve repo_root
        self.repo_root = (
            repo_root
            or os.environ.get("LIVETRADING_REPO_ROOT")
            or find_repo_root()
        )
        self.repo_root = os.path.abspath(self.repo_root)

        # 1b. Pytest-only production-path net (a single env lookup in production).
        #     Disabled entirely unless PYTEST_CURRENT_TEST is set, so resolution is
        #     byte-for-byte unchanged for the live app, the CLI and the scripts.
        _prod_guard_active = production_guard_active()
        _prod_roots: List[Tuple[str, str]] = (
            production_path_roots(self.repo_root) if _prod_guard_active else []
        )

        def _guard_pytest_path(kind: str, value: str, source: str) -> None:
            """Refuse to resolve a writable directory onto live production state under pytest."""
            if _prod_guard_active:
                refuse_production_path(kind, value, source, _prod_roots)

        # 2. Settings: committed TEMPLATE (placeholders only) + untracked LOCAL override.
        #    Both live in `settings_dir`, which is resolved from repo_root (NOT from
        #    data_dir) precisely so a local override can pin `data_dir` itself.
        self.settings_dir = os.path.abspath(settings_dir) if settings_dir else settings_dir_for(self.repo_root)
        self.settings_template_file = os.path.join(self.settings_dir, _SETTINGS_TEMPLATE_NAME)
        self.settings_local_file = os.path.join(self.settings_dir, _SETTINGS_LOCAL_NAME)
        template_settings = self._load_json(self.settings_template_file, default={})
        local_settings = self._load_json(self.settings_local_file, default={})
        if not isinstance(template_settings, dict):
            template_settings = {}
        if not isinstance(local_settings, dict):
            local_settings = {}
        #: Effective file-provided settings: LOCAL OVERRIDE WINS over the template.
        saved_settings: Dict[str, Any] = {**template_settings, **local_settings}
        #: Write target for `update_settings`.  NEVER the committed template -- the UI
        #: must not be able to rewrite a tracked file with machine-specific paths.
        self.settings_file = self.settings_local_file

        def _file_setting(key: str) -> Tuple[str, Any]:
            """(source-label, value) for a key, local override before template.

            Matches `_first_defined`'s (label, value) pair order, so the pytest guard
            can name the exact file/key that produced a refused path.
            """
            local_value = local_settings.get(key)
            if local_value:
                return f"settings.local.json key '{key}' ({self.settings_local_file})", local_value
            template_value = template_settings.get(key)
            if template_value:
                return f"settings.json template key '{key}' ({self.settings_template_file})", template_value
            return "", None

        # 3. Base data_dir
        _data_dir_value, _data_dir_source = _first_defined(
            ("explicit data_dir argument", data_dir),
            ("LIVETRADING_DATA_DIR environment variable", os.environ.get("LIVETRADING_DATA_DIR")),
            _file_setting("data_dir"),
            (
                "dynamic default <repo_root>/web/livetrading/data",
                os.path.join(self.repo_root, "web", "livetrading", "data"),
            ),
        )
        self.data_dir = os.path.abspath(_data_dir_value)
        # Guard BEFORE makedirs: a refused path must not be created on disk.
        _guard_pytest_path("data_dir", self.data_dir, _data_dir_source)
        os.makedirs(self.data_dir, exist_ok=True)

        # 4. Resolve remaining paths with hierarchy:
        #    Parameter > Env > settings.local.json > settings.json template > Dynamic Default
        _archive_dir_value, _archive_dir_source = _first_defined(
            ("explicit archive_dir argument", archive_dir),
            ("LIVETRADING_ARCHIVE_DIR environment variable", os.environ.get("LIVETRADING_ARCHIVE_DIR")),
            _file_setting("archive_dir"),
            ("dynamic default <data_dir>/runs", os.path.join(self.data_dir, "runs")),
        )
        self.archive_dir = os.path.abspath(_archive_dir_value)
        # The daily run archive IS a production write target: a deployment's local
        # override pins it to `<live portfolio>/runs`, and the dynamic default lands
        # inside data_dir. Guarded for the same reason as cache_dir.
        _guard_pytest_path("archive_dir", self.archive_dir, _archive_dir_source)
        os.makedirs(self.archive_dir, exist_ok=True)

        self.strategy_dir = os.path.abspath(
            strategy_dir
            or os.environ.get("LIVETRADING_STRATEGY_DIR")
            or saved_settings.get("strategy_dir")
            or os.path.join(self.repo_root, "pipeline", "research_strategy", "results", "strategy_dumps")
        )

        self.universe_dir = os.path.abspath(
            universe_dir
            or os.environ.get("LIVETRADING_UNIVERSE_DIR")
            or saved_settings.get("universe_dir")
            or os.path.join(self.repo_root, "docs", "universe")
        )

        _cache_dir_value, _cache_dir_source = _first_defined(
            ("explicit cache_dir argument", cache_dir),
            ("LIVETRADING_CACHE_DIR environment variable", os.environ.get("LIVETRADING_CACHE_DIR")),
            _file_setting("cache_dir"),
            ("dynamic default <repo_root>/data", os.path.join(self.repo_root, "data")),
        )
        self.cache_dir = os.path.abspath(_cache_dir_value)
        _guard_pytest_path("cache_dir", self.cache_dir, _cache_dir_source)

        self.stage1_script = os.path.abspath(
            stage1_script
            or os.environ.get("LIVETRADING_STAGE1_SCRIPT")
            or saved_settings.get("stage1_script")
            or os.path.join(self.repo_root, "scripts", "check_live_portfolio_health.py")
        )

        stage2_candidate = (
            stage2_script
            or os.environ.get("LIVETRADING_STAGE2_SCRIPT")
            or saved_settings.get("stage2_script")
            or os.path.join(self.repo_root, "scripts", "run_live_strategy.py")
        )
        universal_candidate = os.path.join(self.repo_root, "scripts", "run_live_strategy.py")
        if not stage2_candidate or "run_live_four_state_blend.py" in stage2_candidate or not os.path.exists(stage2_candidate):
            stage2_candidate = universal_candidate
        self.stage2_script = os.path.abspath(stage2_candidate)
        self.multi_netting_script = os.path.abspath(os.path.join(self.repo_root, "scripts", "run_live_multi_strategy.py"))

        resolved_py = (
            python_executable
            or os.environ.get("LIVETRADING_PYTHON_BIN")
            or saved_settings.get("python_executable")
        )
        if not resolved_py:
            pipeline_py = os.path.join(self.repo_root, "pipeline", ".venv", "bin", "python")
            web_py = os.path.join(self.repo_root, "web", "livetrading", ".venv", "bin", "python")
            if os.path.exists(pipeline_py):
                resolved_py = pipeline_py
            elif os.path.exists(web_py):
                resolved_py = web_py
            else:
                resolved_py = sys.executable

        self.python_executable = os.path.abspath(resolved_py)

        # Detect local MarketDB availability
        has_marketdb = False
        try:
            from common.financial_api import _resolve_duckdb_path
            has_marketdb = _resolve_duckdb_path() is not None
        except Exception:
            pass

        default_provider_fallback = "marketdb" if has_marketdb else "synthetic"
        self.default_data_provider = saved_settings.get("default_data_provider") or default_provider_fallback
        self.default_portfolio_value = float(saved_settings.get("default_portfolio_value", 100000.0))
        self.default_strategy_key = saved_settings.get("default_strategy_key") or "chan_four_state_blend"
        self.default_start_date = saved_settings.get("default_start_date") or "2024-01-01"

        # Files in data_dir
        self.custom_universes_file = os.path.join(self.data_dir, "custom_universes.json")
        self.current_holdings_file = os.path.join(self.data_dir, "current_holdings.json")
        self.account_state_file = os.path.join(self.data_dir, "account_state.json")

    def _load_json(self, path: str, default: Any = None) -> Any:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return default
        return default

    def _save_json(self, path: str, data: Any) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    # --------------------------------------------------------------------------
    # Settings Management
    # --------------------------------------------------------------------------
    def get_settings(self) -> Dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "strategy_dir": self.strategy_dir,
            "universe_dir": self.universe_dir,
            "data_dir": self.data_dir,
            "archive_dir": self.archive_dir,
            "cache_dir": self.cache_dir,
            "stage1_script": self.stage1_script,
            "stage2_script": self.stage2_script,
            "multi_netting_script": self.multi_netting_script,
            "python_executable": self.python_executable,
            "default_data_provider": self.default_data_provider,
            "default_portfolio_value": self.default_portfolio_value,
            "default_strategy_key": self.default_strategy_key,
            "default_start_date": self.default_start_date,
        }

    def update_settings(self, new_settings: Dict[str, Any]) -> Dict[str, Any]:
        """Persist operator-edited settings to the UNTRACKED local override file.

        The write target is ``web/livetrading/data/settings.local.json`` (git-ignored),
        NEVER the committed ``settings.json`` template -- so the UI can pin a
        deployment's real paths without ever dirtying the repository (L2.16).
        """
        current = self.get_settings()
        for k, v in new_settings.items():
            if v is not None and k in current:
                setattr(self, k, v)
                current[k] = v
        self._save_json(self.settings_file, current)
        return current

    # --------------------------------------------------------------------------
    # Custom Universes Management
    # --------------------------------------------------------------------------
    def list_custom_universes(self) -> List[Dict[str, Any]]:
        raw = self._load_json(self.custom_universes_file, default=[])
        if isinstance(raw, dict):
            # In case saved as a map name -> obj
            return list(raw.values())
        return raw if isinstance(raw, list) else []

    def save_custom_universe(self, name: str, symbols: List[str], description: str = "") -> Dict[str, Any]:
        universes = self.list_custom_universes()
        # Clean symbols
        cleaned_symbols = [s.strip().upper() for s in symbols if s and s.strip()]
        new_entry = {
            "name": name.strip(),
            "description": description.strip(),
            "symbols": cleaned_symbols,
            "count": len(cleaned_symbols),
        }
        # Replace if exists, else append
        updated = [u for u in universes if u.get("name") != new_entry["name"]]
        updated.append(new_entry)
        self._save_json(self.custom_universes_file, updated)
        return new_entry

    def delete_custom_universe(self, name: str) -> bool:
        universes = self.list_custom_universes()
        filtered = [u for u in universes if u.get("name") != name]
        if len(filtered) < len(universes):
            self._save_json(self.custom_universes_file, filtered)
            return True
        return False

    # --------------------------------------------------------------------------
    # Discovery: Strategies & Universes
    # --------------------------------------------------------------------------
    def _extract_strategy_metadata(self, data: Dict[str, Any], filename: str) -> Tuple[str, str]:
        """Extract (strategy_key, human_readable_strategy_name) from strategy JSON metadata."""
        spec_name = None
        if isinstance(data.get("research_strategy_spec"), dict):
            entry = data["research_strategy_spec"].get("entry_data", {})
            if isinstance(entry, dict) and entry.get("name"):
                spec_name = str(entry["name"]).strip()

        name_field = data.get("strategy_name") or data.get("name")
        if name_field and isinstance(name_field, str):
            clean_nf = name_field.strip()
            if clean_nf and not clean_nf.endswith(".json") and clean_nf.lower() != "strategy":
                spec_name = spec_name or clean_nf

        template_name = str(data.get("template_name", "")).strip()
        if not spec_name and template_name:
            spec_name = template_name.replace("_", " ").title()

        clean_file = os.path.splitext(filename)[0]
        clean_file_fmt = clean_file.removesuffix("_strategy").replace("_", " ").title()
        strategy_name = spec_name or clean_file_fmt

        strategy_key = (
            data.get("strategy_key")
            or template_name
            or clean_file.removesuffix("_strategy")
        ).strip().lower()

        return strategy_key, strategy_name

    def resolve_keys(
        self,
        strategy_file: str,
        universe_file: Optional[str] = None,
        custom_symbols: Optional[List[str]] = None,
        custom_name: Optional[str] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Tuple[str, str, str, str]:
        """Resolve (strategy_key, universe_key, strategy_name, universe_name)."""
        # 1. Strategy
        strat_key = strategy_key or "default_strategy"
        strat_name = "Default Strategy"
        if strategy_file:
            strat_path = strategy_file
            if not os.path.isabs(strat_path):
                cand = os.path.join(self.strategy_dir, strategy_file)
                cand_repo = os.path.join(self.repo_root, strategy_file)
                if os.path.exists(cand):
                    strat_path = cand
                elif os.path.exists(cand_repo):
                    strat_path = cand_repo
            strat_data = self._load_json(strat_path, default={}) if os.path.exists(strat_path) else {}
            fname = os.path.basename(strat_path)
            extracted_key, extracted_name = self._extract_strategy_metadata(strat_data, fname)
            if not strategy_key:
                strat_key = extracted_key
            strat_name = extracted_name

        # 2. Universe
        # If custom_name is not provided but custom_symbols match a saved universe, auto-detect custom_name
        if not custom_name and custom_symbols:
            req_set = set(s.strip().upper() for s in custom_symbols if s and s.strip())
            for c in self.list_custom_universes():
                c_set = set(s.strip().upper() for s in c.get("symbols", []) if s and s.strip())
                if c_set and c_set == req_set:
                    custom_name = c.get("name")
                    break

        if custom_name:
            c_slug = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fa5]+", "_", custom_name).strip("_").lower()
            univ_key = universe_key or f"custom_{c_slug}"
            univ_name = f"★ {custom_name} (Custom)"
        elif custom_symbols and len(custom_symbols) > 0 and (not universe_file or universe_file == "__CUSTOM__"):
            sym_count = len(custom_symbols)
            univ_key = universe_key or f"custom_{sym_count}_symbols"
            univ_name = f"Custom Universe ({sym_count} symbols)"
        elif universe_file and universe_file != "__CUSTOM__":
            clean_u = os.path.basename(universe_file)
            folder = os.path.basename(os.path.dirname(universe_file))
            stem = os.path.splitext(clean_u)[0]
            if folder and folder not in ("universe", "docs", "."):
                raw_key = f"{folder}_{stem}"
            else:
                raw_key = stem
            derived_key = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fa5]+", "_", raw_key).strip("_").lower()
            univ_key = universe_key or derived_key
            univ_name = f"{folder}/{clean_u}" if folder and folder not in ("universe", "docs", ".") else clean_u
        else:
            univ_key = universe_key or "default_universe"
            univ_name = "Default Universe"

        return strat_key, univ_key, strat_name, univ_name

    def discover_strategies(self) -> List[Dict[str, Any]]:
        results = []
        dirs_to_scan = [self.strategy_dir]
        # Also include generator results if present and different
        gen_dir = os.path.join(self.repo_root, "pipeline", "strategy_generator", "results")
        if os.path.isdir(gen_dir) and gen_dir not in dirs_to_scan:
            dirs_to_scan.append(gen_dir)

        for d in dirs_to_scan:
            if not os.path.exists(d):
                continue
            for root, _, files in os.walk(d):
                for f in files:
                    if f.endswith(".json") and "strategy" in f.lower():
                        full_path = os.path.join(root, f)
                        rel_path = os.path.relpath(full_path, self.repo_root)
                        data = self._load_json(full_path)
                        if isinstance(data, dict) and "template_name" in data:
                            strategy_key, strategy_name = self._extract_strategy_metadata(data, f)
                            tpl = str(data.get("template_name", "")).lower()
                            params = data.get("params", {})
                            is_blend = "blend" in tpl or "blend" in strategy_key
                            category = "Blend Ensemble" if is_blend else "Single Strategy"
                            results.append({
                                "name": strategy_name,
                                "strategy_name": strategy_name,
                                "strategy_key": strategy_key,
                                "filename": f,
                                "path": full_path,
                                "rel_path": rel_path,
                                "template_name": data.get("template_name", ""),
                                "cash_proxy": params.get("cash_proxy", "BIL"),
                                "description": data.get("description", ""),
                                "is_blend": is_blend,
                                "category": category,
                                "weighting_mode": params.get("blend_weighting_mode"),
                                "orthogonalization": params.get("blend_enable_orthogonalization"),
                            })

        # Prioritize production blend strategies at the top of the UI selector
        def _strat_sort_key(x):
            k = x.get("strategy_key", "")
            if k == "chan_four_state_blend":
                return (0, x.get("strategy_name", ""))
            if k == "chan_risk_managed_blend":
                return (1, x.get("strategy_name", ""))
            if k == "chan_crisis_shield_blend":
                return (2, x.get("strategy_name", ""))
            if k == "chan_dual_hybrid_blend":
                return (3, x.get("strategy_name", ""))
            if x.get("is_blend"):
                return (4, x.get("strategy_name", ""))
            return (5, x.get("strategy_name", ""))

        results.sort(key=_strat_sort_key)
        return results

    def discover_universes(self) -> List[Dict[str, Any]]:
        universes = []
        # 1. Pre-defined from universe_dir
        if os.path.exists(self.universe_dir):
            for root, _, files in os.walk(self.universe_dir):
                for f in files:
                    if f.endswith(".txt"):
                        full_path = os.path.join(root, f)
                        rel_path = os.path.relpath(full_path, self.repo_root)
                        # Count symbols
                        symbols = []
                        try:
                            with open(full_path, "r", encoding="utf-8") as ufile:
                                for line in ufile:
                                    s = line.strip()
                                    if s and not s.startswith("#"):
                                        sym = s.replace(",", " ").split()[0].upper()
                                        symbols.append(sym)
                        except Exception:
                            symbols = []
                        # Derive friendly label and universe_key
                        folder = os.path.basename(os.path.dirname(full_path))
                        name = f"{folder}/{f}" if folder != os.path.basename(self.universe_dir) else f
                        stem = os.path.splitext(f)[0]
                        raw_key = f"{folder}_{stem}" if folder != os.path.basename(self.universe_dir) else stem
                        universe_key = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fa5]+", "_", raw_key).strip("_").lower()
                        universes.append({
                            "type": "preset",
                            "name": name,
                            "universe_key": universe_key,
                            "filename": f,
                            "path": full_path,
                            "rel_path": rel_path,
                            "symbols": symbols,
                            "count": len(symbols),
                        })

        # 2. Add saved custom universes
        customs = self.list_custom_universes()
        for c in customs:
            c_slug = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fa5]+", "_", c["name"]).strip("_").lower()
            universes.append({
                "type": "custom",
                "name": f"★ {c['name']} (Custom)",
                "universe_key": f"custom_{c_slug}",
                "custom_name": c["name"],
                "description": c.get("description", ""),
                "symbols": c.get("symbols", []),
                "count": len(c.get("symbols", [])),
                "path": None,
                "rel_path": None,
            })

        return universes

    # --------------------------------------------------------------------------
    # Holdings Management (Global + Daily Archive)
    # --------------------------------------------------------------------------
    def extract_post_run_holdings(self, run_dir: Optional[str]) -> Optional[Dict[str, Any]]:
        """Extract the resulting post-rebalance holdings from a run directory.

        Prioritizes:
          1. post_holdings.json (if explicitly saved -- an empty dict is a valid "all cash" state)
          2. target_shares in live_trading_ticket.json or live_trading_ticket.csv
          3. holdings.json or current_holdings.json snapshot in run_dir (no ticket => no trades)

        Returns None when the run carries no holdings information at all, so callers can tell
        "fully liquidated" ({}) apart from "unknown" and keep searching earlier runs only for the
        latter. (Previously a ticket whose targets were all 0 fell through to the PRE-trade
        snapshot and silently resurrected the liquidated positions on the next day.)
        """
        if not run_dir or not os.path.isdir(run_dir):
            return None

        # 1. Check if post_holdings.json exists
        post_file = os.path.join(run_dir, "post_holdings.json")
        if os.path.exists(post_file):
            data = self._load_json(post_file, default=None)
            if isinstance(data, dict):
                return data

        # 2. Check ticket for target_shares
        ticket = self._load_json(os.path.join(run_dir, "live_trading_ticket.json"))
        if not ticket:
            csv_path = os.path.join(run_dir, "live_trading_ticket.csv")
            if os.path.exists(csv_path):
                ticket = parse_ticket_csv(csv_path)
        target_holdings = post_holdings_from_ticket(ticket)
        if target_holdings is not None:
            return target_holdings

        # 3. Fallback to holdings.json or current_holdings.json in the run dir
        for fname in ("holdings.json", "current_holdings.json"):
            data = self._load_json(os.path.join(run_dir, fname), default=None)
            if isinstance(data, dict):
                return data

        return None

    def _load_snapshot_holdings(self, run_dir: Optional[str]) -> Optional[Dict[str, Any]]:
        """Holdings explicitly snapshotted for a run date (user-saved or Stage 2 pre-trade input)."""
        if not run_dir:
            return None
        for fname in ("holdings.json", "current_holdings.json"):
            data = self._load_json(os.path.join(run_dir, fname), default=None)
            if isinstance(data, dict):
                return data
        return None

    def _earlier_runs(
        self,
        date_str: Optional[str],
        strategy_key: Optional[str],
        universe_key: Optional[str],
    ) -> List[Dict[str, Any]]:
        """Runs for (strategy, universe) strictly before date_str (all runs if None), newest first."""
        if not strategy_key or not universe_key:
            return []
        runs = self.list_runs(strategy_key=strategy_key, universe_key=universe_key, require_both=True)
        if date_str:
            runs = [r for r in runs if r.get("date", "") < date_str]
        return runs

    def _carry_forward_holdings(
        self,
        date_str: Optional[str],
        strategy_key: Optional[str],
        universe_key: Optional[str],
        runs: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Post-trade holdings of the most recent earlier run that has any holdings information.

        Linear scan over one `list_runs` result -- replaces the old get_run -> get_previous_run ->
        get_run recursion, which re-scanned the whole archive per level and could hit the
        recursion limit after a long streak of Stage-1-only days.
        """
        if runs is None:
            runs = self._earlier_runs(date_str, strategy_key, universe_key)
        for r in runs:
            h = self.extract_post_run_holdings(r.get("path"))
            if h is not None:
                return h
        return None

    def _carry_forward_account_state(
        self,
        date_str: Optional[str],
        strategy_key: Optional[str],
        universe_key: Optional[str],
        runs: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """account_state.json of the most recent earlier run that has one (linear scan)."""
        if runs is None:
            runs = self._earlier_runs(date_str, strategy_key, universe_key)
        for r in runs:
            path = r.get("path")
            if not path:
                continue
            st = self._load_json(os.path.join(path, "account_state.json"))
            if st:
                return st
        return None

    def get_holdings(
        self,
        date_str: Optional[str] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Holdings at the START of date_str (i.e. the input to that day's Stage 2 run).

        Resolution order:
          1. An explicit snapshot for that exact date (holdings.json / current_holdings.json) --
             whether saved by the user or written by Stage 2 as its pre-trade input. This MUST win
             over the same day's post_holdings.json: otherwise a manual correction saved on date D
             is hidden by the strategy's assumed fills, and re-running Stage 2 on D reconciles
             against targets as if they were already filled (zero / wrong orders).
          2. Post-trade holdings carried forward from the most recent EARLIER run.
          3. Legacy flat archive snapshot, then the global current_holdings.json.
        With no date: post-trade holdings of the latest run for (strategy, universe).
        """
        date_str = validate_date_str(date_str)
        if date_str:
            run_dir = self.find_existing_run_dir(strategy_key, universe_key, date_str)
            snap = self._load_snapshot_holdings(run_dir)
            if snap is not None:
                return snap

            if strategy_key and universe_key:
                carried = self._carry_forward_holdings(date_str, strategy_key, universe_key)
                if carried is not None:
                    return carried

            legacy = self._load_json(os.path.join(self.archive_dir, date_str, "holdings.json"), default=None)
            if isinstance(legacy, dict):
                return legacy
        elif strategy_key and universe_key:
            latest = self._carry_forward_holdings(None, strategy_key, universe_key)
            if latest is not None:
                return latest

        return self._load_json(self.current_holdings_file, default={})

    def save_holdings(
        self,
        holdings: Dict[str, Any],
        date_str: Optional[str] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> None:
        date_str = validate_date_str(date_str)
        self._save_json(self.current_holdings_file, holdings)
        if date_str:
            daily_dir = self.get_run_dir(strategy_key, universe_key, date_str, prefer_existing=True)
            os.makedirs(daily_dir, exist_ok=True)
            self._save_json(os.path.join(daily_dir, "holdings.json"), holdings)

    # --------------------------------------------------------------------------
    # Market clock: single owner of the daily-archive date key (L2.4 / L2.5)
    # --------------------------------------------------------------------------
    def market_clock_markets(
        self,
        symbols: Optional[List[str]] = None,
        universe_file: Optional[str] = None,
        market: Optional[str] = None,
    ) -> List[str]:
        """Markets this book trades on, inferred from symbols (sorted, deduped).

        `market` (an operator's explicit `--market`) wins outright: when given, only
        that market votes.  Otherwise the symbols of `symbols` / `universe_file` vote,
        cash proxies (`BIL`/`CASH*`) are skipped -- they have no calendar and a US
        T-bill ETF in a China book would otherwise make it look cross-market -- and an
        uninferrable ticker abstains rather than raising.  Empty result means "no
        evidence"; the date resolver then falls back to China, the workspace default.
        """
        if market:
            return [str(market).strip().lower()]

        from common.market_clock import infer_market_from_symbol

        candidates = list(symbols or [])
        if not candidates:
            candidates = read_universe_symbols(universe_file)

        markets = set()
        for sym in candidates:
            if not sym or is_clock_cash_proxy(sym):
                continue
            try:
                markets.add(infer_market_from_symbol(str(sym).strip().upper()))
            except Exception:
                # Index/odd ticker: no calendar evidence, simply does not vote.
                continue
        return sorted(markets)

    def market_clock_date(
        self,
        symbols: Optional[List[str]] = None,
        universe_file: Optional[str] = None,
        market: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> str:
        """ISO date of the market clock's latest PUBLISHED session for this book.

        THE date-key resolver: the daily run directory, `summary.json`'s `date` /
        `as_of`, `get_multi_netting_run_dir`'s fallback and the `/api/*/latest`
        lookups all come from here, so they cannot disagree about which session a
        run belongs to.  A cross-market book takes the EARLIEST market's session
        (no symbol may be required to have published beyond its own close).

        `date.today()` must never appear as a fallback anywhere in this layer: the
        host's calendar day is a non-trading weekend on Saturdays and, intraday, a
        bar that does not exist yet.
        """
        try:
            from common.market_clock import expected_last_session
        except ImportError:  # pragma: no cover - repo root not importable yet
            if self.repo_root not in sys.path:
                sys.path.insert(0, self.repo_root)
            from common.market_clock import expected_last_session

        markets = self.market_clock_markets(symbols, universe_file, market=market)
        if not markets:
            markets = ["china"]
        return min(expected_last_session(m, now=now) for m in markets).isoformat()

    def get_quotes(
        self,
        symbols: List[str],
        as_of_date: Optional[str] = None,
        data_provider: Optional[str] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch latest quotes and reference market prices for a list of ticker symbols.

        (L2.3) This is a LIVE price path, so it may not fall back to the cache's lenient
        "we synced today" answer: the expected session is resolved and passed explicitly.
        Without an explicit `as_of_date` the target date is the MARKET clock's latest
        published session (never the host's `date.today()`), which is also what the cache
        must actually hold. A cross-market symbol list uses the EARLIEST of the involved
        markets' expected sessions, so no symbol is demanded to have published beyond its
        own market's close.
        """
        provider_name = data_provider or self.default_data_provider or "marketdb"

        from common.market_clock import (
            expected_last_session as _expected_last_session,
            infer_market_from_symbol,
        )

        clean_symbols = [str(s).strip().upper() for s in symbols if s and str(s).strip()]

        # Cash proxies have no meaningful calendar (they are priced at 1.0 below) and
        # BIL/CASH would otherwise make a single-market book look cross-market.
        market_symbols = [s for s in clean_symbols if s not in ("BIL", "CASH") and not s.startswith("CASH")]
        markets = set()
        for sym in market_symbols:
            try:
                markets.add(infer_market_from_symbol(sym))
            except Exception:
                continue
        clock_market = sorted(markets)[0] if markets else "china"
        clock_expected = _expected_last_session(clock_market)
        if len(markets) > 1:
            clock_expected = min(_expected_last_session(m) for m in markets)

        target_date = validate_date_str(as_of_date) or clock_expected.isoformat()
        quote_expected_session = (
            pd.Timestamp(target_date).date() if as_of_date else clock_expected
        )

        quotes: Dict[str, Dict[str, Any]] = {}
        for sym in clean_symbols:
            if sym in ("BIL", "CASH") or sym.startswith("CASH"):
                quotes[sym] = {
                    "symbol": sym,
                    "name": "流动性现金",
                    "price": 1.0,
                    "date": target_date,
                    "source": "cash_proxy",
                }

        needed_symbols = [s for s in clean_symbols if s not in quotes]
        if not needed_symbols:
            return {"quotes": quotes, "data_provider": provider_name, "as_of_date": target_date}

        # 1. Check if run archive exists for target_date or previous run with executed prices
        ticket_prices: Dict[str, float] = {}
        ticket_dates: Dict[str, str] = {}
        run_bundle = None
        if strategy_key and universe_key:
            run_bundle = self.get_run(target_date, strategy_key=strategy_key, universe_key=universe_key)
            if not run_bundle:
                run_bundle = self.get_previous_run(target_date, strategy_key=strategy_key, universe_key=universe_key)

        if run_bundle and run_bundle.get("trading_ticket"):
            bundle_date = run_bundle.get("date", target_date)
            for r in run_bundle["trading_ticket"]:
                if isinstance(r, dict) and r.get("symbol") and r.get("price") is not None:
                    try:
                        p_val = float(r["price"])
                        if p_val > 0:
                            ticket_prices[r["symbol"].upper()] = p_val
                            ticket_dates[r["symbol"].upper()] = bundle_date
                    except (ValueError, TypeError):
                        pass

        # 2. Query market data provider
        start_date = (pd.Timestamp(target_date) - timedelta(days=35)).strftime("%Y-%m-%d")
        loaded_universe: Dict[str, Any] = {}
        try:
            from common.data import load_universe
            loaded_universe = load_universe(
                needed_symbols,
                start=start_date,
                end=target_date,
                interval="1d",
                use_cache=True,
                cache_dir=self.cache_dir,
                provider=provider_name,
                expected_last_session=quote_expected_session,
            )
        except Exception as exc:
            print(f"[get_quotes] Warning: failed to load from provider '{provider_name}': {exc}")

        for sym in needed_symbols:
            stock_name = get_stock_name(sym, sym)
            has_ticket = sym in ticket_prices
            t_price = ticket_prices.get(sym)
            t_date = ticket_dates.get(sym, "")

            df = loaded_universe.get(sym)
            has_df = df is not None and hasattr(df, "empty") and not df.empty and "Close" in df.columns
            if has_df:
                last_price = float(df["Close"].iloc[-1])
                last_date = df.index[-1].strftime("%Y-%m-%d") if hasattr(df.index[-1], "strftime") else str(df.index[-1])[:10]
                # (L2.3) Same single freshness owner as the live ticket paths; a UI quote
                # may still be shown for a stale bar, but it must SAY so rather than look
                # like the current price (the 2026-10-22 defect, in display form).
                price_is_stale = is_stale_bar(df, quote_expected_session.isoformat())

                # If provider is synthetic but we have real executed ticket prices, NEVER overwrite with synthetic!
                if has_ticket and provider_name == "synthetic":
                    quotes[sym] = {
                        "symbol": sym,
                        "name": stock_name,
                        "price": t_price,
                        "date": t_date or target_date,
                        "source": "ticket_archive",
                    }
                # If ticket price date is newer than or equal to provider date, prefer real ticket price
                elif has_ticket and t_date and t_date >= last_date:
                    quotes[sym] = {
                        "symbol": sym,
                        "name": stock_name,
                        "price": t_price,
                        "date": t_date,
                        "source": "ticket_archive",
                    }
                else:
                    quotes[sym] = {
                        "symbol": sym,
                        "name": stock_name,
                        "price": last_price,
                        "date": last_date,
                        "source": provider_name,
                        "stale": price_is_stale,
                        "expected_session": quote_expected_session.isoformat(),
                    }
            elif has_ticket:
                quotes[sym] = {
                    "symbol": sym,
                    "name": stock_name,
                    "price": t_price,
                    "date": t_date or target_date,
                    "source": "ticket_archive",
                }
            else:
                quotes[sym] = {
                    "symbol": sym,
                    "name": stock_name,
                    "price": None,
                    "date": None,
                    "source": "not_found",
                }

        return {"quotes": quotes, "data_provider": provider_name, "as_of_date": target_date}

    # --------------------------------------------------------------------------
    # Account State Management (Global + Daily Archive)
    # --------------------------------------------------------------------------
    def get_account_state(
        self,
        date_str: Optional[str] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        date_str = validate_date_str(date_str)
        if date_str:
            # 1. Exact run dir for date_str
            run_dir = self.find_existing_run_dir(strategy_key, universe_key, date_str)
            if run_dir:
                d = self._load_json(os.path.join(run_dir, "account_state.json"))
                if d:
                    return d

            # 2. Carry forward from the most recent earlier run that has an account state
            if strategy_key and universe_key:
                carried = self._carry_forward_account_state(date_str, strategy_key, universe_key)
                if carried:
                    return carried

            legacy = self._load_json(os.path.join(self.archive_dir, date_str, "account_state.json"))
            if legacy:
                return legacy
        elif strategy_key and universe_key:
            latest = self._carry_forward_account_state(None, strategy_key, universe_key)
            if latest:
                return latest

        return self._load_json(
            self.account_state_file,
            default={
                "as_of_date": None,
                "current_nav": 100000.0,
                "peak_nav": 100000.0,
                "circuit_breaker_tier": "NORMAL",
                "circuit_breaker_label": "🟢 NORMAL",
                "linear_equity_scale": 1.0,
                "tier1_consecutive_bars": 0,
                "tier3_consecutive_bars": 0,
                "freeze_remaining_bars": 0,
                "nav_history_10d": [],
                "holdings": {},
            },
        )

    def save_account_state(
        self,
        state: Dict[str, Any],
        date_str: Optional[str] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> None:
        date_str = validate_date_str(date_str)
        self._save_json(self.account_state_file, state)
        if date_str:
            daily_dir = self.get_run_dir(strategy_key, universe_key, date_str, prefer_existing=True)
            os.makedirs(daily_dir, exist_ok=True)
            self._save_json(os.path.join(daily_dir, "account_state.json"), state)

    # --------------------------------------------------------------------------
    # Daily Archive Runs Management (Keyed by Strategy and Universe)
    # --------------------------------------------------------------------------
    def _assert_inside_archive(self, path: str) -> str:
        """Defense in depth against path traversal: every archive path must stay under archive_dir."""
        root = os.path.realpath(self.archive_dir)
        real = os.path.realpath(path)
        if os.path.commonpath([root, real]) != root:
            raise ValueError(f"Refusing archive path outside archive_dir: {path}")
        return path

    def find_existing_run_dir(
        self,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
        date_str: Optional[str] = None,
    ) -> Optional[str]:
        """Find an existing run directory on disk, taking into account universe key variants and case insensitivity.

        Search order is deterministic (exact path, then naming variants, then sorted directory
        listings preferring exact case-insensitive matches) so the same keys always resolve to the
        same folder regardless of filesystem listdir order.
        """
        strategy_key = sanitize_key(strategy_key)
        universe_key = sanitize_key(universe_key)
        date_str = validate_date_str(date_str)

        def _matches(p: str) -> bool:
            if not os.path.exists(p):
                return False
            if date_str:
                return is_run_dir(p)
            return os.path.isdir(p)

        def _join(*parts: str) -> str:
            return os.path.join(*[p for p in parts if p])

        if not strategy_key or not universe_key:
            if date_str:
                flat_legacy = os.path.join(self.archive_dir, date_str)
                if _matches(flat_legacy):
                    return flat_legacy
            return None

        # 1. Direct candidate
        cand = self.get_run_dir(strategy_key, universe_key, date_str, prefer_existing=False)
        if _matches(cand):
            return cand

        # 2. Check universe key variants under strategy_key
        for u_var in get_universe_key_variants(universe_key):
            cand_var = self.get_run_dir(strategy_key, u_var, date_str, prefer_existing=False)
            if _matches(cand_var):
                return cand_var

        def _ordered_matches(entries: List[str], key: str) -> List[str]:
            exact = sorted(e for e in entries if e.lower() == key.lower())
            fuzzy = sorted(e for e in entries if e not in exact and keys_match(e, key))
            return exact + fuzzy

        # 3/4. Scan strategy folders (exact-case first, then case-insensitive) for a matching universe
        if os.path.isdir(self.archive_dir):
            for s_entry in _ordered_matches(os.listdir(self.archive_dir), strategy_key):
                s_path = os.path.join(self.archive_dir, s_entry)
                if not os.path.isdir(s_path):
                    continue
                for u_entry in _ordered_matches(os.listdir(s_path), universe_key):
                    cand_entry = _join(s_path, u_entry, date_str or "")
                    if _matches(cand_entry):
                        return cand_entry

        return None

    def get_run_dir(
        self,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
        date_str: Optional[str] = None,
        prefer_existing: bool = True,
    ) -> str:
        """Construct hierarchical archive directory: <archive_dir>/<strategy_key>/<universe_key>/<date_str>.
        If prefer_existing is True and a matching strategy/universe folder already exists on disk,
        reuse that folder to prevent directory fragmentation.

        Keys are sanitized to single safe path components and date_str must be YYYY-MM-DD: both
        arrive from HTTP query strings / bodies, and an unsanitized '../..' key previously let a
        POST /api/holdings write files outside the archive.
        """
        strategy_key = sanitize_key(strategy_key)
        universe_key = sanitize_key(universe_key)
        date_str = validate_date_str(date_str)

        if prefer_existing and strategy_key and universe_key:
            existing_univ_dir = self.find_existing_run_dir(strategy_key, universe_key, date_str=None)
            if existing_univ_dir and os.path.isdir(existing_univ_dir):
                if date_str:
                    return self._assert_inside_archive(os.path.join(existing_univ_dir, date_str))
                return self._assert_inside_archive(existing_univ_dir)

        parts = [self.archive_dir]
        if strategy_key and universe_key:
            parts.append(strategy_key)
            parts.append(universe_key)
        if date_str:
            parts.append(date_str)
        return self._assert_inside_archive(os.path.join(*parts))

    def save_daily_run(
        self,
        date_str: str,
        health_data: Optional[Dict[str, Any]] = None,
        ticket_data: Optional[List[Dict[str, Any]]] = None,
        csv_content: Optional[str] = None,
        summary: Optional[Dict[str, Any]] = None,
        holdings: Optional[Dict[str, Any]] = None,
        account_state: Optional[Dict[str, Any]] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
        strategy_name: Optional[str] = None,
        universe_name: Optional[str] = None,
    ) -> str:
        """Atomically saves all artifacts for date_str in archive_dir/<strategy_key>/<universe_key>/<date_str>/."""
        run_dir = self.get_run_dir(strategy_key, universe_key, date_str, prefer_existing=True)
        os.makedirs(run_dir, exist_ok=True)

        if summary is None:
            summary = {}
        if strategy_key:
            summary["strategy_key"] = strategy_key
        if universe_key:
            summary["universe_key"] = universe_key
        if strategy_name:
            summary["strategy_name"] = strategy_name
        if universe_name:
            summary["universe_name"] = universe_name
        summary["date"] = date_str

        if health_data is not None:
            self._save_json(os.path.join(run_dir, "stage1_health_report.json"), health_data)
        if ticket_data is not None:
            if isinstance(ticket_data, list):
                for r in ticket_data:
                    if isinstance(r, dict):
                        name_val = str(r.get("name", "")).strip()
                        if not name_val or name_val.lower() in ("custom", "universe", "none", "unknown") or name_val == r.get("symbol"):
                            r["name"] = get_stock_name(r.get("symbol", ""), r.get("symbol", ""))
            self._save_json(os.path.join(run_dir, "live_trading_ticket.json"), ticket_data)
        if csv_content is not None:
            with open(os.path.join(run_dir, "live_trading_ticket.csv"), "w", encoding="utf-8") as f:
                f.write(csv_content)
        if summary:
            self._save_json(os.path.join(run_dir, "summary.json"), summary)
        if holdings is not None:
            self._save_json(os.path.join(run_dir, "holdings.json"), holdings)
            # also update global
            self._save_json(self.current_holdings_file, holdings)
        if account_state is not None:
            self._save_json(os.path.join(run_dir, "account_state.json"), account_state)
            # also update global
            self._save_json(self.account_state_file, account_state)

        return run_dir

    def list_runs(
        self,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
        require_both: bool = False,
    ) -> List[Dict[str, Any]]:
        """List archived runs, optionally filtered strictly by strategy and universe."""
        if require_both and (not strategy_key or not universe_key):
            return []

        if not os.path.exists(self.archive_dir):
            return []

        runs = []
        custom_universes = self.list_custom_universes()
        # Walk archive_dir to discover run directories (both 3-level and legacy 1-level)
        for root, dirs, files in os.walk(self.archive_dir):
            if (
                "stage1_health_report.json" in files
                or "live_trading_ticket.json" in files
                or "netted_trading_ticket.json" in files
                or "summary.json" in files
                or "live_trading_ticket.csv" in files
                or "netted_trading_ticket.csv" in files
            ):
                health_path = os.path.join(root, "stage1_health_report.json")
                ticket_path = os.path.join(root, "live_trading_ticket.json")
                if not os.path.exists(ticket_path):
                    ticket_path = os.path.join(root, "netted_trading_ticket.json")
                csv_path = os.path.join(root, "live_trading_ticket.csv")
                if not os.path.exists(csv_path):
                    csv_path = os.path.join(root, "netted_trading_ticket.csv")
                summary_path = os.path.join(root, "summary.json")

                health_rep = self._load_json(health_path, default={})
                ticket_rep = self._load_json(ticket_path, default=[])
                summary_rep = self._load_json(summary_path, default={})

                if not ticket_rep and os.path.exists(csv_path):
                    ticket_rep = parse_ticket_csv(csv_path)

                base_name = os.path.basename(root)
                # Determine date from summary or directory basename
                date_val = summary_rep.get("date")
                if not date_val:
                    if base_name.startswith("multi_netted_"):
                        date_val = os.path.basename(os.path.dirname(root))
                    else:
                        date_val = base_name

                # Determine strategy and universe keys
                r_strat_key = summary_rep.get("strategy_key") or summary_rep.get("matrix_id")
                if not r_strat_key and base_name.startswith("multi_netted_"):
                    r_strat_key = base_name.replace("multi_netted_", "")

                r_univ_key = summary_rep.get("universe_key")
                if not r_univ_key and base_name.startswith("multi_netted_"):
                    r_univ_key = "multi_matrix"

                r_strat_name = summary_rep.get("strategy_name") or summary_rep.get("name")
                r_univ_name = summary_rep.get("universe_name")

                # If missing from summary, deduce from relative path or strategy_file
                rel_parts = os.path.relpath(root, self.archive_dir).split(os.sep)
                if len(rel_parts) >= 3:
                    if not r_strat_key:
                        r_strat_key = rel_parts[0]
                    if not r_univ_key:
                        r_univ_key = rel_parts[1]
                elif not base_name.startswith("multi_netted_"):
                    # Legacy flat runs
                    strat_file = summary_rep.get("strategy_file") or ""
                    univ_file = summary_rep.get("universe_file") or ""
                    sk, uk, sn, un = self.resolve_keys(strat_file, univ_file)
                    if not r_strat_key:
                        r_strat_key = sk
                    if not r_univ_key:
                        r_univ_key = uk
                    if not r_strat_name:
                        r_strat_name = sn
                    if not r_univ_name:
                        r_univ_name = un

                if not r_univ_name and r_univ_key:
                    for c in custom_universes:
                        if keys_match(c.get("name", ""), r_univ_key):
                            r_univ_name = f"★ {c.get('name')} (Custom)"
                            break
                    if not r_univ_name:
                        r_univ_name = (r_univ_key or "Universe").replace("_", " ")

                if not r_strat_name:
                    r_strat_name = (r_strat_key or "Strategy").replace("_", " ").title()

                # Apply filters using keys_match
                if require_both:
                    if not r_strat_key or not r_univ_key:
                        continue
                    if strategy_key and not keys_match(strategy_key, r_strat_key):
                        continue
                    if universe_key and not keys_match(universe_key, r_univ_key):
                        continue
                else:
                    if strategy_key and (not r_strat_key or not keys_match(strategy_key, r_strat_key)):
                        continue
                    if universe_key and (not r_univ_key or not keys_match(universe_key, r_univ_key)):
                        continue

                directive = health_rep.get("directive") or health_rep.get("gate_code", "UNKNOWN")
                tier = health_rep.get("circuit_breaker_tier") or health_rep.get("portfolio_health", {}).get("tier", "NORMAL")
                trade_count = len([x for x in ticket_rep if isinstance(x, dict) and x.get("action") in ("BUY", "SELL")])

                runs.append({
                    "date": date_val,
                    "path": root,
                    "strategy_key": r_strat_key or "",
                    "strategy_name": r_strat_name,
                    "universe_key": r_univ_key or "",
                    "universe_name": r_univ_name,
                    "has_health": os.path.exists(health_path),
                    "has_ticket": os.path.exists(ticket_path) or os.path.exists(csv_path),
                    "directive": directive,
                    "tier": tier,
                    "trade_count": trade_count,
                    "timestamp": summary_rep.get("timestamp", ""),
                    "strategy": summary_rep.get("strategy_file", ""),
                    "universe": summary_rep.get("universe_file", ""),
                })

        # Sort by date descending, then timestamp descending
        runs.sort(key=lambda x: (x.get("date", ""), x.get("timestamp", "")), reverse=True)
        return runs

    def get_latest_run(
        self,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve latest run bundle for a specific strategy and universe."""
        runs = self.list_runs(strategy_key=strategy_key, universe_key=universe_key, require_both=True)
        if not runs:
            return None
        top_run = runs[0]
        return self.get_run(
            top_run["date"],
            strategy_key=top_run.get("strategy_key") or strategy_key,
            universe_key=top_run.get("universe_key") or universe_key,
        )

    def get_previous_run(
        self,
        date_str: Optional[str] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve the latest archived run strictly before date_str (or latest available if date_str is None)."""
        both_provided = bool(strategy_key and universe_key)
        runs = self.list_runs(strategy_key=strategy_key, universe_key=universe_key, require_both=both_provided)
        if not runs:
            return None

        if date_str:
            earlier_runs = [r for r in runs if r.get("date", "") < date_str]
            if not earlier_runs:
                return None
            chosen = earlier_runs[0]
        else:
            chosen = runs[0]

        return self.get_run(
            chosen["date"],
            strategy_key=chosen.get("strategy_key") or strategy_key,
            universe_key=chosen.get("universe_key") or universe_key,
        )

    def get_run(
        self,
        date_str: str,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve complete archived artifacts for a date, optionally matching strategy & universe."""
        run_dir = self.find_existing_run_dir(strategy_key, universe_key, date_str)
        if not run_dir and strategy_key and date_str:
            cand_netted = self.get_multi_netting_run_dir(strategy_key, date_str)
            if is_run_dir(cand_netted):
                run_dir = cand_netted
        if not run_dir:
            # Search list_runs
            matching = [
                r for r in self.list_runs(strategy_key=strategy_key, universe_key=universe_key)
                if r["date"] == date_str
            ]
            if matching:
                run_dir = matching[0]["path"]
            else:
                legacy = os.path.join(self.archive_dir, date_str)
                if is_run_dir(legacy):
                    run_dir = legacy

        if not run_dir or not is_run_dir(run_dir):
            return None

        summary_data = self._load_json(os.path.join(run_dir, "summary.json"), default={})
        csv_path = os.path.join(run_dir, "live_trading_ticket.csv")
        json_path = os.path.join(run_dir, "live_trading_ticket.json")
        if not os.path.exists(json_path) and not os.path.exists(csv_path):
            netted_json = os.path.join(run_dir, "netted_trading_ticket.json")
            netted_csv = os.path.join(run_dir, "netted_trading_ticket.csv")
            if os.path.exists(netted_json):
                json_path = netted_json
            if os.path.exists(netted_csv):
                csv_path = netted_csv

        ticket = None
        if os.path.exists(csv_path) and os.path.exists(json_path):
            if os.path.getmtime(csv_path) > os.path.getmtime(json_path):
                ticket = parse_ticket_csv(csv_path)
            else:
                ticket = self._load_json(json_path)
        elif os.path.exists(json_path):
            ticket = self._load_json(json_path)
        elif os.path.exists(csv_path):
            ticket = parse_ticket_csv(csv_path)

        if ticket and isinstance(ticket, list):
            for r in ticket:
                if isinstance(r, dict):
                    name_val = str(r.get("name", "")).strip()
                    if is_placeholder_stock_name(name_val, r.get("symbol")):
                        r["name"] = get_stock_name(r.get("symbol", ""), r.get("symbol", ""))

        strat_k = summary_data.get("strategy_key") or summary_data.get("matrix_id") or strategy_key or ""
        univ_k = summary_data.get("universe_key") or universe_key or ""
        strat_n = summary_data.get("strategy_name", "")
        univ_n = summary_data.get("universe_name", "")

        # Holdings extraction: pre-trade snapshot first (holdings.json / current_holdings.json),
        # then carry forward from earlier runs (< date_str).
        # We must NOT fall back to extract_post_run_holdings for bundle["holdings"],
        # because bundle["holdings"] is used as the pre-trade baseline in the UI!
        holdings_data = self._load_json(os.path.join(run_dir, "holdings.json"))
        if not holdings_data:
            holdings_data = self._load_json(os.path.join(run_dir, "current_holdings.json"))
        if not holdings_data and strat_k and univ_k:
            holdings_data = self._carry_forward_holdings(date_str, strat_k, univ_k)
        if not holdings_data:
            holdings_data = self.get_holdings(date_str, strat_k, univ_k)

        post_holdings_data = self.extract_post_run_holdings(run_dir)

        account_state = self._load_json(os.path.join(run_dir, "account_state.json"))
        if not account_state and strat_k and univ_k:
            account_state = self._carry_forward_account_state(date_str, strat_k, univ_k)

        # If summary didn't have nice names, resolve from custom universes / strategies
        if not univ_n and univ_k:
            for c in self.list_custom_universes():
                if keys_match(c.get("name", ""), univ_k):
                    univ_n = f"★ {c.get('name')} (Custom)"
                    break
            if not univ_n:
                univ_n = univ_k.replace("_", " ").title()

        if not strat_n and strat_k:
            strat_n = strat_k.replace("_", " ").title()

        prices_map: Dict[str, float] = {}
        if ticket and isinstance(ticket, list):
            for r in ticket:
                if isinstance(r, dict) and r.get("symbol") and r.get("price") is not None:
                    try:
                        p_val = float(r["price"])
                        if p_val > 0:
                            prices_map[str(r["symbol"]).upper()] = p_val
                    except (ValueError, TypeError):
                        pass

        friction_savings = self._load_json(os.path.join(run_dir, "friction_savings_report.json"))
        sub_ledgers = self._load_json(os.path.join(run_dir, "sub_ledgers.json"))

        return {
            "date": date_str,
            "path": run_dir,
            "strategy_key": strat_k,
            "strategy_name": strat_n,
            "universe_key": univ_k,
            "universe_name": univ_n,
            "health_report": self._load_json(os.path.join(run_dir, "stage1_health_report.json")),
            "trading_ticket": ticket,
            "friction_savings": friction_savings,
            "sub_ledgers": sub_ledgers,
            "summary": summary_data,
            "holdings": holdings_data or {},
            "post_holdings": post_holdings_data or {},
            "prices": prices_map,
            "account_state": account_state,
            "has_csv": os.path.exists(os.path.join(run_dir, "live_trading_ticket.csv")) or os.path.exists(os.path.join(run_dir, "netted_trading_ticket.csv")),
        }

    def get_run_csv(
        self,
        date_str: str,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Optional[str]:
        run_dir = self.find_existing_run_dir(strategy_key, universe_key, date_str)
        if not run_dir and strategy_key and date_str:
            cand_netted = self.get_multi_netting_run_dir(strategy_key, date_str)
            if is_run_dir(cand_netted):
                run_dir = cand_netted
        if not run_dir:
            matching = [
                r for r in self.list_runs(strategy_key=strategy_key, universe_key=universe_key)
                if r["date"] == date_str
            ]
            if matching:
                run_dir = matching[0]["path"]
            else:
                legacy = os.path.join(self.archive_dir, date_str)
                if is_run_dir(legacy):
                    run_dir = legacy

        if not run_dir or not is_run_dir(run_dir):
            return None

        for cand_name in ("live_trading_ticket.csv", "netted_trading_ticket.csv"):
            cand_p = os.path.join(run_dir, cand_name)
            if os.path.exists(cand_p):
                with open(cand_p, "r", encoding="utf-8") as f:
                    return f.read()
        return None

    # ==========================================================================
    # Multi-Strategy Order Netting & Portfolio Matrix Management
    # ==========================================================================
    def get_multi_strategy_presets(self) -> List[Dict[str, Any]]:
        """Default recommended multi-strategy collaborative matrix presets."""
        return [
            {
                "matrix_id": "tri_core_blend",
                "name": "三核协同旗舰组合 (Tri-Core Blend Matrix)",
                "description": "危机盾40% + 风控盾35% + 四象危机盾25% 协同撮合净额化",
                "universe_file": "docs/universe/china/pruned_core_satellite_blend_16.txt",
                "portfolio_value": 100000.0,
                "strategies": [
                    {"strategy_key": "chan_crisis_shield_blend", "capital_weight": 0.40, "name": "Chan Single Crisis Shield Blend"},
                    {"strategy_key": "chan_risk_managed_blend", "capital_weight": 0.35, "name": "Chan Risk-Managed Blend"},
                    {"strategy_key": "chan_four_state_shield_blend", "capital_weight": 0.25, "name": "Chan Four-State Shield Blend"},
                ],
            },
            {
                "matrix_id": "dual_core_shield",
                "name": "双核攻守对冲组合 (Dual-Core Crisis-Defense Matrix)",
                "description": "危机盾50% + 风控盾50% 经典双核平衡净额化",
                "universe_file": "docs/universe/china/pruned_core_satellite_blend_16.txt",
                "portfolio_value": 100000.0,
                "strategies": [
                    {"strategy_key": "chan_crisis_shield_blend", "capital_weight": 0.50, "name": "Chan Single Crisis Shield Blend"},
                    {"strategy_key": "chan_risk_managed_blend", "capital_weight": 0.50, "name": "Chan Risk-Managed Blend"},
                ],
            },
            {
                "matrix_id": "four_state_duo",
                "name": "缠论四象主升组合 (Four-State Alpha Duo)",
                "description": "四象危机盾50% + 四象风控盾50% 拒绝盘整纯趋势配置",
                "universe_file": "docs/universe/china/pruned_core_satellite_blend_16.txt",
                "portfolio_value": 100000.0,
                "strategies": [
                    {"strategy_key": "chan_four_state_shield_blend", "capital_weight": 0.50, "name": "Chan Four-State Shield Blend"},
                    {"strategy_key": "chan_four_state_blend", "capital_weight": 0.50, "name": "Chan Four-State Risk-Managed Blend"},
                ],
            },
        ]

    def get_multi_strategy_configs(self) -> List[Dict[str, Any]]:
        """Load configured multi-strategy matrix presets."""
        cfg_file = os.path.join(self.data_dir, "multi_strategy_configs.json")
        saved = self._load_json(cfg_file, default=None)
        if saved and isinstance(saved, list) and len(saved) > 0:
            return saved
        # Return default presets and save
        presets = self.get_multi_strategy_presets()
        self._save_json(cfg_file, presets)
        return presets

    def save_multi_strategy_config(self, config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Save or update a multi-strategy matrix preset."""
        configs = self.get_multi_strategy_configs()
        m_id = config.get("matrix_id")
        if not m_id:
            m_id = sanitize_key(config.get("name", "custom_matrix")) or "custom_matrix"
            config["matrix_id"] = m_id

        found = False
        for i, c in enumerate(configs):
            if c.get("matrix_id") == m_id:
                configs[i] = config
                found = True
                break
        if not found:
            configs.append(config)

        cfg_file = os.path.join(self.data_dir, "multi_strategy_configs.json")
        self._save_json(cfg_file, configs)
        return configs

    def get_multi_netting_run_dir(self, matrix_id: str, date_str: str) -> str:
        """Resolve archive directory path for a multi-strategy netting run.

        (L2.5) The date component is an ARCHIVE KEY, so it has exactly one source:
        the caller's explicit `as_of` when one was given, else the market clock
        (`market_clock_date`) -- never the host's `date.today()`.  An *omitted*
        date falls back to the clock; a *malformed* one is rejected by
        `validate_date_str` (this string is a path component, so silently
        substituting any other day would both hide the caller's bug and let an
        unsanitized key reach the filesystem).
        """
        clean_m = sanitize_key(matrix_id) or "matrix"
        clean_d = validate_date_str(date_str) or self.market_clock_date()
        return os.path.join(self.archive_dir, clean_d, f"multi_netted_{clean_m}")

    def get_multi_netting_run(self, date_str: str, matrix_id: str) -> Optional[Dict[str, Any]]:
        """Load netted run artifacts for specified matrix and date."""
        run_dir = self.get_multi_netting_run_dir(matrix_id, date_str)
        if not os.path.exists(run_dir):
            # No dated archive run for this matrix: fall back to the directory the
            # netting CLI writes when it is invoked without `--output-dir` (a
            # CLI-produced ticket is never copied into the archive). Resolved by
            # `scripts/live_paths.py` -- the same owner as the writer, so this can
            # never point at a code tree the CLI has stopped writing to (as
            # `docs/ruleset/multi_strategy_netted` now is).
            fallback_dir = MULTI_NETTING_FALLBACK_DIR
            if os.path.exists(fallback_dir) and os.path.exists(os.path.join(fallback_dir, "netted_trading_ticket.json")):
                run_dir = fallback_dir
            else:
                return None

        ticket_json = self._load_json(os.path.join(run_dir, "netted_trading_ticket.json"), default=[])
        savings_json = self._load_json(os.path.join(run_dir, "friction_savings_report.json"), default={})
        ledgers_json = self._load_json(os.path.join(run_dir, "sub_ledgers.json"), default={})
        summary_json = self._load_json(os.path.join(run_dir, "summary.json"), default={})

        return {
            "date": date_str,
            "matrix_id": matrix_id,
            "path": run_dir,
            "trading_ticket": ticket_json,
            "friction_savings": savings_json,
            "sub_ledgers": ledgers_json,
            "summary": summary_json,
            "has_csv": os.path.exists(os.path.join(run_dir, "netted_trading_ticket.csv")),
        }


