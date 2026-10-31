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


# ---------------------------------------------------------------------------
# 5. The multi-strategy matrix chain: WHICH archive is the reference there
# ---------------------------------------------------------------------------
# `run_multi_strategy_netted_deploy` writes `<archive>/<date>/multi_netted_<matrix>`,
# which is deliberately NOT part of the per-(strategy, universe) carry-forward chain
# that `get_previous_post_holdings` walks.  Before this was addressed, the netted path
# returned `holdings_plan` only and did NO comparison at all -- i.e. the advisory check
# was inert on exactly the deployment mode that produced the 2026-10-27 stale book.


def _make_matrix_run(archive_dir, date_str, matrix_id, ticket=None, post=None, holdings=None,
                     account_state=None):
    run_dir = os.path.join(archive_dir, date_str, f"multi_netted_{matrix_id}")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "matrix_id": matrix_id}, f)
    if ticket is not None:
        with open(os.path.join(run_dir, "netted_trading_ticket.json"), "w", encoding="utf-8") as f:
            json.dump(ticket, f)
    if post is not None:
        with open(os.path.join(run_dir, "post_holdings.json"), "w", encoding="utf-8") as f:
            json.dump(post, f)
    if holdings is not None:
        with open(os.path.join(run_dir, "holdings.json"), "w", encoding="utf-8") as f:
            json.dump(holdings, f)
    if account_state is not None:
        with open(os.path.join(run_dir, "account_state.json"), "w", encoding="utf-8") as f:
            json.dump(account_state, f)
    return run_dir


def test_matrix_previous_book_is_derived_from_the_netted_ticket(temp_storage, tmp_path):
    archive = str(tmp_path / "archive")
    run_dir = _make_matrix_run(
        archive,
        "2026-10-26",
        "tri_core_blend",
        ticket=[
            {"symbol": "600900.SH", "action": "BUY", "target_shares": 1200},
            {"symbol": "000999.SZ", "action": "BUY", "target_shares": 600},
            {"symbol": "BIL", "action": "SWEEP_CASH", "target_shares": 0},
        ],
    )
    plan, where = temp_storage.get_previous_multi_netting_post_holdings(
        date_str="2026-10-27", matrix_id="tri_core_blend"
    )
    assert plan == {"600900.SH": 1200, "000999.SZ": 600}
    assert where == run_dir


def test_matrix_previous_book_prefers_post_holdings_file(temp_storage, tmp_path):
    archive = str(tmp_path / "archive")
    _make_matrix_run(
        archive,
        "2026-10-24",
        "tri_core_blend",
        ticket=[{"symbol": "600900.SH", "action": "BUY", "target_shares": 1}],
        post={"600900.SH": 999},
    )
    plan, _ = temp_storage.get_previous_multi_netting_post_holdings(
        date_str="2026-10-27", matrix_id="tri_core_blend"
    )
    assert plan == {"600900.SH": 999}


def test_matrix_previous_book_reads_the_csv_when_json_is_missing(temp_storage, tmp_path):
    archive = str(tmp_path / "archive")
    run_dir = os.path.join(archive, "2026-10-24", "multi_netted_tri_core_blend")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "netted_trading_ticket.csv"), "w", encoding="utf-8") as f:
        f.write("symbol,name,action,price,target_shares,delta_shares\n")
        f.write("600660.SH,X,BUY,10.0,400,400\n")
    plan, _ = temp_storage.get_previous_multi_netting_post_holdings(
        date_str="2026-10-27", matrix_id="tri_core_blend"
    )
    assert plan == {"600660.SH": 400}


def test_matrix_previous_book_ignores_the_same_day_and_other_matrices(temp_storage, tmp_path):
    archive = str(tmp_path / "archive")
    _make_matrix_run(archive, "2026-10-27", "tri_core_blend", post={"600900.SH": 1})
    _make_matrix_run(archive, "2026-10-27", "dual_core_shield", post={"600900.SH": 2})
    _make_matrix_run(archive, "2026-10-26", "tri_core_blend", post={"600900.SH": 1200})
    plan, where = temp_storage.get_previous_multi_netting_post_holdings(
        date_str="2026-10-27", matrix_id="tri_core_blend"
    )
    assert plan == {"600900.SH": 1200}
    assert where.endswith(os.path.join("2026-10-26", "multi_netted_tri_core_blend"))


