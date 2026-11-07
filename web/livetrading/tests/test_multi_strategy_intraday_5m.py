"""`D11` -- REAL 5m intraday support in the NETTED runner, accepted in THREE states.

Frozen spec: `docs/development_plan.md` §4.50 (task row `D11`, `:398`). The ruled shape is

* §4.50.0 -- the switch FOLLOWS CONFIGURATION; no default-off manual CLI switch;
* §4.50.1 -- the request must be resolved through THAT template's own `resolve_param`
  (a `params.get("enable_intraday_5m", False)` here would miss
  `cfg.blend_enable_intraday_5m` while `blend_base.py:714` still resolved True, i.e. the
  2026-10-22 "printed success, fields all empty" defect on the netted path);
* §4.50.2 -- ONE round-level union load (never inside the strategy loop), strategy-level
  gate unchanged;
* §4.50.3 -- the child's own block must OUTRANK stdout/health in
  `reconcile_intraday_5m_observability`, or `_apply_intraday_observability`'s hard overwrite
  turns a real block back into `source="none"` (false green);
* §4.50.4 -- three states, each with its own offline case:
  1. zero requests  => no load, `source="none"`, artifacts bit-identical to pre-D11;
  2. partial request => exactly one load, the requester sees intraday columns, the
     NON-requester's weights are cell-by-cell unchanged;
  3. requested but unavailable => honest `requested=True / enabled=False / reason`, no fake
     intraday features, and the block survives the web layer with `source` intact.

100% offline: the synthetic provider generates the daily bars AND the 5m bars (no network,
no DuckDB cache write, no `data/intraday_5m` parquet, nothing under `data/runs/`). Every test
freezes the market clock and writes into `tmp_path`.
"""

import datetime as dt
import json
import os
import sys

import pandas as pd
import pytest

_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import common.cli_utils as cli_utils  # noqa: E402
from execution_service import (  # noqa: E402
    INTRADAY_5M_WARN_MARKER,
    ExecutionService,
    extract_child_intraday_5m_block,
    reconcile_intraday_5m_observability,
)
from scripts import run_live_multi_strategy as rms  # noqa: E402

_UTC = dt.timezone.utc
#: Beijing 15:30 on 2026-10-23 -> close + publication grace -> as_of 2026-10-23.
_BEIJING_1530 = dt.datetime(2026, 10, 23, 7, 30, tzinfo=_UTC)
_AS_OF = "2026-10-23"
_UNIVERSE = ["600000.SH", "601857.SH", "BIL"]
_DAILY_COLUMNS = {"Open", "High", "Low", "Close", "Volume"}


@pytest.fixture(autouse=True)
def _hermetic_5m_cache(monkeypatch, tmp_path):
    """Never let the 5m loader's read-only DuckDB fallback touch the repo's production cache.

    `common.intraday_features.load_intraday_5m_universe` builds its candidate list as
    `<cache_dir>/cache.duckdb` **then** `<repo>/data/cache.duckdb` and
    `pipeline/data/cache.duckdb`. `_run_netting` now passes `--cache-dir <tmp_path>/cache`
    (reviewer N3), but that alone is not hermetic: an EMPTY provider frame still falls
    through to the repo-relative candidates, so these tests would start reading real 5m
    rows the moment they exist on this machine. Point every candidate inside tmp_path.
    """
    import common.intraday_features as intraday_mod

    def _tmp_candidates(cache_dir=None):
        return [os.path.join(str(cache_dir or tmp_path), "cache.duckdb")]

    monkeypatch.setattr(intraday_mod, "_intraday_5m_cache_candidates", _tmp_candidates)


# --------------------------------------------------------------------------- #
# Harness: real `main()`, real daily + 5m synthetic data, only the strategy
# definitions are doubles (the subject is the runner's wiring, not a blend engine)
# --------------------------------------------------------------------------- #
def _freeze(monkeypatch, instant):
    """Pin the single wall-clock read every live entrypoint goes through."""
    real_market_now = cli_utils.market_now

    def _frozen(market, now=None, holidays=None, publication_grace_minutes=None):
        return real_market_now(
            market,
            now=now if now is not None else instant,
            holidays=holidays,
            publication_grace_minutes=publication_grace_minutes,
        )

    monkeypatch.setattr(cli_utils, "market_now", _frozen)


