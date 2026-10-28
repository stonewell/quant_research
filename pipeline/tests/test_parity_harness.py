"""P1.1 -- parity harness: archived live run vs. backtest-engine replay.

`docs/development_plan.md` **P1.1** requires a two-track replay test bed: take a
run archive (`holdings.json`, `stage1_health_report.json`,
`live_trading_ticket.csv`, `summary.json`), push the SAME books and the SAME
marks through `common/allocation_backtester.run_allocation_backtest`, and assert
binary-level alignment (`|dw| < 1e-4`, `dShares == 0`).

WHAT THESE TESTS PIN
====================
1. **A real archive replays bit-identically** (`aligned_china_2026_10_22`), and
   that fixture really does exercise the row families a live ticket contains:
   a partial SELL, a full liquidation, a BUY whose raw share count is truncated
   to a whole lot, a HOLD, and a symbol held with NO ticket row at all.
2. **The harness has discriminating power** -- a one-share edit to a ticket row
   is caught, and so is an edit to the instruction weight alone (which proves the
   engine re-derives shares instead of echoing the archive).
3. **Stage-2 gate rows are never called "aligned"**, even when the book happens
   to match (`CANCEL_PENDING`, `TENURE_LOCKED`), and a gate the engine cannot
   model (`COOLDOWN_BLOCKED`, `MIN_ENTRY_THRESHOLD`) is reported as an
   unexplained divergence with its label and magnitude.
4. **A pre-`L2.24` archive (no `blocking_reason` column)** is surfaced as
   unverifiable rather than silently read as `NONE`.
5. **The one systematic live-vs-engine divergence this harness found is pinned,
   not papered over**: the live runner rounds a SELL's lot count UP
   (`np.ceil`), the engine floors it (`// lot`), so a non-lot-aligned partial
   sell differs by exactly one lot (`live_sell_ceil_2026_10_22`).
6. **Missing prices are never invented** (a held symbol with no ticket row and
   no market data is reported, not silently dropped).
7. **Invalid / ambiguous input exits 2** and never guesses.
8. **No production path is hardcoded** -- asserted structurally (AST + source
   scan) on the harness module AND on this test file.
9. **The acceptance line cannot be relaxed from the CLI** (`MAX_ABS_DELTA_W` and
   `REQUIRED_DELTA_SHARES` are module constants, no threshold flags exist).
10. **The fixtures themselves obey the live sizing rule** (independently
    recomputed here), so a hand-edited fixture cannot quietly become
    self-fulfilling.

100% offline: the fixtures are synthetic archives under
`pipeline/tests/fixtures/parity/`, the market data comes from the archive's own
price column (plus one small `market_data.json`), and every mutable case is
built in `tmp_path`. No network, no DuckDB, no real archive, no production path.
"""

from __future__ import annotations

import ast
import copy
import csv
import json
import math
import pathlib
import re
import shutil
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline import parity_harness as ph  # noqa: E402


FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "parity"
ALIGNED = "aligned_china_2026_10_22"
GATES = "gate_and_unexplained_2026_10_23"
LEGACY = "legacy_no_blocking_column_2026_10_20"
SELL_CEIL = "live_sell_ceil_2026_10_22"

