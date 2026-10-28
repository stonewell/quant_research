"""Holdings-input reconciliation for the live trading station (`L2`-adjacent defect fix).

FORENSIC CONTEXT (what this suite guards)
-----------------------------------------
The live station has NO broker fill-receipt channel: nothing in
``web/livetrading`` ever sees ``fill_price`` / ``filled_shares`` / a broker callback
(``grep -rn "成交回执|fill_price|filled_shares|broker_api|回写" web/livetrading`` -> 0 hits).
``StorageManager.save_holdings()`` therefore persists whatever the operator's DOM table
submits, and the page used to keep that table unchanged after a deploy -- so the NEXT
day's deploy re-submitted the PREVIOUS round's pre-trade holdings.  Proven in the live
archive: ``10-27/holdings.json`` was byte-identical (md5 3517d8b5...) to
``10-22/post_holdings.json`` while ``10-26/post_holdings.json`` already described a
different book.

Two things are tested here, both without touching the real ``data/`` tree:

1. ``storage.diff_holdings_input_vs_prev_post`` -- the PURE comparison between the
   operator's input book and the previous round's PLANNED post-ticket book.  Advisory
   only: a mismatch is not proof of an error (partial fills look identical), so the
   function may never mutate anything.
2. ``StorageManager.get_previous_post_holdings`` -- which archive directory the
   comparison is made against.  It must read a PLANNED book and must NOT fall back to
   ``holdings.json`` (that file holds the day's PRE-trade INPUT, i.e. the very value
   being audited -- comparing against it would make the check agree with itself).

WHY THIS FILE LIVES IN ``pipeline/tests/``
------------------------------------------
``web/livetrading/tests`` needs ``fastapi`` (``server.create_app``); the shared
``pipeline`` venv has no ``fastapi``, so that package does not collect there.  Like
``test_live_output_path_agreement.py``, this suite loads ``storage.py`` as a standalone
module via ``importlib`` -- ``storage.py`` itself imports only stdlib + pandas +
``common.*`` / ``scripts.live_paths`` (no fastapi), which is asserted below so a future
import of ``fastapi`` into that module cannot silently break offline collection.

100% offline: only ``tmp_path`` directories, no market data, no network.
"""