def _weights_from_last_close(universe):
    """Deterministic, sparse, and CLOSE-DEPENDENT target weights.

    Close dependence is deliberate: `enrich_universe_with_intraday_features` REPLACES the
    partial as-of daily bar from truncated 5m data, so a strategy handed the enriched copy
    computes different weights than one handed the untouched daily copy. That is what makes
    the "non-requesting strategy cell-by-cell unchanged" assertion discriminating instead of
    tautological.
    """
    closes = {}
    last_date = None
    for sym, df in universe.items():
        if df is None or getattr(df, "empty", True) or "Close" not in getattr(df, "columns", ()):
            continue
        last_date = df.index.max() if last_date is None else max(last_date, df.index.max())
        if str(sym) not in ("BIL", "CASH"):
            closes[sym] = float(df["Close"].iloc[-1])
    if not closes:
        return pd.DataFrame()
    total = float(sum(closes.values())) or 1.0
    row = {sym: value / total for sym, value in closes.items()}
    return pd.DataFrame([row], index=pd.DatetimeIndex([last_date]))


class _RecordingTemplate:
    """Template double WITHOUT `resolve_param` (what a static allocation template looks like)."""

    def __init__(self, label, *, observations):
        self.label = label
        self.observations = observations
        self.params = {}

    def generate_weights(self, universe, params):
        self.observations[self.label] = {
            "symbols": sorted(universe),
            "columns": {sym: tuple(getattr(df, "columns", ())) for sym, df in universe.items()},
            "last_close": {
                sym: float(df["Close"].iloc[-1])
                for sym, df in universe.items()
                if df is not None
                and not getattr(df, "empty", True)
                and "Close" in getattr(df, "columns", ())
            },
            "has_intraday_column": {
                sym: rms.INTRADAY_5M_INDICATOR_COLUMN in getattr(df, "columns", ())
                for sym, df in universe.items()
            },
            "weights": _weights_from_last_close(universe),
        }
        return _weights_from_last_close(universe)

    def explain_weights(self, params):  # pragma: no cover - contract completeness
        return self.label

    def warmup_bars(self, params):  # pragma: no cover - contract completeness
        return 0


class _SwitchedTemplate(_RecordingTemplate):
    """Template double WITH `resolve_param` -- the only shape that can request 5m (§4.50.1).

    `params` stays EMPTY on purpose: the switch is answered by the resolver, never by a bare
    `params.get(...)` lookup. A runner that re-implemented the resolution would read zero
    requests here and the partial-request state would go red.
    """

    def __init__(self, label, *, observations, enable_5m, cutoff=None):
        super().__init__(label, observations=observations)
        self._enable_5m = enable_5m
        self._cutoff = cutoff

    def resolve_param(self, p, name, default=None, param_type=None):
        if name == "enable_intraday_5m":
            return self._enable_5m
        if name == "intraday_cutoff_time":
            return self._cutoff if self._cutoff is not None else default
        return default


def _install_fake_strategy_resolution(monkeypatch, templates):
    """Replace ONLY the dump/config resolver; everything else in `main()` is real.

    The synthesized definition carries no `research_strategy_spec` and only static-template
    names, so `scripts/live_preflight.py` sees a genuine static template and has nothing to
    verify -- that verifier (and the dump-version parity check inside the real resolver) has
    its own suite; here the subject is the 5m wiring.
    """

    def _resolve(key, _strategy_file=None):
        template = templates[key]
        strat_def = {
            "template_name": "equal_weight",
            "strategy_name": f"Fake {key}",
            "params": dict(template.params),
        }
        return strat_def, "equal_weight", "tests:fake_strategy", template

    monkeypatch.setattr(rms, "resolve_strategy_definition", _resolve)


def _write_universe(directory, symbols):
    directory = str(directory)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "universe.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(symbols) + "\n")
    return path