def test_matrix_previous_book_never_uses_holdings_json(temp_storage, tmp_path):
    """`holdings.json` is the PRE-trade INPUT -- using it would hide the defect."""
    archive = str(tmp_path / "archive")
    _make_matrix_run(archive, "2026-10-26", "tri_core_blend", holdings={"600900.SH": 999})
    assert temp_storage.get_previous_multi_netting_post_holdings(
        date_str="2026-10-27", matrix_id="tri_core_blend"
    ) == (None, None)


def test_matrix_previous_book_none_on_first_run(temp_storage):
    assert temp_storage.get_previous_multi_netting_post_holdings(
        date_str="2026-10-27", matrix_id="tri_core_blend"
    ) == (None, None)


def test_matrix_previous_book_requires_a_valid_date(temp_storage, tmp_path):
    _make_matrix_run(str(tmp_path / "archive"), "2026-10-26", "tri_core_blend", post={"600900.SH": 1})
    with pytest.raises(ValueError):
        temp_storage.get_previous_multi_netting_post_holdings(date_str="2026-10-2x", matrix_id="tri_core_blend")


# ---------------------------------------------------------------------------
# 6. `_check_holdings_input` -- the stale-input detection path itself
# ---------------------------------------------------------------------------
# Loaded the way `server.py` loads it (bare `storage` on sys.path) so the
# `HoldingsInputMismatchError` class identity matches the one `execution_service` raises.


_WEB_DIR = os.path.join(_REPO_ROOT, "web", "livetrading")
if _WEB_DIR not in sys.path:
    sys.path.insert(0, _WEB_DIR)


@pytest.fixture
def live_modules():
    import importlib.util

    # Bare-name import: this is the module `execution_service` will `from storage import ...`.
    import storage as bare_storage

    path = os.path.join(_WEB_DIR, "execution_service.py")
    spec = importlib.util.spec_from_file_location(
        "web_livetrading_execution_service_holdings_input", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return bare_storage, module


@pytest.fixture
def service_workspace(tmp_path, live_modules):
    bare_storage, es = live_modules
    data_dir = tmp_path / "data"
    archive_dir = tmp_path / "archive"
    cache_dir = tmp_path / "cache"
    settings_dir = tmp_path / "settings"
    for d in (data_dir, archive_dir, cache_dir, settings_dir):
        d.mkdir()
    manager = bare_storage.StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(data_dir),
        archive_dir=str(archive_dir),
        cache_dir=str(cache_dir),
        settings_dir=str(settings_dir),
    )
    return es.ExecutionService(manager), manager, str(archive_dir), bare_storage


#: The incident's shape: the 10-27 input book was byte-identical to `10-22/post_holdings.json`
#: while `10-26/post_holdings.json` already described a different book.
_STALE_INPUT = {"600900.SH": 800, "600660.SH": 400, "000999.SZ": 600}
_PREV_PLANNED = {"600900.SH": 1200, "600660.SH": 900, "000999.SZ": 900}


def test_stale_input_is_flagged_against_the_previous_planned_book(service_workspace, monkeypatch):
    monkeypatch.delenv("LIVETRADING_STRICT_HOLDINGS_INPUT", raising=False)
    service, manager, archive, _bare_storage = service_workspace
    prev_dir = _make_matrix_run(archive, "2026-10-26", "tri_core_blend", post=_PREV_PLANNED)

    check = service._check_holdings_input(
        _STALE_INPUT, val_date="2026-10-27", strategy_key=None, universe_key=None,
        matrix_id="tri_core_blend",
    )
    assert check["prev_post_available"] is True
    assert check["prev_post_run_dir"] == prev_dir
    assert check["strict"] is False
    assert check["n_mismatched"] == 3
    assert check["mismatched"]["600900.SH"] == {"input": 800, "prev_post": 1200, "delta": -400}
    assert check["mismatched"]["600660.SH"]["delta"] == -500
    assert check["mismatched"]["000999.SZ"]["delta"] == -300


def test_up_to_date_input_passes_the_matrix_check(service_workspace, monkeypatch):
    monkeypatch.delenv("LIVETRADING_STRICT_HOLDINGS_INPUT", raising=False)
    service, _manager, archive, _bare_storage = service_workspace
    _make_matrix_run(archive, "2026-10-26", "tri_core_blend", post=_PREV_PLANNED)
    check = service._check_holdings_input(
        dict(_PREV_PLANNED), val_date="2026-10-27", strategy_key=None, universe_key=None,
        matrix_id="tri_core_blend",
    )
    assert check["prev_post_available"] is True
    assert check["n_mismatched"] == 0