import importlib.util
import json
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _load_web_storage():
    """Load ``web/livetrading/storage.py`` as a standalone script module.

    Mirrors ``test_live_output_path_agreement.py``: the web app imports its own modules by
    bare name after putting ``web/livetrading`` on ``sys.path``; loading it under a unique
    name keeps this suite independent of that setup and of the web test package's conftest
    (which reseats ``LIVETRADING_*`` directories).
    """
    path = os.path.join(_REPO_ROOT, "web", "livetrading", "storage.py")
    spec = importlib.util.spec_from_file_location("web_livetrading_storage_holdings_input", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


storage = _load_web_storage()
diff = storage.diff_holdings_input_vs_prev_post


# ---------------------------------------------------------------------------
# 0. Import hygiene: the pure function must stay importable without fastapi
# ---------------------------------------------------------------------------


def test_storage_module_does_not_pull_in_fastapi():
    """`storage.py` is the pure/testable half of the web trio -- it must not need fastapi.

    If someone ever adds a module-level `from fastapi import ...` here, this suite (and
    every offline consumer of `diff_holdings_input_vs_prev_post`) would stop collecting in
    the shared `pipeline` venv, where fastapi is not installed.  Checked on the AST (not a
    text grep) so the docstring prose that names fastapi is not a false positive.
    """
    import ast

    path = os.path.join(_REPO_ROOT, "web", "livetrading", "storage.py")
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "fastapi" not in imported, (
        "web/livetrading/storage.py must stay free of fastapi (web-facing models belong in "
        "models.py / server.py) -- it is imported by offline tests without fastapi."
    )
    assert "fastapi" not in sys.modules


# ---------------------------------------------------------------------------
# 1. diff_holdings_input_vs_prev_post -- the pure comparison
# ---------------------------------------------------------------------------


def test_identical_books_produce_no_mismatch():
    check = diff({"600900.SH": 800, "600660.SH": 400}, {"600900.SH": 800, "600660.SH": 400})
    assert check == {"mismatched": {}, "n_mismatched": 0, "n_symbols": 2}


def test_missing_key_on_the_prev_side_is_a_mismatch():
    """Symbol held in the INPUT but absent from last round's plan -> input/prev_post/delta."""
    check = diff({"600900.SH": 800}, {})
    assert check["mismatched"] == {"600900.SH": {"input": 800, "prev_post": 0, "delta": 800}}
    assert check["n_mismatched"] == 1
    assert check["n_symbols"] == 1


def test_missing_key_on_the_input_side_is_a_mismatch():
    """Double-sided by design: a PLANNED symbol the input dropped is reported too."""
    check = diff({}, {"600660.SH": 400})
    assert check["mismatched"] == {"600660.SH": {"input": 0, "prev_post": 400, "delta": -400}}
    assert check["n_mismatched"] == 1


def test_both_sides_disjoint_is_reported_in_both_directions():
    check = diff({"000999.SZ": 600}, {"600660.SH": 400})
    assert check["n_mismatched"] == 2
    assert check["n_symbols"] == 2
    assert check["mismatched"]["000999.SZ"]["delta"] == 600
    assert check["mismatched"]["600660.SH"]["delta"] == -400


def test_same_symbol_different_quantity_reports_the_delta():
    check = diff({"600900.SH": 1000}, {"600900.SH": 800})
    assert check["mismatched"] == {"600900.SH": {"input": 1000, "prev_post": 800, "delta": 200}}
    assert check["n_symbols"] == 1


def test_all_different_book():
    """The real incident shape: a whole stale round, several symbols, both directions."""
    current = {"600900.SH": 800, "600660.SH": 400, "000999.SZ": 600}
    prev_post = {"600900.SH": 1200, "601728.SH": 5800, "601988.SH": 2000}
    check = diff(current, prev_post)
    assert check["n_symbols"] == 5
    assert set(check["mismatched"]) == {
        "600900.SH",
        "600660.SH",
        "000999.SZ",
        "601728.SH",
        "601988.SH",
    }
    assert check["mismatched"]["600900.SH"]["delta"] == -400
    assert check["mismatched"]["601728.SH"] == {"input": 0, "prev_post": 5800, "delta": -5800}


def test_empty_dicts_are_consistent():
    assert diff({}, {}) == {"mismatched": {}, "n_mismatched": 0, "n_symbols": 0}


def test_explicit_zero_matches_an_absent_symbol():
    """`0` and "not held" are the same position -- must not be flagged (nor crash)."""
    assert diff({"600900.SH": 0}, {})["n_mismatched"] == 0
    assert diff({}, {"600900.SH": 0})["n_mismatched"] == 0
    assert diff({"600900.SH": 0}, {"600900.SH": 0})["n_mismatched"] == 0


def test_zero_input_against_a_real_plan_is_a_mismatch():
    check = diff({"600900.SH": 0}, {"600900.SH": 800})
    assert check["mismatched"] == {"600900.SH": {"input": 0, "prev_post": 800, "delta": -800}}


def test_none_payloads_behave_like_empty_books():
    """A fresh checkout / missing key must not raise inside the deploy path."""
    assert diff(None, None) == {"mismatched": {}, "n_mismatched": 0, "n_symbols": 0}
    assert diff(None, {"600900.SH": 800})["n_mismatched"] == 1


def test_symbol_case_and_whitespace_are_not_mismatches():
    """The DOM table and `post_holdings_from_ticket` disagree on casing in practice."""
    check = diff({" 600900.sh ": 800}, {"600900.SH": 800})
    assert check["n_mismatched"] == 0
    assert check["n_symbols"] == 1


def test_string_and_row_dict_values_are_coerced():
    """Holdings payloads arrive as JSON numbers, numeric strings, or `{shares: n}` rows."""
    check = diff({"600900.SH": "800", "600660.SH": {"shares": 400}}, {"600900.SH": 800, "600660.SH": 400})
    assert check["n_mismatched"] == 0


def test_cash_symbols_are_not_filtered_here_the_caller_does_it():
    """Pure dict differ by contract -- cash filtering belongs to the caller.

    `execution_service._check_holdings_input` drops BIL/CASH from the INPUT side only,
    because `post_holdings_from_ticket` never records cash (cash is the implied NAV
    residual). Documented here so nobody "fixes" it into the pure function, where it would
    silently hide a real mismatch on a genuine BIL position.
    """
    check = diff({"BIL": 100}, {})
    assert check["mismatched"]["BIL"] == {"input": 100, "prev_post": 0, "delta": 100}


def test_fractional_deltas_are_not_truncated():
    check = diff({"600900.SH": 100.5}, {"600900.SH": 100})
    assert check["mismatched"]["600900.SH"]["delta"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 2. format_holdings_input_mismatch -- the readable operator summary
# ---------------------------------------------------------------------------


def test_summary_line_lists_at_most_five_symbols_and_the_totals():
    current = {f"60090{i}.SH": 1000 + i for i in range(7)}
    prev_post = {f"60090{i}.SH": 500 for i in range(7)}
    check = diff(current, prev_post)
    line = storage.format_holdings_input_mismatch(check)
    assert line  # non-empty
    assert "7/7" in line
    assert "(+2 more)" in line
    # gross |delta| = 500+501+...+506 = 3521; net = same (all positive)
    assert "3521" in line
    # Only the first 5 symbols are enumerated; the 6th/7th are folded into "(+2 more)".
    assert "600900.SH" in line and "600905.SH" not in line


def test_summary_line_is_empty_when_books_agree():
    assert storage.format_holdings_input_mismatch(diff({"600900.SH": 800}, {"600900.SH": 800})) == ""
    assert storage.format_holdings_input_mismatch({}) == ""


# ---------------------------------------------------------------------------
# 3. Strict-mode switch (env, matching the existing LIVETRADING_* convention)
# ---------------------------------------------------------------------------


def test_strict_mode_is_off_by_default(monkeypatch):
    monkeypatch.delenv("LIVETRADING_STRICT_HOLDINGS_INPUT", raising=False)
    assert storage.strict_holdings_input_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_strict_mode_env_opt_in(monkeypatch, value):
    monkeypatch.setenv("LIVETRADING_STRICT_HOLDINGS_INPUT", value)
    assert storage.strict_holdings_input_enabled() is True


def test_holdings_input_mismatch_error_is_a_runtime_error():
    """The server maps any exception to HTTP 500 + `detail`, which the UI surfaces."""
    assert issubclass(storage.HoldingsInputMismatchError, RuntimeError)


# ---------------------------------------------------------------------------
# 4. StorageManager.get_previous_post_holdings -- WHICH archive is the reference
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_storage(tmp_path):
    """Hermetic StorageManager: every directory under `tmp_path`, never the real tree."""
    for name in ("data", "archive", "cache", "settings"):
        (tmp_path / name).mkdir()
    return storage.StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(tmp_path / "data"),
        archive_dir=str(tmp_path / "archive"),
        cache_dir=str(tmp_path / "cache"),
        settings_dir=str(tmp_path / "settings"),
    )


def _make_run(archive_dir, date_str, summary, files):
    run_dir = os.path.join(archive_dir, "strat_x", "univ_y", date_str)
    os.makedirs(run_dir, exist_ok=True)
    payload = {"date": date_str, "strategy_key": "strat_x", "universe_key": "univ_y"}
    payload.update(summary or {})
    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f)
    for fname, content in files.items():
        with open(os.path.join(run_dir, fname), "w", encoding="utf-8") as f:
            json.dump(content, f)
    return run_dir