def _run_netting(monkeypatch, root, strategy_keys, *, holdings=None):
    """Run the REAL `main()` end-to-end and return `<root>/out`."""
    _freeze(monkeypatch, _BEIJING_1530)
    root = str(root)
    universe_file = _write_universe(root, _UNIVERSE)
    out_dir = os.path.join(root, "out")
    cache_dir = os.path.join(root, "cache")
    argv = [
        "run_live_multi_strategy.py",
        "--strategies",
        *[f"{key}:1.0" for key in strategy_keys],
        "--universe-file",
        universe_file,
        "--output-dir",
        out_dir,
        "--start-date",
        "2026-01-01",
        "--data-provider",
        "synthetic",
        "--portfolio-value",
        "100000",
        # (reviewer N3) Without this the 5m loader falls back to the repo's production
        # `data/cache.duckdb` (read-only), which makes these tests non-deterministic as soon
        # as real 5m rows exist there. `_hermetic_5m_cache` closes the remaining candidates.
        "--cache-dir",
        cache_dir,
        "--no-cache",
    ]
    if holdings:
        argv.extend(["--current-holdings", json.dumps(holdings)])
    monkeypatch.setattr(sys, "argv", argv)
    rms.main()
    return out_dir


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


# --------------------------------------------------------------------------- #
# State 1 (no request) -- the 5m code path does not run at all
# --------------------------------------------------------------------------- #
def test_zero_requests_never_touch_the_5m_path(monkeypatch, tmp_path):
    """State 1: zero requests => no load call, no observability block, identical artifacts.

    §4.50.4 clause 1. Two shapes of "no request" are exercised together, because they take
    different branches inside the runner: a template with NO switch at all (a static
    allocation template) and a template whose own resolver answers `False`.

    re-break: change the gate to `if True:` (or `if intraday_5m_requesters is not None:`) --
    the poisoned loader raises and this test fails naming the 5m loader (verified red).
    """
    def _poisoned(*args, **kwargs):
        raise AssertionError(
            "the 5m loader must not be called when no strategy requested 5m (§4.50.4 clause 1)"
        )

    monkeypatch.setattr(rms, "load_intraday_5m_panel", _poisoned)

    obs_first, obs_second = {}, {}

    def _templates(observations):
        return {
            "trend_static": _RecordingTemplate("trend_static", observations=observations),
            "trend_off": _SwitchedTemplate(
                "trend_off", observations=observations, enable_5m=False
            ),
        }

    templates = _templates(obs_first)
    _install_fake_strategy_resolution(monkeypatch, templates)
    first_out = _run_netting(monkeypatch, tmp_path / "first", ["trend_static", "trend_off"])

    templates = _templates(obs_second)
    _install_fake_strategy_resolution(monkeypatch, templates)
    second_out = _run_netting(monkeypatch, tmp_path / "second", ["trend_static", "trend_off"])

    first_summary = _load_json(os.path.join(first_out, "summary.json"))
    # Nothing requested 5m => this round must not write a block at all, so the web layer's
    # reconcile falls through to `source="none"` instead of trusting a fabricated verdict.
    assert "observability" not in first_summary

    # The strategies saw EXACTLY the pre-D11 daily universe: no 5m column, no synthesized
    # `Is_Partial_Bar` marker column leaking in from the enrichment.
    for observations in (obs_first, obs_second):
        for label in ("trend_static", "trend_off"):
            recorded = observations[label]
            assert not any(recorded["has_intraday_column"].values()), label
            for sym, columns in recorded["columns"].items():
                assert "Is_Partial_Bar" not in columns, (label, sym)
                assert set(columns) <= _DAILY_COLUMNS, (label, sym, columns)

    # Bit-identical artifacts: two zero-request rounds differ only by the output directory.
    for name in ("netted_trading_ticket.json", "netted_trading_ticket.csv"):
        assert _read_bytes(os.path.join(first_out, name)) == _read_bytes(
            os.path.join(second_out, name)
        ), name

    # ... and the two zero-request shapes agree on the actual signal they produced.
    pd.testing.assert_frame_equal(
        obs_first["trend_static"]["weights"], obs_second["trend_static"]["weights"], check_exact=True
    )
    pd.testing.assert_frame_equal(
        obs_first["trend_off"]["weights"], obs_second["trend_off"]["weights"], check_exact=True
    )