def test_matrix_check_reports_no_reference_on_the_first_run(service_workspace, monkeypatch):
    monkeypatch.delenv("LIVETRADING_STRICT_HOLDINGS_INPUT", raising=False)
    service, _manager, _archive, _bare_storage = service_workspace
    check = service._check_holdings_input(
        _STALE_INPUT, val_date="2026-10-27", strategy_key=None, universe_key=None,
        matrix_id="tri_core_blend",
    )
    assert check["prev_post_available"] is False
    assert check["n_mismatched"] == 0
    assert check["n_symbols"] == 3


def test_cash_proxies_are_dropped_from_the_input_side(service_workspace, monkeypatch):
    monkeypatch.delenv("LIVETRADING_STRICT_HOLDINGS_INPUT", raising=False)
    service, _manager, archive, _bare_storage = service_workspace
    _make_matrix_run(archive, "2026-10-26", "tri_core_blend", post={"600900.SH": 1200})
    check = service._check_holdings_input(
        {"600900.SH": 1200, "BIL": 43227}, val_date="2026-10-27",
        strategy_key=None, universe_key=None, matrix_id="tri_core_blend",
    )
    assert check["n_mismatched"] == 0
    assert "BIL" not in check["mismatched"]


def test_strict_mode_refuses_the_matrix_deploy_before_writing_holdings(
    service_workspace, monkeypatch
):
    """Strict mode must ABORT -- not merely report -- and must leave no `holdings.json`."""
    import subprocess

    monkeypatch.setenv("LIVETRADING_STRICT_HOLDINGS_INPUT", "1")
    service, manager, archive, bare_storage = service_workspace
    _make_matrix_run(archive, "2026-10-26", "tri_core_blend", post=_PREV_PLANNED)

    called = []

    def _fail_if_called(cmd, *a, **kw):  # pragma: no cover - must never run
        called.append(cmd)
        raise AssertionError("subprocess must not be launched when strict mode refuses")

    monkeypatch.setattr(subprocess, "run", _fail_if_called)

    with pytest.raises(bare_storage.HoldingsInputMismatchError):
        service.run_multi_strategy_netted_deploy(
            matrix_id="tri_core_blend",
            custom_symbols=["600900.SH"],
            as_of_date="2026-10-27",
            current_holdings=_STALE_INPUT,
            data_provider="synthetic",
        )

    assert called == []
    run_dir = manager.get_multi_netting_run_dir("tri_core_blend", "2026-10-27")
    assert not os.path.exists(os.path.join(run_dir, "holdings.json"))