def test_previous_post_holdings_comes_from_the_planned_book(temp_storage, tmp_path):
    run_dir = _make_run(
        str(tmp_path / "archive"),
        "2026-10-26",
        {},
        {"post_holdings.json": {"600900.SH": 800, "600660.SH": 400}},
    )
    holdings, where = temp_storage.get_previous_post_holdings(
        date_str="2026-10-27", strategy_key="strat_x", universe_key="univ_y"
    )
    assert holdings == {"600900.SH": 800, "600660.SH": 400}
    assert where == run_dir


def test_previous_post_holdings_derives_from_the_previous_ticket_when_no_post_file(temp_storage, tmp_path):
    """Older archives / CLI runs may lack `post_holdings.json` but carry `target_shares`."""
    _make_run(
        str(tmp_path / "archive"),
        "2026-10-24",
        {},
        {
            "live_trading_ticket.json": [
                {"symbol": "600900.SH", "action": "BUY", "target_shares": 800},
                {"symbol": "600660.SH", "action": "BUY", "target_shares": 400},
                {"symbol": "BIL", "action": "SWEEP_CASH", "target_shares": 0},
            ]
        },
    )
    holdings, where = temp_storage.get_previous_post_holdings(
        date_str="2026-10-27", strategy_key="strat_x", universe_key="univ_y"
    )
    assert holdings == {"600900.SH": 800, "600660.SH": 400}
    assert where is not None


def test_previous_post_holdings_never_uses_holdings_json(temp_storage, tmp_path):
    """`holdings.json` is the day's PRE-trade INPUT -- the value being audited.

    Using it as the reference would make the check agree with itself and hide exactly the
    defect it exists to catch, so a run carrying ONLY `holdings.json` must yield no
    reference at all.
    """
    _make_run(str(tmp_path / "archive"), "2026-10-26", {}, {"holdings.json": {"600900.SH": 999}})
    holdings, where = temp_storage.get_previous_post_holdings(
        date_str="2026-10-27", strategy_key="strat_x", universe_key="univ_y"
    )
    assert (holdings, where) == (None, None)


def test_previous_post_holdings_ignores_the_same_day_and_later_runs(temp_storage, tmp_path):
    """The reference must be strictly EARLIER -- today's own (pre-trade) archive is not it."""
    archive = str(tmp_path / "archive")
    _make_run(archive, "2026-10-27", {}, {"post_holdings.json": {"600900.SH": 1}})
    _make_run(archive, "2026-10-26", {}, {"post_holdings.json": {"600900.SH": 800}})
    holdings, _ = temp_storage.get_previous_post_holdings(
        date_str="2026-10-27", strategy_key="strat_x", universe_key="univ_y"
    )
    assert holdings == {"600900.SH": 800}


def test_previous_post_holdings_none_on_a_first_run(temp_storage, tmp_path):
    """No earlier archive -> no reference, so the caller reports "unavailable", not a fake diff."""
    assert temp_storage.get_previous_post_holdings(
        date_str="2026-10-27", strategy_key="strat_x", universe_key="univ_y"
    ) == (None, None)