# --------------------------------------------------------------------------- #
# State 2 (partial request) -- one union load; only the requester sees 5m columns
# --------------------------------------------------------------------------- #
def test_partial_request_loads_once_and_only_the_requester_sees_intraday(monkeypatch, tmp_path):
    """State 2: exactly ONE round-level load; the non-requester's weights are unchanged.

    §4.50.2. The loader is wrapped (not replaced) so the real synthetic 5m pipeline runs --
    the intraday columns below are genuinely produced, not stubbed.

    re-break: (a) move the load inside the strategy loop -> `len(calls) == 1` fails;
    (b) hand the enriched universe to every strategy (drop the per-strategy gate) -> the
    non-requester's weights move (the synthesized partial bar changes its last close) and the
    exact frame comparison fails; (c) resolve the switch via `params.get` instead of the
    template's `resolve_param` -> the requester's params are empty, nothing loads, and the
    intraday-column assertion fails.
    """
    calls = []
    real_loader = rms.load_intraday_5m_panel

    def _counting_loader(symbols, **kwargs):
        calls.append({"symbols": list(symbols), **kwargs})
        return real_loader(symbols, **kwargs)

    monkeypatch.setattr(rms, "load_intraday_5m_panel", _counting_loader)

    # --- partial-request round: out-of-universe holding forces the union to cover it -------
    obs_partial = {}
    templates = {
        "blend_on": _SwitchedTemplate(
            "blend_on", observations=obs_partial, enable_5m=True, cutoff="14:00"
        ),
        "trend_static": _RecordingTemplate("trend_static", observations=obs_partial),
    }
    _install_fake_strategy_resolution(monkeypatch, templates)
    partial_out = _run_netting(
        monkeypatch,
        tmp_path / "partial",
        ["blend_on", "trend_static"],
        holdings={"000001.SZ": 300},
    )

    assert len(calls) == 1, "the 5m panel must be loaded exactly ONCE per round"
    # Round-level UNION, holdings included -- not just the universe file's symbols.
    assert set(calls[0]["symbols"]) == {"600000.SH", "601857.SH", "000001.SZ", "BIL"}, (
        "the union load must cover out-of-universe holdings too (§4.50.2)"
    )
    # The requesting strategy's own cutoff is what the shared enrichment ran at.
    assert calls[0]["cutoff_time"] == "14:00"

    requester = obs_partial["blend_on"]
    non_requester = obs_partial["trend_static"]
    assert any(requester["has_intraday_column"].values()), (
        "the requesting strategy must actually receive the intraday columns"
    )
    assert not any(non_requester["has_intraday_column"].values()), (
        "the non-requesting strategy must not inherit 5m columns from the union load"
    )
    for sym, columns in non_requester["columns"].items():
        assert "Is_Partial_Bar" not in columns, sym

    block = _load_json(os.path.join(partial_out, "summary.json"))["observability"]["intraday_5m"]
    assert block["requested"] is True
    assert block["enabled"] is True
    assert block["rows_5m"] > 0
    assert block["source"] == rms.INTRADAY_5M_SOURCE
    assert block["as_of_date"] == _AS_OF
    assert block["reason"] is None
    assert "warning" not in block

    # --- baseline round: same two strategies, requester's switch answered False ----------
    obs_baseline = {}
    templates = {
        "blend_on": _SwitchedTemplate("blend_on", observations=obs_baseline, enable_5m=False),
        "trend_static": _RecordingTemplate("trend_static", observations=obs_baseline),
    }
    _install_fake_strategy_resolution(monkeypatch, templates)
    _run_netting(
        monkeypatch,
        tmp_path / "baseline",
        ["blend_on", "trend_static"],
        holdings={"000001.SZ": 300},
    )
    assert len(calls) == 1, "the zero-request round must not add a 5m load"

    # Cell-by-cell identical for the strategy that asked for nothing (§4.50.4 clause 2).
    pd.testing.assert_frame_equal(
        obs_baseline["trend_static"]["weights"],
        obs_partial["trend_static"]["weights"],
        check_exact=True,
    )
    assert (
        obs_baseline["trend_static"]["last_close"] == obs_partial["trend_static"]["last_close"]
    )