def test_matrix_deploy_records_the_check_in_response_and_summary(
    service_workspace, monkeypatch
):
    """The check must reach both the API response and `summary.json` (forensics)."""
    import subprocess

    monkeypatch.delenv("LIVETRADING_STRICT_HOLDINGS_INPUT", raising=False)
    service, manager, archive, _bare_storage = service_workspace
    _make_matrix_run(archive, "2026-10-26", "tri_core_blend", post=_PREV_PLANNED)

    def _fake_run(cmd, *a, **kw):
        out_dir = cmd[cmd.index("--output-dir") + 1]
        with open(os.path.join(out_dir, "netted_trading_ticket.json"), "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"symbol": "600900.SH", "action": "BUY", "target_shares": 1200},
                    {"symbol": "BIL", "action": "SWEEP_CASH", "target_shares": 0},
                ],
                f,
            )

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _Proc()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = service.run_multi_strategy_netted_deploy(
        matrix_id="tri_core_blend",
        custom_symbols=["600900.SH"],
        as_of_date="2026-10-27",
        current_holdings=_STALE_INPUT,
        data_provider="synthetic",
    )

    assert result["status"] == "success"
    check = result["holdings_input_check"]
    assert check["prev_post_available"] is True
    assert check["n_mismatched"] == 3
    assert result["holdings_plan"] == {"600900.SH": 1200}

    run_dir = manager.get_multi_netting_run_dir("tri_core_blend", "2026-10-27")
    with open(os.path.join(run_dir, "summary.json"), encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["holdings_input_check"]["n_mismatched"] == 3
    assert summary["holdings_input_check"]["prev_post_available"] is True


# ---------------------------------------------------------------------------
# 7. D5 -- the REFERENCE book's own lifecycle cross-check (2026-10-29 incident)
# ---------------------------------------------------------------------------
# FORENSIC CONTEXT (why section 6 was not enough)
# ----------------------------------------------
# On 2026-10-29 the check above reported `n_mismatched: 0` -- a clean pass -- on a book
# that still carried 600584.SH and 600362.SH, two names the operator had already SOLD on
# 10-26.  The differ cannot see it because the defect is in the REFERENCE: those phantom
# 100-share lots had been written into the PLANNED book by earlier rounds (the ticket
# carried them as `HOLD_FILTERED / COOLDOWN_BLOCKED` with `target_shares=100`), so the
# reference and the input agreed with each other.
#
# The independent witness is `account_state.json`'s lifecycle machine.  The invariant
# asserted here is the whole point of D5:
#
#     prev_post[sym] > 0  AND  lifecycle(sym) not a holding state
#       => sym IS flagged as a phantom reference entry
#       => "n_mismatched == 0" is NOT reported as a clean pass
#
# The 2026-10-29 archive proves the shape: `2026-10-29/holdings.json` (and
# `2026-10-29/215823/holdings.json`) were byte-identical to `2026-10-27/post_holdings.json`,
# six symbols including both ghosts, while the lifecycle machine had them at FLAT.

#: The incident's book: 4 genuinely held names + the 2 phantom residuals.
_INCIDENT_BOOK = {
    "600900.SH": 800,
    "600660.SH": 400,
    "000999.SZ": 600,
    "601728.SH": 5800,
    "600584.SH": 100,
    "600362.SH": 100,
}

_INCIDENT_LIFECYCLE = {
    "600900.SH": {"symbol": "600900.SH", "state": "HELD_ACTIVE", "held_bars": 6},
    "600660.SH": {"symbol": "600660.SH", "state": "HELD_ACTIVE", "held_bars": 6},
    "000999.SZ": {"symbol": "000999.SZ", "state": "HELD_GESTATION", "held_bars": 1},
    "601728.SH": {"symbol": "601728.SH", "state": "PENDING_EXIT", "held_bars": 9},
    # Sold on 10-26; the residual lot survived in the input/planned book.
    "600584.SH": {"symbol": "600584.SH", "state": "FLAT", "held_bars": 0},
    "600362.SH": {"symbol": "600362.SH", "state": "COOLDOWN", "held_bars": 0},
}


def test_phantom_reference_entry_is_flagged_when_lifecycle_says_flat(
    service_workspace, monkeypatch
):
    """The 10-29 shape: the input EQUALS the planned book, so the differ sees nothing."""
    monkeypatch.delenv("LIVETRADING_STRICT_HOLDINGS_INPUT", raising=False)
    service, _manager, archive, _bare_storage = service_workspace
    _make_matrix_run(
        archive,
        "2026-10-26",
        "tri_core_blend",
        post=_INCIDENT_BOOK,
        account_state={"lifecycle_states": _INCIDENT_LIFECYCLE},
    )

    check = service._check_holdings_input(
        dict(_INCIDENT_BOOK),
        val_date="2026-10-29",
        strategy_key=None,
        universe_key=None,
        matrix_id="tri_core_blend",
    )

    # The differ itself is unchanged: identical books, no mismatch...
    assert check["n_mismatched"] == 0
    # ...and that must NOT be reported as a clean pass.
    assert check["clean_pass"] is False
    assert check["prev_post_lifecycle_available"] is True
    assert check["phantom_reference_warning_code"] == storage.PHANTOM_REFERENCE_ENTRY
    assert check["n_phantom_reference"] == 2
    assert set(check["phantom_reference"]) == {"600584.SH", "600362.SH"}
    assert check["phantom_reference"]["600584.SH"] == {
        "prev_post": 100,
        "lifecycle_state": "FLAT",
    }
    assert check["phantom_reference"]["600362.SH"]["lifecycle_state"] == "COOLDOWN"


def test_clean_pass_requires_no_mismatch_and_no_phantom_reference(
    service_workspace, monkeypatch
):
    """A genuinely consistent round still reports a clean pass."""
    monkeypatch.delenv("LIVETRADING_STRICT_HOLDINGS_INPUT", raising=False)
    service, _manager, archive, _bare_storage = service_workspace
    clean_book = {"600900.SH": 800, "000999.SZ": 600}
    _make_matrix_run(
        archive,
        "2026-10-26",
        "tri_core_blend",
        post=clean_book,
        account_state={
            "lifecycle_states": {
                "600900.SH": {"symbol": "600900.SH", "state": "HELD_ACTIVE"},
                "000999.SZ": {"symbol": "000999.SZ", "state": "HELD_GESTATION"},
            }
        },
    )
    check = service._check_holdings_input(
        dict(clean_book), val_date="2026-10-29", strategy_key=None, universe_key=None,
        matrix_id="tri_core_blend",
    )
    assert check["n_mismatched"] == 0
    assert check["n_phantom_reference"] == 0
    assert check["clean_pass"] is True


@pytest.mark.parametrize("state", ["HELD_GESTATION", "HELD_ACTIVE", "PENDING_EXIT"])
def test_holding_lifecycle_states_are_never_flagged(service_workspace, monkeypatch, state):
    monkeypatch.delenv("LIVETRADING_STRICT_HOLDINGS_INPUT", raising=False)
    service, _manager, archive, _bare_storage = service_workspace
    book = {"600900.SH": 800}
    _make_matrix_run(
        archive,
        "2026-10-26",
        "tri_core_blend",
        post=book,
        account_state={"lifecycle_states": {"600900.SH": {"symbol": "600900.SH", "state": state}}},
    )
    check = service._check_holdings_input(
        dict(book), val_date="2026-10-29", strategy_key=None, universe_key=None,
        matrix_id="tri_core_blend",
    )
    assert check["n_phantom_reference"] == 0
    assert check["clean_pass"] is True


@pytest.mark.parametrize("state", ["FLAT", "COOLDOWN", "PENDING_ENTRY"])
def test_non_holding_lifecycle_states_are_flagged(state):
    """The pure invariant, independent of any archive layout."""
    out = storage.diff_reference_book_vs_lifecycle(
        {"600584.SH": 100, "600900.SH": 800},
        {"600584.SH": state, "600900.SH": "HELD_ACTIVE"},
    )
    assert out["n_phantom_reference"] == 1
    assert out["phantom_reference"] == {"600584.SH": {"prev_post": 100, "lifecycle_state": state}}
    assert out["reference_symbols_without_lifecycle"] == []


def test_reference_symbol_without_a_lifecycle_entry_is_audited_not_flagged():
    """No recorded state is not evidence of an exit (legacy archives predate the machine).

    The symbol is surfaced in `reference_symbols_without_lifecycle` so the gap is visible,
    but it must not flip a deploy into "phantom reference" on its own.
    """
    out = storage.diff_reference_book_vs_lifecycle(
        {"600900.SH": 800, "601728.SH": 5800}, {"600900.SH": "HELD_ACTIVE"}
    )
    assert out["n_phantom_reference"] == 0
    assert out["reference_symbols_without_lifecycle"] == ["601728.SH"]


def test_zero_and_cash_entries_are_not_phantom_candidates():
    out = storage.diff_reference_book_vs_lifecycle(
        {"600900.SH": 0, "600584.SH": 100, "BIL": 0}, {"600900.SH": "FLAT", "600584.SH": "FLAT"}
    )
    assert list(out["phantom_reference"]) == ["600584.SH"]


def test_lifecycle_reader_accepts_the_prior_state_block_and_lowercase():
    states = storage.extract_lifecycle_states(
        {"prior_state": {"lifecycle_states": {" 600584.sh ": {"state": "flat"}}}}
    )
    assert states == {"600584.SH": "FLAT"}


def test_previous_run_without_lifecycle_states_keeps_the_old_behaviour(
    service_workspace, monkeypatch
):
    """A pre-lifecycle (`1.x`) archive: nothing recorded -> nothing flagged."""
    monkeypatch.delenv("LIVETRADING_STRICT_HOLDINGS_INPUT", raising=False)
    service, _manager, archive, _bare_storage = service_workspace
    _make_matrix_run(archive, "2026-10-26", "tri_core_blend", post=_INCIDENT_BOOK)
    check = service._check_holdings_input(
        dict(_INCIDENT_BOOK), val_date="2026-10-29", strategy_key=None, universe_key=None,
        matrix_id="tri_core_blend",
    )
    assert check["prev_post_lifecycle_available"] is False
    assert check["n_phantom_reference"] == 0
    assert check["clean_pass"] is True


def test_phantom_reference_is_also_checked_on_the_per_strategy_chain(
    service_workspace, monkeypatch
):
    """Non-matrix deploys go through `get_previous_post_holdings` -- same cross-check."""
    monkeypatch.delenv("LIVETRADING_STRICT_HOLDINGS_INPUT", raising=False)
    service, _manager, archive, _bare_storage = service_workspace
    run_dir = os.path.join(archive, "strat_x", "univ_y", "2026-10-26")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"date": "2026-10-26", "strategy_key": "strat_x", "universe_key": "univ_y"}, f)
    with open(os.path.join(run_dir, "post_holdings.json"), "w", encoding="utf-8") as f:
        json.dump(_INCIDENT_BOOK, f)
    with open(os.path.join(run_dir, "account_state.json"), "w", encoding="utf-8") as f:
        json.dump({"lifecycle_states": _INCIDENT_LIFECYCLE}, f)

    check = service._check_holdings_input(
        dict(_INCIDENT_BOOK), val_date="2026-10-29", strategy_key="strat_x", universe_key="univ_y"
    )
    assert check["prev_post_run_dir"] == run_dir
    assert check["n_mismatched"] == 0
    assert check["n_phantom_reference"] == 2
    assert check["clean_pass"] is False