LOT = 100
PV = 1_000_000.0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _copy(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    dst = tmp_path / name
    shutil.copytree(FIXTURES / name, dst)
    return dst


def _read_csv(path: pathlib.Path):
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def _write_csv(path: pathlib.Path, rows, fieldnames) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _patch_ticket(run_dir: pathlib.Path, symbol: str, **changes) -> None:
    path = run_dir / "live_trading_ticket.csv"
    rows, fieldnames = _read_csv(path)
    hit = 0
    for row in rows:
        if row["symbol"].strip().upper() == symbol:
            row.update({k: str(v) for k, v in changes.items()})
            hit += 1
    assert hit == 1, f"fixture integrity: {symbol} must appear exactly once"
    _write_csv(path, rows, fieldnames)


def _patch_json(path: pathlib.Path, **changes) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _row(report, symbol: str):
    matches = [r for r in report.rows if r.symbol == symbol]
    assert len(matches) == 1, f"{symbol} appears {len(matches)} times in the report"
    return matches[0]


# --------------------------------------------------------------------------- #
# 1. the aligned archive: bit-identical replay
# --------------------------------------------------------------------------- #
def test_aligned_archive_is_share_level_identical():
    report = ph.run_parity(FIXTURES / ALIGNED, env={})

    assert report.verdict == "ALIGNED"
    assert report.ok is True
    assert report.unexplained_rows == []
    assert report.max_abs_delta_shares == 0
    # Acceptance line, then the stronger "actually exact" claim.
    assert report.max_abs_delta_w < ph.MAX_ABS_DELTA_W
    assert report.max_abs_delta_w < 1e-12
    assert all(r.status == ph.STATUS_ALIGNED for r in report.rows)
    assert {r.symbol for r in report.rows} == {
        "600519.SH", "000001.SZ", "600036.SH", "601398.SH", "000002.SZ", "600000.SH",
    }

    replay = report.meta["replay"]
    assert replay["market"] == "china"
    assert replay["lot_size"] == LOT
    assert replay["cost_model"] == "none"
    assert replay["equity_at_as_of"] == pytest.approx(PV, abs=1e-6)
    assert report.meta["archive"]["has_blocking_reason_column"] is True


def test_aligned_fixture_covers_the_required_row_families():
    """The fixture must exercise more than "a buy that happens to line up"."""
    report = ph.run_parity(FIXTURES / ALIGNED, env={})

    # partial SELL
    sell = _row(report, "600519.SH")
    assert sell.action == "SELL"
    assert sell.archive_shares == 2500 and sell.replay_shares == 2500

    # full liquidation (explicit 0.0 target, odd-lot free by contract)
    flat = _row(report, "000002.SZ")
    assert flat.action == "SELL" and flat.archive_shares == 0 and flat.replay_shares == 0

    # BUY truncated to whole lots, with the idled remainder disclosed
    trunc = _row(report, "000001.SZ")
    assert trunc.action == "BUY"
    assert trunc.archive_shares % LOT == 0
    assert trunc.replay_shares == trunc.archive_shares
    assert trunc.truncation_gap is not None and trunc.truncation_gap < -1e-5
    assert trunc.archive_target_weight == pytest.approx(0.20137, abs=1e-12)

    # HOLD: instruction equals the current book, so nothing trades
    hold = _row(report, "601398.SH")
    assert hold.action == "HOLD" and hold.delta_shares == 0

    # held with NO ticket row: still seeded and compared
    absent = _row(report, "600000.SH")
    assert absent.action == "ABSENT"
    assert absent.archive_shares == 1500 and absent.replay_shares == 1500
    assert absent.blocking_reason == "NONE"

    # every row of this archive carries the L2.24 column
    assert report.meta["archive"]["has_blocking_reason_column"] is True


# --------------------------------------------------------------------------- #
# 2. discriminating power
# --------------------------------------------------------------------------- #
def test_one_share_perturbation_is_detected(tmp_path):
    """Change ONE share in one ticket row -> must be caught, and only that row."""
    run_dir = _copy(tmp_path, ALIGNED)
    _patch_ticket(run_dir, "600036.SH", target_shares=1501)

    report = ph.run_parity(run_dir, env={})

    assert report.verdict == "DIVERGED"
    assert report.ok is False
    assert len(report.unexplained_rows) == 1
    bad = report.unexplained_rows[0]
    assert bad.symbol == "600036.SH"
    assert bad.delta_shares == 1500 - 1501  # replay - archive == -1
    assert bad.archive_shares == 1501 and bad.replay_shares == 1500
    assert bad.severity == "divergent"
    assert bad.tag == ph.TAG_SILENT_DIVERGENCE
    # the acceptance metrics are computed over the rows that DID align
    assert report.max_abs_delta_shares == 0
    # one share on a 1M book is 5e-6 -- below the 1e-4 weight band by design,
    # which is exactly why the share count, not the weight, is the binary check.
    assert abs(bad.delta_w) < ph.MAX_ABS_DELTA_W
    assert ph.main(["--archive-dir", str(run_dir), "--quiet"]) == 1


def test_instruction_weight_perturbation_is_detected(tmp_path):
    """Editing the WEIGHT alone must move the engine's shares -- no echo-back."""
    run_dir = _copy(tmp_path, ALIGNED)
    _patch_ticket(run_dir, "000001.SZ", target_weight=0.30, delta_weight=0.30)

    report = ph.run_parity(run_dir, env={})

    assert report.verdict == "DIVERGED"
    bad = _row(report, "000001.SZ")
    assert bad.status == ph.STATUS_DIVERGENT
    assert bad.replay_shares == 30000  # 0.30 * 1M / 10, a whole 300 lots
    assert bad.delta_shares == 30000 - 20100
    assert all(r.status == ph.STATUS_ALIGNED for r in report.rows if r.symbol != "000001.SZ")


# --------------------------------------------------------------------------- #
# 3. gate rows / unexplained rows
# --------------------------------------------------------------------------- #
def test_gate_rows_are_listed_separately_and_never_aligned():
    report = ph.run_parity(FIXTURES / GATES, env={})

    assert report.verdict == "DIVERGED"
    assert ph.main(["--archive-dir", str(FIXTURES / GATES), "--quiet"]) == 1

    # CANCEL_PENDING: a never-held record. Book matches, but it is NOT aligned.
    cancel = _row(report, "600036.SH")
    assert cancel.action == "CANCEL_PENDING"
    assert cancel.tag == ph.TAG_CANCEL_PENDING
    assert cancel.status == ph.STATUS_NON_REPLAYABLE
    assert cancel not in report.unexplained_rows
    assert cancel in report.non_replayable_rows

    # TENURE_LOCKED: archive suppressed a no-op; book matches, still not aligned.
    tenure = _row(report, "600519.SH")
    assert tenure.tag == ph.TAG_GATE_NOT_MODELLED
    assert tenure.status == ph.STATUS_NON_REPLAYABLE
    assert tenure.blocking_reason == "TENURE_LOCKED"

    # COOLDOWN_BLOCKED: this replay pins the lifecycle layer OFF (the archive is
    # already the live post-lifecycle book), so it bought anyway.
    cool = _row(report, "000001.SZ")
    assert cool.status == ph.STATUS_DIVERGENT
    assert cool.tag == ph.TAG_GATE_NOT_MODELLED
    assert cool.delta_shares == 15000
    assert cool.archive_shares == 0 and cool.replay_shares == 15000

    # MIN_ENTRY_THRESHOLD: the engine sells down to the (tiny) instruction.
    tiny = _row(report, "601857.SH")
    assert tiny.status == ph.STATUS_DIVERGENT
    assert tiny.tag == ph.TAG_GATE_NOT_MODELLED
    assert tiny.archive_shares == 2000 and tiny.replay_shares == 700
    assert tiny.delta_shares == -1300

    assert {r.symbol for r in report.unexplained_rows} == {"000001.SZ", "601857.SH"}
    assert {r.symbol for r in report.non_replayable_rows} == {"600036.SH", "600519.SH"}
    # No gated row may be filed as aligned.
    assert all(r.blocking_reason in ("NONE",) for r in report.rows if r.status == ph.STATUS_ALIGNED)
    # Every separated row must explain itself in text.
    for row in report.non_replayable_rows + report.unexplained_rows:
        assert row.explanation and len(row.explanation) > 20


def test_legacy_archive_without_blocking_reason_column():
    snapshot = ph.load_archive(FIXTURES / LEGACY, env={})
    assert snapshot.has_blocking_reason_column is False

    report = ph.run_parity(FIXTURES / LEGACY, env={})
    assert report.verdict == "DIVERGED"

    # Book matches, but the non-trade is unverifiable -> non_replayable, not aligned.
    sell = _row(report, "600519.SH")
    assert sell.status == ph.STATUS_NON_REPLAYABLE
    assert sell.tag == ph.TAG_LEGACY_MISSING_BLOCKING_REASON
    assert sell.delta_shares == 0

    # Book does NOT match -> unexplained, still attributed to the missing column.
    phantom = _row(report, "000001.SZ")
    assert phantom.status == ph.STATUS_DIVERGENT
    assert phantom.tag == ph.TAG_LEGACY_MISSING_BLOCKING_REASON
    assert phantom.delta_shares == 10000
    assert all(r.status != ph.STATUS_ALIGNED for r in report.rows)


# --------------------------------------------------------------------------- #
# 4. the systematic live-vs-engine divergence this harness found
# --------------------------------------------------------------------------- #
def test_sell_lot_rounding_rule_divergence_is_exactly_one_lot():
    """Live SELL sizing uses `np.ceil(raw/lot)`, the engine uses `// lot`."""
    report = ph.run_parity(FIXTURES / SELL_CEIL, env={})

    assert report.verdict == "DIVERGED"
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.action == "SELL"
    assert row.tag == ph.TAG_SELL_ROUNDING_RULE
    assert row.archive_shares == 1500 and row.replay_shares == 1600
    assert row.delta_shares == LOT  # exactly one lot, never more
    assert abs(row.delta_w - 0.01) < 1e-12  # one lot * 100 / 1M
    # The raw sell (450 shares) is NOT lot-aligned, which is what exposes the rule.
    assert abs(row.archive_target_weight - 0.155) < 1e-12
    assert ph.main(["--archive-dir", str(FIXTURES / SELL_CEIL), "--quiet"]) == 1


# --------------------------------------------------------------------------- #
# 5. no invented prices
# --------------------------------------------------------------------------- #
def test_missing_market_price_is_reported_never_invented(tmp_path):
    run_dir = _copy(tmp_path, ALIGNED)
    (run_dir / ph.MARKET_DATA_FILE).unlink()  # 600000.SH has no ticket row either

    report = ph.run_parity(run_dir, env={})

    assert report.verdict == "DIVERGED"
    # A real position that cannot be priced is a divergence, not a silent drop.
    assert report.meta["replay"]["excluded_symbols"] == {"600000.SH": ph.TAG_MISSING_MARKET_PRICE}
    row = _row(report, "600000.SH")
    assert row.status == ph.STATUS_DIVERGENT
    assert row.tag == ph.TAG_MISSING_MARKET_PRICE
    assert row.replay_shares is None and row.price is None
    assert row.archive_shares == 1500
    # every other row still aligns exactly
    assert all(r.status == ph.STATUS_ALIGNED for r in report.rows if r.symbol != "600000.SH")


def test_cash_proxy_without_a_price_is_advisory_not_divergent(tmp_path):
    """A cash substitute is modelled as cash by the engine; it must not cry wolf."""
    run_dir = _copy(tmp_path, ALIGNED)
    payload = json.loads((run_dir / "holdings.json").read_text(encoding="utf-8"))
    payload["BIL"] = 250  # held, no ticket row, no price anywhere
    (run_dir / "holdings.json").write_text(json.dumps(payload), encoding="utf-8")

    report = ph.run_parity(run_dir, env={})

    bil = _row(report, "BIL")
    assert bil.status == ph.STATUS_NON_REPLAYABLE
    assert bil.severity == "advisory"
    assert bil.tag == ph.TAG_MISSING_MARKET_PRICE
    assert report.verdict == "ALIGNED"


# --------------------------------------------------------------------------- #
# 6. invalid input -> exit 2, never a guess
# --------------------------------------------------------------------------- #
def _case_empty_dir(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    return empty, {}


def _case_no_portfolio_value(tmp_path):
    run_dir = _copy(tmp_path, ALIGNED)
    payload = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    payload.pop("portfolio_value", None)
    (run_dir / "summary.json").write_text(json.dumps(payload), encoding="utf-8")
    return run_dir, {}


def _case_no_symbol_column(tmp_path):
    run_dir = _copy(tmp_path, ALIGNED)
    (run_dir / "live_trading_ticket.csv").write_text(
        "action,price,target_weight\nBUY,10.0,0.1\n", encoding="utf-8"
    )
    return run_dir, {}


def _case_broken_summary(tmp_path):
    run_dir = _copy(tmp_path, ALIGNED)
    (run_dir / "summary.json").write_text("{not json", encoding="utf-8")
    return run_dir, {}


def _case_missing_ticket(tmp_path):
    run_dir = _copy(tmp_path, ALIGNED)
    (run_dir / "live_trading_ticket.csv").unlink()
    return run_dir, {}


def _case_missing_market_data_file(tmp_path):
    return FIXTURES / ALIGNED, {"market_data": str(tmp_path / "nope.json")}


@pytest.mark.parametrize(
    "builder",
    [
        _case_empty_dir,
        _case_no_portfolio_value,
        _case_no_symbol_column,
        _case_broken_summary,
        _case_missing_ticket,
        _case_missing_market_data_file,
    ],
    ids=lambda fn: fn.__name__.replace("_case_", ""),
)
def test_invalid_input_raises_and_exits_2(tmp_path, capsys, builder):
    archive_dir, kwargs = builder(tmp_path)

    with pytest.raises(ph.ParityConfigError):
        ph.run_parity(archive_dir, env={}, **kwargs)

    code = ph.main(["--archive-dir", str(archive_dir), "--quiet",
                    *(["--market-data", kwargs["market_data"]] if "market_data" in kwargs else [])])
    assert code == 2
    assert "INVALID INPUT" in capsys.readouterr().err


def test_nonexistent_archive_dir_exits_2(tmp_path, capsys):
    missing = tmp_path / "does_not_exist"
    with pytest.raises(ph.ParityConfigError):
        ph.run_parity(missing, env={})
    assert ph.main(["--archive-dir", str(missing), "--quiet"]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_unknown_market_and_bad_lot_exit_2(tmp_path):
    with pytest.raises(SystemExit) as exc:
        ph.main(["--archive-dir", str(FIXTURES / ALIGNED), "--market", "mars"])
    assert exc.value.code == 2  # argparse rejects it

    with pytest.raises(ph.ParityConfigError):
        ph.run_parity(FIXTURES / ALIGNED, lot=-1, env={})


# --------------------------------------------------------------------------- #
# 7. archive-root resolution
# --------------------------------------------------------------------------- #
def test_ambiguous_archive_root_is_rejected(tmp_path):
    root = tmp_path / "archive"
    for day in ("2026-10-22", "2026-10-21"):
        shutil.copytree(FIXTURES / ALIGNED, root / day)

    with pytest.raises(ph.ParityConfigError) as exc:
        ph.resolve_archive_dir(root, env={})
    message = str(exc.value)
    assert "ambiguous" in message and "2 run directories" in message


def test_single_run_parent_root_is_resolved(tmp_path):
    root = tmp_path / "archive"
    run_dir = root / "2026-10-22"
    shutil.copytree(FIXTURES / ALIGNED, run_dir)

    assert ph.resolve_archive_dir(root, env={}) == run_dir
    assert ph.resolve_archive_dir(run_dir, env={}) == run_dir
    assert ph.run_parity(root, env={}).verdict == "ALIGNED"


def test_archive_root_comes_from_cli_or_env_never_a_hardcoded_path(tmp_path, monkeypatch):
    with pytest.raises(ph.ParityConfigError) as exc:
        ph.resolve_archive_dir(None, env={})
    assert ph.ARCHIVE_DIR_ENV in str(exc.value)

    assert ph.resolve_archive_dir(
        None, env={ph.ARCHIVE_DIR_ENV: str(FIXTURES / ALIGNED)}
    ) == FIXTURES / ALIGNED

    # CLI wins over the environment, even when the env value is bogus.
    assert ph.resolve_archive_dir(
        str(FIXTURES / ALIGNED), env={ph.ARCHIVE_DIR_ENV: str(tmp_path / "nope")}
    ) == FIXTURES / ALIGNED

    # ... and the whole CLI path works from the env var alone.
    monkeypatch.setenv(ph.ARCHIVE_DIR_ENV, str(FIXTURES / ALIGNED))
    assert ph.main(["--quiet"]) == 0

    monkeypatch.delenv(ph.ARCHIVE_DIR_ENV)
    assert ph.main(["--quiet"]) == 2


# --------------------------------------------------------------------------- #
# 8. no hardcoded production paths (static)
# --------------------------------------------------------------------------- #
_ALLOWED_IMPORT_ROOTS = {
    "__future__", "argparse", "csv", "json", "math", "os", "sys",
    "dataclasses", "pathlib", "typing", "pandas", "common", "pipeline",
}


def _source_files():
    module = pathlib.Path(ph.__file__).resolve()
    return [module, pathlib.Path(__file__).resolve()]


def test_no_hardcoded_production_paths_in_module_or_tests():
    # Built by concatenation so THIS file does not itself contain the literals
    # it forbids (the scan below covers this file too).
    forbidden = [
        "/" + "home/",
        "/" + "Users/",
        "C:" + "\\",
        "\\\\" + "home",
        "data" + "/cache",
        # import-shaped needles: prose may legitimately say "no DuckDB / no socket",
        # an `import` of one may not exist.
        "import " + "duckdb",
        "from " + "duckdb",
        "import " + "yfinance",
        "from " + "yfinance",
        "import " + "baostock",
        "from " + "baostock",
        "import " + "requests",
        "from " + "requests",
        "import " + "urllib",
        "from " + "urllib",
        "import " + "socket",
        "from " + "socket",
        "http" + "://",
        "https" + "://",
    ]
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle.lower() not in text.lower(), f"{path.name} contains forbidden {needle!r}"

    # Structural: NO string literal anywhere in the harness may start with "/"
    # (the only way to name an absolute POSIX path). The test file is exempt from
    # this stronger form because it must build the literal checks above.
    tree = ast.parse(pathlib.Path(ph.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not node.value.startswith("/") or node.value == "/", (
                f"absolute path literal in the harness: {node.value!r}"
            )


def test_module_imports_no_network_or_database_stack():
    tree = ast.parse(pathlib.Path(ph.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    assert roots <= _ALLOWED_IMPORT_ROOTS, f"unexpected imports: {roots - _ALLOWED_IMPORT_ROOTS}"


# --------------------------------------------------------------------------- #
# 9. the acceptance line is not a CLI knob
# --------------------------------------------------------------------------- #
def test_thresholds_are_constants_not_cli_flags():
    assert ph.MAX_ABS_DELTA_W == 1e-4
    assert ph.REQUIRED_DELTA_SHARES == 0

    parser = ph._build_arg_parser()
    options = {opt for action in parser._actions for opt in action.option_strings}
    for flag in ("--max-abs-delta-w", "--delta-w", "--threshold", "--tolerance",
                 "--max-delta-w", "--delta-shares"):
        assert flag not in options, f"{flag!r} must not be a CLI knob"
    dests = {action.dest for action in parser._actions}
    assert not ({"max_abs_delta_w", "threshold", "tolerance", "delta_shares"} & dests)
    # ... and the report echoes the constants, so a run is auditable.
    payload = ph.run_parity(FIXTURES / ALIGNED, env={}).to_dict()
    assert payload["thresholds"] == {
        "max_abs_delta_weight": ph.MAX_ABS_DELTA_W,
        "delta_shares": ph.REQUIRED_DELTA_SHARES,
    }


# --------------------------------------------------------------------------- #
# 10. fixture integrity: the archives obey the LIVE sizing rule
# --------------------------------------------------------------------------- #
def test_fixture_rows_follow_the_live_sizing_rule():
    """Recompute every fixture row from `scripts/run_live_strategy.py`'s rule.

    LIVE rule (verified against `run_live_strategy.py`): BUY rounds the lot count
    DOWN (`int(np.floor(raw/lot))*lot`), SELL rounds it UP
    (`int(np.ceil(raw/lot))*lot`), a full exit sells the whole position, and any
    gated / no-trade action keeps `current_shares` untouched. Pinning it here
    means a hand-edited fixture cannot become self-fulfilling.
    """
    fixture_dirs = [p for p in sorted(FIXTURES.iterdir()) if p.is_dir()]
    assert len(fixture_dirs) == 4

    for run_dir in fixture_dirs:
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        pv = float(summary["portfolio_value"])
        rows, fieldnames = _read_csv(run_dir / "live_trading_ticket.csv")
        assert rows, f"{run_dir.name} has no ticket rows"

        for row in rows:
            symbol = row["symbol"]
            cur = int(float(row["current_shares"]))
            price = float(row["price"])
            cur_w = float(row["current_weight"])
            tgt_w = float(row["target_weight"])
            delta = int(float(row["delta_shares"]))
            target = int(float(row["target_shares"]))
            action = row["action"].strip().upper()
            blocking = row.get("blocking_reason", "NONE")
            blocking = "NONE" if blocking is None or not str(blocking).strip() else str(blocking).strip()

            assert cur_w == pytest.approx(cur * price / pv, abs=1e-12), (
                f"{run_dir.name}/{symbol}: current_weight must be shares*price/PV"
            )
            assert target == cur + delta, f"{run_dir.name}/{symbol}: target != current + delta"

            raw = abs(tgt_w - cur_w) * pv / price
            if action in ph.NO_TRADE_ACTIONS or blocking.upper() not in ("NONE",):
                assert delta == 0 and target == cur, (
                    f"{run_dir.name}/{symbol}: a gated/no-trade row must not move shares"
                )
                continue
            if action == "BUY":
                assert delta == int(math.floor(raw / LOT)) * LOT
            elif action == "SELL":
                if tgt_w <= 1e-6:
                    assert delta == -cur
                else:
                    assert delta == -min(int(math.ceil(raw / LOT)) * LOT, cur)
            else:  # pragma: no cover - fixture guard
                raise AssertionError(f"{run_dir.name}/{symbol}: unexpected action {action!r}")


# --------------------------------------------------------------------------- #
# 11. report contract / CLI surface
# --------------------------------------------------------------------------- #
def test_json_report_contract(tmp_path, capsys):
    out = tmp_path / "report.json"
    code = ph.main(["--archive-dir", str(FIXTURES / ALIGNED), "--json", str(out), "--quiet"])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert payload["ok"] is True
    assert payload["verdict"] == "ALIGNED"
    assert payload["archive"]["as_of"] == "2026-10-22"
    assert set(payload) >= {
        "ok", "verdict", "archive", "thresholds", "summary", "meta",
        "rows", "not_aligned_rows", "unexplained_rows", "non_replayable_rows",
    }
    summary = payload["summary"]
    assert summary["rows"] == 6 and summary["aligned"] == 6
    assert summary["unexplained"] == 0
    assert summary["weight_basis"].startswith("realized = target_shares")

    for row in payload["rows"]:
        assert set(row) >= {
            "symbol", "status", "tag", "severity", "action", "blocking_reason",
            "archive_shares", "replay_shares", "delta_shares",
            "archive_w", "replay_w", "delta_w",
            "archive_target_weight", "truncation_gap", "price", "explanation",
        }

    replay = payload["meta"]["replay"]
    assert replay["execution_price"] == "close"
    assert replay["seed_date"] == "2026-10-21"
    assert replay["fills"], "the replay must report at least one fill"
    assert any(f["action"] == "SELL" for f in replay["fills"])
    assert payload["meta"]["disclosures"], "the harness must disclose its own limits"
    assert any("market data" in line for line in payload["meta"]["disclosures"])

    # `--json -` writes machine output to stdout only
    assert ph.main(["--archive-dir", str(FIXTURES / ALIGNED), "--json", "-"]) == 0
    stdout = capsys.readouterr().out
    assert json.loads(stdout)["verdict"] == "ALIGNED"


def test_human_report_shows_unexplained_rows_and_the_verdict():
    report = ph.run_parity(FIXTURES / GATES, env={})
    text = ph.format_report(report)
    assert "VERDICT: DIVERGED" in text
    assert "UNEXPLAINED rows (2)" in text
    assert "NON-REPLAYABLE rows (2)" in text
    assert "000001.SZ" in text and "COOLDOWN_BLOCKED" in text


def test_rows_without_share_counts_are_unverifiable_not_aligned(tmp_path):
    """No share count on either side -> refuse to call the row aligned."""
    run_dir = _copy(tmp_path, ALIGNED)
    path = run_dir / "live_trading_ticket.csv"
    rows, fieldnames = _read_csv(path)
    for row in rows:
        row.pop("current_shares", None)
        row.pop("target_shares", None)
        row.pop("delta_shares", None)
    keep = [f for f in fieldnames if f not in ("current_shares", "target_shares", "delta_shares")]
    _write_csv(path, rows, keep)
    (run_dir / "holdings.json").write_text("{}\n", encoding="utf-8")

    report = ph.run_parity(run_dir, env={})

    assert report.verdict == "INCONCLUSIVE"   # not "ALIGNED"
    assert report.ok is False
    assert report.unexplained_rows == []
    assert all(r.tag == ph.TAG_UNVERIFIABLE_ROW for r in report.rows)
    assert all(r.status == ph.STATUS_NON_REPLAYABLE for r in report.rows)
    assert ph.main(["--archive-dir", str(run_dir), "--quiet"]) == 1


def test_cost_model_engine_exposes_the_fee_drag(tmp_path):
    """Default = zero costs (sizing-comparable); engine costs ADD a divergence."""
    baseline = ph.run_parity(FIXTURES / ALIGNED, env={})
    assert baseline.verdict == "ALIGNED"

    costly = ph.run_parity(FIXTURES / ALIGNED, cost_model="engine", env={})
    assert costly.verdict == "DIVERGED"
    assert costly.meta["replay"]["cost_model"] == "engine"
    assert "fee drag" in costly.meta["replay"]["cost_model_note"]
    assert costly.divergent_max_abs_delta_w > ph.MAX_ABS_DELTA_W
    # the archived book paid no fees, so it can differ by at most a lot per row
    assert costly.divergent_max_abs_delta_shares <= LOT


# --------------------------------------------------------------------------- #
# 12. tolerant reads of a partially-populated archive
# --------------------------------------------------------------------------- #
def test_as_of_falls_back_to_price_bar_date(tmp_path):
    run_dir = _copy(tmp_path, ALIGNED)
    payload = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    for key in ("date", "as_of"):
        payload.pop(key, None)
    (run_dir / "summary.json").write_text(json.dumps(payload), encoding="utf-8")

    rows, fieldnames = _read_csv(run_dir / "live_trading_ticket.csv")
    for row in rows:
        row["price_bar_date"] = "2026-10-22"
    _write_csv(run_dir / "live_trading_ticket.csv", rows, fieldnames + ["price_bar_date"])

    snapshot = ph.load_archive(run_dir, env={})
    assert snapshot.as_of.strftime("%Y-%m-%d") == "2026-10-22"
    assert any("price_bar_date" in note for note in snapshot.notes)
    assert ph.run_parity(run_dir, env={}).verdict == "ALIGNED"


def test_weights_based_holdings_are_converted_to_shares(tmp_path):
    run_dir = _copy(tmp_path, ALIGNED)
    # 3% of 1M at 20.0 == the same 1500 shares the shares-basis fixture holds.
    (run_dir / "holdings.json").write_text(json.dumps({"600000.SH": 0.03}), encoding="utf-8")

    snapshot = ph.load_archive(run_dir, env={})
    assert "weights" in snapshot.holdings_basis
    assert snapshot.prior_shares["600000.SH"] == pytest.approx(0.03)  # raw weight, not a share count
    report = ph.run_parity(run_dir, env={})
    assert report.verdict == "ALIGNED"
    assert _row(report, "600000.SH").archive_shares == 1500

    # Forcing the SHARES basis on the same weights file reads 0.03 as "0 shares",
    # i.e. the flag is honoured instead of being silently re-guessed -- and the
    # row is then a replay/holdings disagreement the report must expose.
    forced = ph.run_parity(run_dir, holdings_basis="shares", env={})
    assert ph.load_archive(run_dir, holdings_basis="shares", env={}).prior_shares["600000.SH"] == 0
    assert _row(forced, "600000.SH").archive_shares == 0


def test_portfolio_value_override_is_honoured(tmp_path):
    report = ph.run_parity(FIXTURES / ALIGNED, portfolio_value=2_000_000.0, env={})
    # Doubling the NAV basis doubles every target share count -> divergence.
    assert report.verdict == "DIVERGED"
    row = _row(report, "600519.SH")
    assert row.replay_shares == 5000  # 0.25 * 2M / 100
    assert row.archive_shares == 2500


def test_report_is_json_serializable_for_every_fixture():
    for name in (ALIGNED, GATES, LEGACY, SELL_CEIL):
        report = ph.run_parity(FIXTURES / name, env={})
        payload = report.to_dict()
        # `default=` covers numpy scalars; a plain round-trip proves no leaks.
        text = json.dumps(payload, ensure_ascii=False, default=ph._json_default)
        reloaded = json.loads(text)
        assert reloaded["verdict"] == report.verdict
        assert len(reloaded["rows"]) == len(report.rows)


def test_snapshot_is_immutable_input_copy(tmp_path):
    """`run_parity` must not mutate the archive it was pointed at."""
    run_dir = _copy(tmp_path, ALIGNED)
    before = {p.name: p.read_bytes() for p in sorted(run_dir.iterdir())}
    ph.run_parity(run_dir, env={})
    after = {p.name: p.read_bytes() for p in sorted(run_dir.iterdir())}
    assert before == after


def test_deep_copied_ticket_helper_does_not_share_state(tmp_path):
    """Guard the fixture helpers themselves (a shared dict would fake alignment)."""
    run_dir = _copy(tmp_path, ALIGNED)
    snapshot = ph.load_archive(run_dir, env={})
    reference = copy.deepcopy({k: v.raw for k, v in snapshot.ticket.items()})
    ph.run_parity(run_dir, env={})
    assert {k: v.raw for k, v in ph.load_archive(run_dir, env={}).ticket.items()} == reference
    # and `re` is imported for the source scan in another test module's spirit
    assert re.search(r"parity", pathlib.Path(ph.__file__).name)


def test_lifecycle_path_parity_multi_day():
    """The sequential replay is a WRAPPER check, not a live-parity claim (F4).

    Without `live_decisions` the helper must report INCONCLUSIVE; supplying the live
    side turns it into a real comparison. The records fed below are the simulator's
    own output used as a fixture -- they pin the comparison mechanics only.
    """
    import numpy as np
    import pandas as pd
    from common.position_lifecycle import PositionLifecycleConfig, simulate_position_lifecycle
    from common.testing import make_ohlcv_from_closes as make_df

    dates = pd.bdate_range("2020-01-01", periods=15)
    universe = {
        "A": make_df([100.0 + i for i in range(15)], start="2020-01-01"),
        "B": make_df([50.0 - 0.5 * i for i in range(15)], start="2020-01-01"),
    }
    weights = pd.DataFrame(np.nan, index=dates, columns=["A", "B"])
    weights.loc[dates[0], "A"] = 0.5
    weights.loc[dates[0], "B"] = 0.5
    weights.loc[dates[5], "A"] = 0.6
    weights.loc[dates[5], "B"] = 0.4

    cfg = PositionLifecycleConfig(guard_latency_bars=2, min_holding_bars=2)

    res = ph.verify_lifecycle_path_parity(universe, weights, cfg=cfg, market="china")
    assert res["status"] == ph.LIFECYCLE_PARITY_INCONCLUSIVE
    assert res["parity"] is False
    assert res["replay_mismatches"] == []
    assert res["days_checked"] == 15

    _, sim_records = simulate_position_lifecycle(universe, weights, cfg=cfg, market="china")
    aligned = ph.verify_lifecycle_path_parity(
        universe, weights, cfg=cfg, market="china", live_decisions=sim_records
    )
    assert aligned["status"] == ph.LIFECYCLE_PARITY_ALIGNED
    assert aligned["parity"] is True
    assert aligned["mismatches"] == []