# --------------------------------------------------------------------------- #
# State 3 (requested, unavailable) -- honest UNAVAILABLE end to end
# --------------------------------------------------------------------------- #
def test_requested_but_unavailable_stays_honest_through_the_web_layer(monkeypatch, tmp_path, capsys):
    """State 3: `requested=True / enabled=False / reason`, `source` survives, no fake features.

    §4.50.4 clause 3 + §4.50.3. The child's stdout DOES contain the warning sentinel here
    (that is the ported Stage-2 WARN), and the point of the reconcile assertions is that the
    child's own block still wins over that older, less specific source.

    re-break: (a) `enabled = bool(requested)` in `build_intraday_5m_block` (drop the row
    check) -> `enabled is False` fails; (b) drop `persist_intraday_5m_observability` ->
    `summary.json` has no block and the reconciled source degrades to `"stdout"`, failing the
    `source == "multi_strategy_netted"` assertion (verified red).
    """
    recorded = {}

    def _empty_panel(symbols, **kwargs):
        recorded["symbols"] = list(symbols)
        recorded["kwargs"] = kwargs
        frames = {}
        for sym in symbols:
            frame = pd.DataFrame(
                {"Open": [], "High": [], "Low": [], "Close": [], "Volume": []},
                index=pd.DatetimeIndex([]),
            )
            frame.attrs["intraday_5m_available"] = False
            frames[sym] = frame
        return frames

    monkeypatch.setattr(rms, "load_intraday_5m_panel", _empty_panel)

    obs = {}
    templates = {"blend_on": _SwitchedTemplate("blend_on", observations=obs, enable_5m=True)}
    _install_fake_strategy_resolution(monkeypatch, templates)
    out_dir = _run_netting(monkeypatch, tmp_path / "unavailable", ["blend_on"])

    stdout = capsys.readouterr().out
    # The ported Stage-2 WARN: sentinel + the honest `enabled=False` verdict, never a
    # "success" line counting daily symbols.
    assert rms.INTRADAY_5M_WARN_MARKER in stdout
    assert "enabled=False" in stdout
    assert "5m 日内特征已聚合" not in stdout

    # The requesting strategy got NO intraday feature column -- so nothing downstream can
    # compute a feature (and no `intraday_vwap_breadth` can be faked) from this round.
    assert not any(obs["blend_on"]["has_intraday_column"].values())

    summary = _load_json(os.path.join(out_dir, "summary.json"))
    block = summary["observability"]["intraday_5m"]
    assert block["requested"] is True
    assert block["enabled"] is False
    assert block["rows_5m"] == 0
    assert block["reason"], "an UNAVAILABLE verdict must carry a reason"
    assert block["source"] == rms.INTRADAY_5M_SOURCE
    assert block["as_of_date"] == _AS_OF
    assert set(recorded["symbols"]) == set(_UNIVERSE)

    # --- §4.50.3: the child's block outranks the stdout marker -------------------------
    reconciled = reconcile_intraday_5m_observability(
        None, stdout=stdout, as_of_date=_AS_OF, child_report=block
    )
    assert reconciled["source"] == rms.INTRADAY_5M_SOURCE
    assert reconciled["enabled"] is False
    assert reconciled["requested"] is True

    # ... and the web layer's hard overwrite now persists THAT block, not `source="none"`,
    # with the UNAVAILABLE verdict visible in the freshness audit (design §12 case 11).
    ExecutionService(None)._apply_intraday_observability(summary, reconciled)
    assert summary["observability"]["intraday_5m"]["source"] == rms.INTRADAY_5M_SOURCE
    assert summary["observability"]["intraday_5m"]["enabled"] is False
    assert summary["freshness_summary"]["intraday_5m"]["status"] == "UNAVAILABLE"
    assert summary["freshness_summary"]["intraday_5m"]["enabled"] is False


# --------------------------------------------------------------------------- #
# Contract pins for the constants the two layers must agree on
# --------------------------------------------------------------------------- #
def test_the_child_marker_and_source_match_the_web_layer():
    """The stdout sentinel and the source tag are a cross-layer contract, pinned here.

    `web/livetrading/execution_service.py::INTRADAY_5M_WARN_MARKER` is what the reconciler
    greps for; the runner must print exactly it, and its `source` must be distinguishable
    from the generic `"none"`/"stdout" verdicts.
    """
    assert rms.INTRADAY_5M_WARN_MARKER == INTRADAY_5M_WARN_MARKER
    assert rms.INTRADAY_5M_SOURCE == "multi_strategy_netted"
    assert rms.INTRADAY_5M_SOURCE not in ("none", "stdout")