def test_strict_mode_refuses_a_phantom_reference(service_workspace, monkeypatch):
    """Strict mode must ABORT on a stale REFERENCE, not merely report it."""
    import subprocess

    monkeypatch.setenv("LIVETRADING_STRICT_HOLDINGS_INPUT", "1")
    service, manager, archive, bare_storage = service_workspace
    _make_matrix_run(
        archive,
        "2026-10-26",
        "tri_core_blend",
        post=_INCIDENT_BOOK,
        account_state={"lifecycle_states": _INCIDENT_LIFECYCLE},
    )

    called = []

    def _fail_if_called(cmd, *a, **kw):  # pragma: no cover - must never run
        called.append(cmd)
        raise AssertionError("subprocess must not be launched when strict mode refuses")

    monkeypatch.setattr(subprocess, "run", _fail_if_called)

    with pytest.raises(bare_storage.HoldingsInputMismatchError):
        service.run_multi_strategy_netted_deploy(
            matrix_id="tri_core_blend",
            custom_symbols=["600900.SH"],
            as_of_date="2026-10-29",
            current_holdings=dict(_INCIDENT_BOOK),
            data_provider="synthetic",
        )
    assert called == []
    run_dir = manager.get_multi_netting_run_dir("tri_core_blend", "2026-10-29")
    assert not os.path.exists(os.path.join(run_dir, "holdings.json"))


