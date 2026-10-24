#!/usr/bin/env python3
"""Run-local, OVERRIDDEN COPIES of the blend strategy dumps -- never the live ones.

Why this module exists (2026-10-24 live-trading incident)
--------------------------------------------------------
`pipeline/scripts/backtest_blend_strategies.sh` used to run
`run_research_strategy.py --dump-strategies` and then, in its Step 2, stamp a
SINGLE global mode into all six
`research_strategy/results/strategy_dumps/*blend*.json` files IN PLACE:

    params['blend_weighting_mode'] = ${WEIGHTING_MODE:-rolling_ic_ir}   # + 3 aliases

Those dumps are not scratch files. `scripts/run_live_strategy.py`'s
`DEFAULT_STRATEGY_FILE` *is* `chan_four_state_blend_strategy.json` and nothing on
that path re-reads `strategies_config.json`, so whatever the last blend backtest
left behind is what a live order ticket is sized by. `chan_four_state_blend` is
pinned to `blend_weighting_mode="regime_ic_ir_hybrid"`
(`pipeline/research_strategy/strategies_config.json`, AGENTS.md section 8 -- the
documented exception), so a plain wrapper run silently switched the DEPLOYED
engine to `rolling_ic_ir`: `blend_base.py:445` then returns before the `regime_*`
branch at `:465`, making the `bucket_renorm_max_delta` clamp (`:522-529`) and the
thrust hysteresis inert. `check_live_portfolio_health.py:1144-1152` mislabelled
the mode for the same reason, and the web station shells out to both.

The structural flaw is that ONE mode for all six blends cannot be correct: no
single `WEIGHTING_MODE` value expresses a repo where one strategy pins a
different mode. So the wrapper cannot keep that shape at all.

The fix (this module)
---------------------
An ablation still needs to *explore* modes, so the override capability stays --
it is just redirected at a run-local COPY:

    materialise_dump_overrides(source_dir, dest_dir, weighting_mode=...,
                               enable_orthogonalization=..., extra_params=...,
                               pinned_modes={...})  -> OverrideReport

`source_dir` (the live dumps) is opened read-only and is never written, renamed
or chmod-ed. Every matched dump is copied into `dest_dir`; when a mode and/or an
orthogonalization flag is requested, each copy additionally gets the PREFIXED
`blend_weighting_mode` / `blend_enable_orthogonalization` keys written into
`params` and mirrored into `research_strategy_spec.entry_data.parameters` (the
two containers a clean dump keeps in lockstep). The batch then consumes
`<dest_dir>/*blend*.json` via its existing `--strategies-glob`.

Deliberate detail -- PREFIXED keys only: the old Step 2 also wrote the
unprefixed aliases `weighting_mode` / `enable_orthogonalization`. Those are a
second source of truth (`BaseBlendAllocationStrategy.resolve_param` precedence 3
is `blend_<name>`, precedence 4 the bare name) and are exactly the pair that lied
in this incident; `pipeline/tests/test_strategy_dumps_invariants.py` bans them
from dumps. Nothing reads them when the prefixed key is present, so they are not
reintroduced here.

Atomicity: every copy is written into a sibling `.staging.` directory first and
the finished directory is then renamed into place, so a failure mid-write leaves
both the live dumps AND any previous override directory untouched -- there is no
state in which `dest_dir` holds a half-written blend family.

Offline: reads local JSON, writes local JSON. No data provider, no network.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

#: Filename pattern the wrapper's batch step consumes (`*blend*.json`), kept here
#: so the helper and the shell cannot drift apart.
DEFAULT_BLEND_PATTERN = "*blend*.json"

#: Directory name of the override copy inside the run's `--results` directory.
OVERRIDE_DIR_NAME = "_strategy_dumps_override"

#: The keys `BaseBlendAllocationStrategy.resolve_param` actually reads
#: (precedence 3 -- above the bare, unprefixed spelling). See module docstring.
WEIGHTING_MODE_KEY = "blend_weighting_mode"
ORTHOGONALIZATION_KEY = "blend_enable_orthogonalization"

_STRATEGY_FILE_SUFFIX = "_strategy.json"

#: The documented AGENTS.md section-8 exception. Named literally (not looked up)
#: so the warning that quotes it cannot be quietly silenced by a config edit.
PINNED_EXCEPTION_STRATEGY = "chan_four_state_blend"
PINNED_EXCEPTION_MODE = "regime_ic_ir_hybrid"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DUMPS_DIR = os.path.join(
    _REPO_ROOT, "pipeline", "research_strategy", "results", "strategy_dumps"
)
DEFAULT_CONFIG_PATH = os.path.join(
    _REPO_ROOT, "pipeline", "research_strategy", "strategies_config.json"
)


# --------------------------------------------------------------------------- #
# Report types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PinnedDeviation:
    """One strategy whose configured/pinned mode the request deliberately departs from.

    `strategy` is the CONFIG key (dump filename minus `_strategy.json`), i.e.
    `chan_four_state_blend`, not `chan_four_state_blend_strategy`.
    """

    strategy: str
    pinned_mode: str
    requested_mode: str

    @property
    def is_documented_exception(self) -> bool:
        return self.strategy == PINNED_EXCEPTION_STRATEGY


@dataclass(frozen=True)
class OverrideReport:
    """What `materialise_dump_overrides` did, in a form the shell can print.

    * `written`   -- every strategy copied into `dest_dir` (in glob order)
    * `overridden`-- the subset carrying the requested override (mode and/or ortho)
    * `unchanged` -- the subset copied byte-for-byte (nothing requested, or the
                     request produced no key: e.g. ortho-only requests leave
                     `blend_weighting_mode()` alone but still count as overridden)
    * `pinned_deviations` -- requests that depart from a strategy's pinned mode;
                     these strategies ARE still in `overridden` (an ablation is
                     allowed to explore), the tuple only marks them as deviations
    * `applied`   -- the keys actually written into every overridden copy
    """

    source_dir: str
    dest_dir: str
    written: Tuple[str, ...] = ()
    overridden: Tuple[str, ...] = ()
    unchanged: Tuple[str, ...] = ()
    pinned_deviations: Tuple[PinnedDeviation, ...] = ()
    applied: Tuple[Tuple[str, Any], ...] = ()

    @property
    def has_pinned_deviation(self) -> bool:
        return bool(self.pinned_deviations)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def strategy_key_for(filename: str) -> str:
    """`chan_four_state_blend_strategy.json` -> `chan_four_state_blend`."""
    base = os.path.basename(filename)
    if base.endswith(_STRATEGY_FILE_SUFFIX):
        return base[: -len(_STRATEGY_FILE_SUFFIX)]
    return os.path.splitext(base)[0]


def load_pinned_modes(config_path: Optional[str] = None) -> Dict[str, str]:
    """`{strategy_key: configured blend_weighting_mode}` from `strategies_config.json`.

    Every blend entry that declares a mode is treated as pinned to it -- that is
    the single source of truth a clean dump is written from, and it is what makes
    `chan_four_state_blend`'s `regime_ic_ir_hybrid` (AGENTS.md section 8) visible
    to the override step instead of being averaged away by one global default.
    A missing/unreadable config yields `{}` (no pins known) rather than an error:
    the override copy is still produced, only the warning is lost.
    """
    path = config_path or DEFAULT_CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(config, dict):
        return {}
    pinned: Dict[str, str] = {}
    for key, entry in config.items():
        if not isinstance(entry, dict):
            continue
        params = entry.get("parameters")
        if not isinstance(params, dict):
            continue
        mode = params.get(WEIGHTING_MODE_KEY)
        if isinstance(mode, str) and mode:
            pinned[str(key)] = mode
    return pinned


def _parameter_containers(doc: Dict[str, Any]) -> Tuple[Dict[str, Any], ...]:
    """The two dicts a dump keeps in lockstep (`params` and the spec mirror).

    Both are always created when present-or-creatable, because a clean dump has
    both and a consumer may read either (`run_live_strategy.py:1543`,
    `check_live_portfolio_health.py:1144-1152`).
    """
    containers = [doc.setdefault("params", {})]
    spec = doc.get("research_strategy_spec")
    if isinstance(spec, dict):
        entry = spec.setdefault("entry_data", {})
        if isinstance(entry, dict):
            containers.append(entry.setdefault("parameters", {}))
    return tuple(containers)


def _apply_override(
    doc: Dict[str, Any],
    weighting_mode: Optional[str],
    enable_orthogonalization: Optional[bool],
    extra_params: Mapping[str, Any],
) -> None:
    """Write the requested keys into every parameter container of one dump copy.

    `None` means "the caller did not ask", which is NOT the same as "set it to
    the default": a `None` weighting mode must leave the dump's own configured
    value (e.g. `chan_four_state_blend`'s pinned `regime_ic_ir_hybrid`) intact.
    """
    for container in _parameter_containers(doc):
        if weighting_mode is not None:
            container[WEIGHTING_MODE_KEY] = weighting_mode
        if enable_orthogonalization is not None:
            container[ORTHOGONALIZATION_KEY] = bool(enable_orthogonalization)
        # Applied last, exactly like the old Step 2 (`params.update(extra_params)`):
        # an extra param naming a mode deliberately wins over the flag.
        container.update(extra_params)


def _reject_unsafe_dest(source: str, dest: str) -> None:
    if source == dest:
        raise ValueError(
            f"dest_dir must differ from source_dir ({source!r}): this helper exists precisely so "
            "the LIVE strategy dumps are never written by a backtest run."
        )
    try:
        # Different drives on Windows raise instead of returning a path: that is
        # also "not inside the source", so treat it as safe.
        inside_source = os.path.commonpath([source, dest]) == source
    except ValueError:
        inside_source = False
    if inside_source:
        raise ValueError(
            f"dest_dir {dest!r} is inside source_dir {source!r}; write the override copy to a "
            "run-local directory (e.g. <results>/_strategy_dumps_override), never inside the dumps."
        )


# --------------------------------------------------------------------------- #
# The one entry point the shell uses
# --------------------------------------------------------------------------- #
def materialise_dump_overrides(
    source_dir: str,
    dest_dir: str,
    *,
    weighting_mode: Optional[str] = None,
    enable_orthogonalization: Optional[bool] = None,
    extra_params: Optional[Mapping[str, Any]] = None,
    pinned_modes: Optional[Mapping[str, str]] = None,
    pattern: str = DEFAULT_BLEND_PATTERN,
) -> OverrideReport:
    """Copy `source_dir/<pattern>` into `dest_dir`, applying the requested overrides.

    `source_dir` is READ ONLY by construction: files are opened `"r"` and never
    touched, and the destination is built in a separate staging directory that is
    renamed into place only once every copy is complete.

    With `weighting_mode=None` and `enable_orthogonalization=None` the copies are
    byte-identical (`shutil.copy2`, mtime preserved) -- that is the "no injection
    at all" path, and it is what makes the helper safe to call unconditionally.

    Returns an `OverrideReport`; a strategy whose pinned mode the request departs
    from appears in BOTH `overridden` (the deviation IS applied -- an ablation may
    explore another mode) and `pinned_deviations` (so the caller can shout about
    it without aborting).
    """
    source = os.path.abspath(os.fspath(source_dir))
    dest = os.path.abspath(os.fspath(dest_dir))
    if not os.path.isdir(source):
        raise FileNotFoundError(f"strategy dumps directory not found: {source!r}")
    _reject_unsafe_dest(source, dest)

    extra = dict(extra_params or {})
    pinned = dict(pinned_modes or {})

    matched = sorted(glob.glob(os.path.join(source, pattern)))
    if not matched:
        raise ValueError(
            f"no strategy dump matched {pattern!r} in {source!r}. Refusing to build an empty "
            "override directory (the batch would then fail on an empty --strategies-glob)."
        )

    applied: Dict[str, Any] = {}
    if weighting_mode is not None:
        applied[WEIGHTING_MODE_KEY] = weighting_mode
    if enable_orthogonalization is not None:
        applied[ORTHOGONALIZATION_KEY] = bool(enable_orthogonalization)
    applied.update(extra)
    inject = bool(applied)

    parent = os.path.dirname(dest) or "."
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=os.path.basename(dest) + ".staging.", dir=parent)

    written: list = []
    overridden: list = []
    unchanged: list = []
    deviations: list = []

    completed = False
    try:
        for path in matched:
            name = os.path.basename(path)
            key = strategy_key_for(name)
            target = os.path.join(staging, name)

            if not inject:
                shutil.copy2(path, target)
                unchanged.append(key)
            else:
                with open(path, "r", encoding="utf-8") as handle:
                    doc = json.load(handle)
                _apply_override(doc, weighting_mode, enable_orthogonalization, extra)
                # Same writer shape as `common.reporting.write_json_report`, the
                # function that produced the SOURCE dump, so a copy differs from
                # its source only in the requested keys.
                with open(target, "w", encoding="utf-8") as handle:
                    json.dump(doc, handle, indent=2, default=str)
                overridden.append(key)
                if weighting_mode is not None:
                    pinned_mode = pinned.get(key)
                    if pinned_mode and pinned_mode != weighting_mode:
                        deviations.append(
                            PinnedDeviation(
                                strategy=key,
                                pinned_mode=pinned_mode,
                                requested_mode=weighting_mode,
                            )
                        )

            written.append(key)

        # Swap in the finished directory. The previous override directory (if any)
        # is only removed AFTER every copy exists, so `dest` is never half-written;
        # `source` is not touched by either step.
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.rename(staging, dest)
        completed = True
    finally:
        if not completed and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)

    return OverrideReport(
        source_dir=source,
        dest_dir=dest,
        written=tuple(written),
        overridden=tuple(overridden),
        unchanged=tuple(unchanged),
        pinned_deviations=tuple(deviations),
        applied=tuple(applied.items()),
    )


# --------------------------------------------------------------------------- #
# Reporting (the warning text lives here so it cannot be paraphrased away)
# --------------------------------------------------------------------------- #
def format_pinned_exception_warning(deviation: PinnedDeviation, dest_dir: str) -> str:
    """The prominent, unmissable warning for the AGENTS.md section-8 exception.

    Printed as ONE block per deviation. Deliberately does not abort: an ablation
    is allowed to explore another mode -- it just may never do so silently.
    """
    return (
        "!!! WARNING: PINNED WEIGHTING-MODE EXCEPTION OVERRIDDEN !!!\n"
        f"  {deviation.strategy} is PINNED to {WEIGHTING_MODE_KEY}={deviation.pinned_mode!r}\n"
        "  (pipeline/research_strategy/strategies_config.json; documented exception in\n"
        "  AGENTS.md section 8), but this run explicitly requested\n"
        f"  {deviation.requested_mode!r}.\n"
        "  The request is KEPT -- an ablation may legitimately explore another mode -- but the\n"
        "  deviation is REAL: with anything other than "
        f"{PINNED_EXCEPTION_MODE!r}, blend_base.py:445\n"
        "  takes the `rolling_ic_ir` branch and RETURNS before the regime_* branch at :465, so\n"
        "  the bucket_renorm_max_delta clamp (:522-529) and the thrust hysteresis are INERT.\n"
        f"  Scope of this override: ONLY the run-local copy {dest_dir}/{deviation.strategy}"
        f"{_STRATEGY_FILE_SUFFIX}\n"
        "  (plus this run's --strategy-params). The live dump, and therefore\n"
        "  scripts/run_live_strategy.py's DEFAULT_STRATEGY_FILE, is NOT modified.\n"
        "!!! WARNING: PINNED WEIGHTING-MODE EXCEPTION OVERRIDDEN !!!"
    )


def format_generic_deviation_warning(deviation: PinnedDeviation, dest_dir: str) -> str:
    return (
        f"!!! WARNING: weighting-mode override departs from {deviation.strategy}'s configured "
        f"value ({deviation.pinned_mode!r} -> {deviation.requested_mode!r}); "
        f"applied to the run-local copy under {dest_dir} only, the live dump is untouched."
    )


def format_report(report: OverrideReport) -> str:
    """Human-readable block: what was overridden, what was left alone, and why."""
    lines = [
        "=== Run-local strategy-dump override copies ===",
        f"Source (LIVE, read-only):  {report.source_dir}",
        f"Copy  (consumed by batch): {report.dest_dir}",
    ]
    if report.overridden:
        lines.append(f"Overridden ({len(report.overridden)}): " + ", ".join(report.overridden))
        rendered = ", ".join(f"{key}={value!r}" for key, value in report.applied)
        lines.append(f"  applied keys: {rendered}")
    if report.unchanged:
        lines.append(f"Copied unchanged ({len(report.unchanged)}): " + ", ".join(report.unchanged))
    if not report.overridden:
        lines.append("No override requested: every copy is byte-identical to its source.")
    for deviation in report.pinned_deviations:
        if deviation.is_documented_exception:
            lines.append(format_pinned_exception_warning(deviation, report.dest_dir))
        else:
            lines.append(format_generic_deviation_warning(deviation, report.dest_dir))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI (called by backtest_blend_strategies.sh via `uv run python <this file>`)
# --------------------------------------------------------------------------- #
def _parse_bool(raw: str) -> bool:
    return str(raw).strip().lower() in ("true", "1", "yes", "y", "on")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Materialise run-local overridden COPIES of strategy_dumps/*blend*.json. "
            "The live dumps directory is read only -- never written, never renamed."
        )
    )
    parser.add_argument("--source-dir", default=DEFAULT_DUMPS_DIR,
                        help=f"live dumps directory, read only (default: {DEFAULT_DUMPS_DIR})")
    parser.add_argument("--dest-dir", required=True,
                        help="run-local copy directory, e.g. <results>/_strategy_dumps_override")
    parser.add_argument("--weighting-mode", default=None,
                        help="value for blend_weighting_mode; omit to keep each dump's configured mode")
    parser.add_argument("--enable-orthogonalization", default=None,
                        help="true/false for blend_enable_orthogonalization; omit to keep the configured value")
    parser.add_argument("--extra-params", default="{}",
                        help="JSON object merged into every copy's params (default: {})")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help=f"strategies_config.json used to resolve pinned modes (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--pattern", default=DEFAULT_BLEND_PATTERN,
                        help=f"glob inside --source-dir (default: {DEFAULT_BLEND_PATTERN})")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    try:
        extra_raw = (args.extra_params or "{}").strip()
        extra_params = json.loads(extra_raw) if extra_raw else {}
    except json.JSONDecodeError as exc:
        print(f"Error: --extra-params is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(extra_params, dict):
        print(f"Error: --extra-params must be a JSON object, got {type(extra_params).__name__}",
              file=sys.stderr)
        return 2

    try:
        report = materialise_dump_overrides(
            args.source_dir,
            args.dest_dir,
            weighting_mode=args.weighting_mode or None,
            enable_orthogonalization=(
                None if args.enable_orthogonalization is None
                else _parse_bool(args.enable_orthogonalization)
            ),
            extra_params=extra_params,
            pinned_modes=load_pinned_modes(args.config),
            pattern=args.pattern,
        )
    except (OSError, ValueError) as exc:
        print(f"Error: could not build the strategy-dump override copy: {exc}", file=sys.stderr)
        return 1

    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