# --------------------------------------------------------------------------- #
# B1 -- the SAME-DAY RERUN regression (the netted run dir is fixed per matrix+date)
# --------------------------------------------------------------------------- #
def test_same_date_rerun_does_not_inherit_the_previous_rounds_block(monkeypatch, tmp_path):
    """Round 2 in the SAME output dir, zero requests => NO block, `source="none"`.

    `<archive>/<date>/multi_netted_<matrix>` is fixed per matrix+date and never cleaned, and
    `persist_run_metadata` MERGES into the existing `summary.json`. Before the fix a round-1
    block (`requested=True / enabled=True`) survived into round 2 verbatim -- and its
    `as_of_date` matched round 2's, so the web layer had no tie to reject it and republished
    a 5m verdict for a round that never touched 5m.

    re-break: delete the `clear_intraday_5m_observability(args.output_dir)` call at round
    start -> round 2's summary still carries round 1's `requested=True / enabled=True`
    block and every assertion below fails (verified red).
    """
    # --- round 1: requests 5m and really gets rows ---------------------------------------
    obs_one = {}
    templates = {"blend_on": _SwitchedTemplate("blend_on", observations=obs_one, enable_5m=True)}
    _install_fake_strategy_resolution(monkeypatch, templates)
    out_dir = _run_netting(monkeypatch, tmp_path / "same_day", ["blend_on"])

    summary_one = _load_json(os.path.join(out_dir, "summary.json"))
    round_one_block = summary_one["observability"]["intraday_5m"]
    assert round_one_block["requested"] is True
    assert round_one_block["enabled"] is True
    assert round_one_block["source"] == rms.INTRADAY_5M_SOURCE
    assert round_one_block["as_of_date"] == _AS_OF
    # (B1) Round identity is stamped so the layer above can reject it later.
    assert round_one_block["generated_at"]
    assert round_one_block["round_id"]

    # --- round 2: SAME output dir, SAME as_of, ZERO requests ------------------------------
    obs_two = {}
    templates = {
        "blend_on": _SwitchedTemplate("blend_on", observations=obs_two, enable_5m=False),
    }
    _install_fake_strategy_resolution(monkeypatch, templates)
    rerun_out = _run_netting(monkeypatch, tmp_path / "same_day", ["blend_on"])
    assert rerun_out == out_dir, "both rounds must write the SAME matrix run directory"

    summary_two = _load_json(os.path.join(out_dir, "summary.json"))
    # Not `requested=False` only -- the zero-request contract is that NO block is written.
    assert "intraday_5m" not in summary_two.get("observability", {}), (
        "round 1's block was resurrected into round 2 (§4.50.4 clause 1)"
    )

    reconciled = reconcile_intraday_5m_observability(
        None,
        stdout="",
        as_of_date=_AS_OF,
        child_report=extract_child_intraday_5m_block(summary_two),
        # The web layer's own round start; round 1's block necessarily predates it.
        round_started_at=dt.datetime.now(_UTC),
    )
    assert reconciled["requested"] is False
    assert reconciled["enabled"] is False
    assert reconciled["source"] == "none"

    # ... and the round-1 block itself is now STALE by the monotonic freshness rule, i.e.
    # even if a future merge resurrected it, the web layer would refuse to believe it.
    stale = reconcile_intraday_5m_observability(
        None,
        stdout="",
        as_of_date=_AS_OF,
        child_report=round_one_block,
        round_started_at=dt.datetime.now(_UTC),
    )
    assert stale["source"] == "none"
    assert stale["enabled"] is False