def test_phantom_reference_lands_in_the_summary(service_workspace, monkeypatch):
    """Forensics: `summary.json` must carry the phantom block even in warn-only mode."""
    import subprocess

    monkeypatch.delenv("LIVETRADING_STRICT_HOLDINGS_INPUT", raising=False)
    service, manager, archive, _bare_storage = service_workspace
    _make_matrix_run(
        archive,
        "2026-10-26",
        "tri_core_blend",
        post=_INCIDENT_BOOK,
        account_state={"lifecycle_states": _INCIDENT_LIFECYCLE},
    )

    def _fake_run(cmd, *a, **kw):
        out_dir = cmd[cmd.index("--output-dir") + 1]
        with open(os.path.join(out_dir, "netted_trading_ticket.json"), "w", encoding="utf-8") as f:
            json.dump([{"symbol": "600900.SH", "action": "BUY", "target_shares": 800}], f)

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _Proc()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = service.run_multi_strategy_netted_deploy(
        matrix_id="tri_core_blend",
        custom_symbols=["600900.SH"],
        as_of_date="2026-10-29",
        current_holdings=dict(_INCIDENT_BOOK),
        data_provider="synthetic",
    )

    assert result["status"] == "success"
    assert result["holdings_input_check"]["n_phantom_reference"] == 2
    assert result["holdings_input_check"]["clean_pass"] is False

    run_dir = manager.get_multi_netting_run_dir("tri_core_blend", "2026-10-29")
    with open(os.path.join(run_dir, "summary.json"), encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["holdings_input_check"]["n_phantom_reference"] == 2
    assert set(summary["holdings_input_check"]["phantom_reference"]) == {"600584.SH", "600362.SH"}
    assert summary["holdings_input_check"]["clean_pass"] is False