def test_crashed_round_cannot_be_read_as_this_rounds_report(monkeypatch, tmp_path):
    """A round that dies before writing its block leaves NO stale report behind.

    Cheap variant of "the child crashed": run the real round-1 cleanup-free path to create a
    stale block, then invoke ONLY the round-start cleanup (which is where a new round removes
    the inherited block) and stop. The previous round's block must no longer be readable as
    a report for the current round.
    """
    obs = {}
    templates = {"blend_on": _SwitchedTemplate("blend_on", observations=obs, enable_5m=True)}
    _install_fake_strategy_resolution(monkeypatch, templates)
    out_dir = _run_netting(monkeypatch, tmp_path / "crash", ["blend_on"])

    stale_block = _load_json(os.path.join(out_dir, "summary.json"))["observability"][
        "intraday_5m"
    ]
    assert stale_block["enabled"] is True

    # --- this is exactly the round-start step, then the round "dies" ---------------------
    assert rms.clear_intraday_5m_observability(out_dir) is True
    summary = _load_json(os.path.join(out_dir, "summary.json"))
    assert "intraday_5m" not in summary.get("observability", {})
    # Nothing else was destroyed: the §9 clock audit trail survives the cleanup.
    assert summary.get("clock")
    assert summary.get("freshness_summary")

    reconciled = reconcile_intraday_5m_observability(
        None,
        stdout="",
        as_of_date=_AS_OF,
        child_report=extract_child_intraday_5m_block(summary),
        round_started_at=dt.datetime.now(_UTC),
    )
    assert reconciled["requested"] is False
    assert reconciled["source"] == "none"

    # Idempotent: a second cleanup finds nothing and leaves the file untouched.
    assert rms.clear_intraday_5m_observability(out_dir) is False


# --------------------------------------------------------------------------- #
# N2 -- the two honest branches that had no coverage
# --------------------------------------------------------------------------- #
def test_partial_missing_symbols_are_named_in_a_warn(monkeypatch, tmp_path, capsys):
    """Some 5m symbols arrive, some do not => ONE loud WARN naming the missing ones.

    §4.50.7 requires the honest Stage-2 warning to be ported WITH the loading block: a
    silent partial load is how "fields all empty" looked like success on 2026-10-22.
    """
    real_loader = rms.load_intraday_5m_panel

    def _drop_one(symbols, **kwargs):
        frames = real_loader(symbols, **kwargs)
        frames["600000.SH"] = pd.DataFrame(
            {"Open": [], "High": [], "Low": [], "Close": [], "Volume": []},
            index=pd.DatetimeIndex([]),
        )
        return frames

    monkeypatch.setattr(rms, "load_intraday_5m_panel", _drop_one)

    obs = {}
    templates = {"blend_on": _SwitchedTemplate("blend_on", observations=obs, enable_5m=True)}
    _install_fake_strategy_resolution(monkeypatch, templates)
    out_dir = _run_netting(monkeypatch, tmp_path / "partial_missing", ["blend_on"])

    stdout = capsys.readouterr().out
    assert "缺少 5m 数据" in stdout
    assert "600000.SH" in stdout
    # A partial load is still a REAL load for the symbols that arrived.
    assert "5m 日内特征已聚合" in stdout

    block = _load_json(os.path.join(out_dir, "summary.json"))["observability"]["intraday_5m"]
    assert block["requested"] is True
    assert block["enabled"] is True
    assert "600000.SH" in block["symbols_unavailable"]


def test_enrichment_exception_still_yields_an_honest_unavailable_ticket(
    monkeypatch, tmp_path, capsys
):
    """The load/enrich raise path => `requested=True / enabled=False / reason`, ticket survives.

    What must NOT happen: the round dies, or it silently reverts to a clean daily run with no
    trace that 5m was requested (§4.50.4 state 3).
    """
    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic 5m explosion")

    monkeypatch.setattr(rms, "apply_intraday_5m_features", _boom)

    obs = {}
    templates = {"blend_on": _SwitchedTemplate("blend_on", observations=obs, enable_5m=True)}
    _install_fake_strategy_resolution(monkeypatch, templates)
    out_dir = _run_netting(monkeypatch, tmp_path / "enrich_error", ["blend_on"])

    stdout = capsys.readouterr().out
    assert rms.INTRADAY_5M_WARN_MARKER in stdout
    assert "enabled=False" in stdout
    assert "synthetic 5m explosion" in stdout

    summary = _load_json(os.path.join(out_dir, "summary.json"))
    block = summary["observability"]["intraday_5m"]
    assert block["requested"] is True
    assert block["enabled"] is False
    assert block["reason"] == "5m 加载/聚合异常: synthetic 5m explosion"
    assert block["source"] == rms.INTRADAY_5M_SOURCE
    # The round still produced a ticket -- degraded signal, not a dead run.
    assert os.path.exists(os.path.join(out_dir, "netted_trading_ticket.json"))

