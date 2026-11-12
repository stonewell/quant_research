"""`D5` -- an EMPTY post-ticket book is not a MISSING post-ticket book.

FORENSIC CONTEXT (archive `runs/2026-11-11/205137`)
---------------------------------------------------
`post_holdings.json` there contains `{}` because that session fully liquidated -- `{}` IS
the correct result.  Nothing in the file says so, and the writer (`storage.
post_holdings_from_ticket`) only keeps keys for `target_shares > 0`, so:

* `{}` (a confirmed all-cash book) and "the file was never produced / the writer died
  half-way" are byte-indistinguishable to every reader;
* downstream (`holdings_input_check.prev_post_available`, the next day's seed) had to guess,
  and a guess of "no book" silently re-buys a book that was deliberately sold (the
  2026-10-26 / 10-27 ghost-lot class recorded in `scripts/repair_live_run_snapshots.py`).

THE FIX PINNED HERE
-------------------
`post_holdings.json` keeps its shape (`{symbol: shares}`, `{}` = flat) and a sidecar is
written next to it::

    post_holdings.meta.json = {"as_of": "YYYY-MM-DD", "complete": true, "n_symbols": N,
                               "generated_at": ISO-8601, "source": "ticket"|"replay"}

`classify_post_holdings_meta` is the pure decision rule; `StorageManager.
post_holdings_meta_status` is its I/O wrapper, and it is what
`_planned_book_from_run_dir` / `extract_post_run_holdings` now consult.

* `{}` + `complete: true`  -> AVAILABLE, book `{}` (a legal all-cash seed);
* book file + `complete: false`/missing field -> UNAVAILABLE + reason, never "flat";
* book file missing -> UNAVAILABLE (`post_holdings_missing`);
* book file + NO sidecar (every pre-D5 archive) -> AVAILABLE, flagged
  `legacy_post_holdings_no_meta` -- the old semantics are preserved on purpose, because
  refusing every legacy archive would make the whole chain unreadable overnight.

100% offline: `tmp_path` archives only, no market data, no network.
"""

import json
import logging
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import storage  # noqa: E402  (bare name, exactly how `server.py` imports it)

STRATEGY_KEY = "strat_x"
UNIVERSE_KEY = "univ_y"
POST = storage.POST_HOLDINGS_FILENAME
META = storage.POST_HOLDINGS_META_FILENAME


@pytest.fixture
def temp_storage(tmp_path):
    """Hermetic `StorageManager`: every directory under `tmp_path`, never the real tree."""
    for name in ("data", "archive", "cache", "settings"):
        (tmp_path / name).mkdir(exist_ok=True)
    return storage.StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(tmp_path / "data"),
        archive_dir=str(tmp_path / "archive"),
        cache_dir=str(tmp_path / "cache"),
        settings_dir=str(tmp_path / "settings"),
    )


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _make_run(archive_dir, date_str, files):
    run_dir = os.path.join(archive_dir, STRATEGY_KEY, UNIVERSE_KEY, date_str)
    os.makedirs(run_dir, exist_ok=True)
    _write_json(
        os.path.join(run_dir, "summary.json"),
        {"date": date_str, "strategy_key": STRATEGY_KEY, "universe_key": UNIVERSE_KEY},
    )
    for fname, content in (files or {}).items():
        _write_json(os.path.join(run_dir, fname), content)
    return run_dir


def _complete_meta(n_symbols=0):
    return {
        "as_of": "2026-11-11",
        "complete": True,
        "n_symbols": n_symbols,
        "generated_at": "2026-11-11T12:52:42+08:00",
        "source": "ticket",
    }


# ---------------------------------------------------------------------------
# 1. The pure rule
# ---------------------------------------------------------------------------


def test_missing_book_file_is_unavailable_even_with_a_complete_sidecar():
    status = storage.classify_post_holdings_meta(_complete_meta(), file_exists=False)
    assert status["available"] is False
    assert status["reason"] == storage.POST_HOLDINGS_REASON_MISSING


def test_empty_book_with_complete_sidecar_is_available():
    status = storage.classify_post_holdings_meta(_complete_meta(), file_exists=True)
    assert status["available"] is True
    assert status["reason"] == storage.POST_HOLDINGS_REASON_OK
    assert status["complete"] is True
    assert status["legacy"] is False


@pytest.mark.parametrize("meta", [{"complete": False}, {"complete": None}, {"as_of": "x"}])
def test_incomplete_sidecar_is_unavailable(meta):
    status = storage.classify_post_holdings_meta(meta, file_exists=True)
    assert status["available"] is False
    assert status["reason"] == storage.POST_HOLDINGS_REASON_INCOMPLETE
    assert status["complete"] is False


@pytest.mark.parametrize("meta", [None, {}, "not-a-mapping"])
def test_book_without_a_sidecar_is_legacy_but_usable(meta):
    """Every pre-D5 archive lands here; `{}` must still mean FLAT for them."""
    status = storage.classify_post_holdings_meta(meta, file_exists=True)
    assert status["available"] is True
    assert status["legacy"] is True
    assert status["reason"] == storage.POST_HOLDINGS_REASON_LEGACY


# ---------------------------------------------------------------------------
# 2. StorageManager wiring: the reason is surfaced, never a silent guess
# ---------------------------------------------------------------------------


def test_status_wrapper_reports_paths_and_the_legacy_warning(temp_storage, tmp_path, caplog):
    run_dir = _make_run(str(tmp_path / "archive"), "2026-11-10", {POST: {"600900.SH": 800}})
    with caplog.at_level(logging.WARNING, logger=storage.logger.name):
        status = temp_storage.post_holdings_meta_status(run_dir)
    assert status["available"] is True
    assert status["legacy"] is True
    assert status["book"] == {"600900.SH": 800}
    assert status["path"] == os.path.join(run_dir, POST)
    assert status["meta_path"] == os.path.join(run_dir, META)
    assert status["meta_present"] is False
    assert any(storage.POST_HOLDINGS_REASON_LEGACY in r.getMessage() for r in caplog.records)


def test_status_wrapper_accepts_a_preloaded_book(temp_storage, tmp_path):
    run_dir = _make_run(str(tmp_path / "archive"), "2026-11-11", {POST: {}, META: _complete_meta()})
    status = temp_storage.post_holdings_meta_status(run_dir, book={})
    assert status["available"] is True
    assert status["book"] == {}
    assert status["n_symbols"] == 0


def test_incomplete_sidecar_is_logged_as_unusable(temp_storage, tmp_path, caplog):
    run_dir = _make_run(
        str(tmp_path / "archive"),
        "2026-11-11",
        {POST: {"600900.SH": 800}, META: {"as_of": "2026-11-11", "complete": False}},
    )
    with caplog.at_level(logging.WARNING, logger=storage.logger.name):
        status = temp_storage.post_holdings_meta_status(run_dir)
    assert status["available"] is False
    assert status["reason"] == storage.POST_HOLDINGS_REASON_INCOMPLETE
    assert any("NOT usable" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 3. The readers: `{}` survives as `{}`, missing/incomplete never becomes `{}`
# ---------------------------------------------------------------------------


def test_planned_book_returns_the_confirmed_empty_book(temp_storage, tmp_path):
    run_dir = _make_run(
        str(tmp_path / "archive"), "2026-11-11", {POST: {}, META: _complete_meta()}
    )
    assert temp_storage._planned_book_from_run_dir(run_dir) == {}


def test_legacy_empty_book_still_reads_as_empty(temp_storage, tmp_path):
    """A pre-D5 archive with no sidecar keeps the old semantics (`{}` = flat)."""
    run_dir = _make_run(str(tmp_path / "archive"), "2026-10-26", {POST: {}})
    assert temp_storage._planned_book_from_run_dir(run_dir) == {}


def test_incomplete_book_falls_back_to_the_ticket_replay(temp_storage, tmp_path):
    """The writer said its own book is partial -> use the ticket instead of trusting it."""
    run_dir = _make_run(
        str(tmp_path / "archive"),
        "2026-11-11",
        {
            POST: {"600900.SH": 1},
            META: {"as_of": "2026-11-11", "complete": False},
            "live_trading_ticket.json": [
                {"symbol": "600900.SH", "action": "BUY", "target_shares": 800},
                {"symbol": "BIL", "action": "SWEEP_CASH", "target_shares": 0},
            ],
        },
    )
    assert temp_storage._planned_book_from_run_dir(run_dir) == {"600900.SH": 800}


def test_incomplete_book_without_a_ticket_is_unknown_not_flat(temp_storage, tmp_path):
    run_dir = _make_run(
        str(tmp_path / "archive"),
        "2026-11-11",
        {POST: {}, META: {"as_of": "2026-11-11", "complete": False}},
    )
    assert temp_storage._planned_book_from_run_dir(run_dir) is None


def test_get_previous_post_holdings_returns_an_empty_book_and_its_dir(temp_storage, tmp_path):
    """The D5 contract at the API boundary: `({}, run_dir)` is a CONFIRMED all-cash book."""
    run_dir = _make_run(
        str(tmp_path / "archive"), "2026-11-11", {POST: {}, META: _complete_meta()}
    )
    holdings, where = temp_storage.get_previous_post_holdings(
        date_str="2026-11-12", strategy_key=STRATEGY_KEY, universe_key=UNIVERSE_KEY
    )
    assert holdings == {}
    assert where == run_dir


def test_get_previous_post_holdings_skips_an_incomplete_book(temp_storage, tmp_path):
    archive = str(tmp_path / "archive")
    _make_run(
        archive,
        "2026-11-11",
        {POST: {}, META: {"as_of": "2026-11-11", "complete": False}},
    )
    _make_run(archive, "2026-11-10", {POST: {"600900.SH": 800}})
    holdings, where = temp_storage.get_previous_post_holdings(
        date_str="2026-11-12", strategy_key=STRATEGY_KEY, universe_key=UNIVERSE_KEY
    )
    assert holdings == {"600900.SH": 800}
    assert where.endswith(os.path.join("2026-11-10"))


def test_extract_post_run_holdings_distinguishes_empty_from_missing(temp_storage, tmp_path):
    archive = str(tmp_path / "archive")
    empty_dir = _make_run(archive, "2026-11-11", {POST: {}, META: _complete_meta()})
    assert temp_storage.extract_post_run_holdings(empty_dir) == {}

    missing_dir = _make_run(archive, "2026-11-10", {})
    assert temp_storage.extract_post_run_holdings(missing_dir) is None


def test_resolve_seed_holdings_honours_a_confirmed_empty_book(temp_storage, tmp_path):
    """`({}, "previous_post_holdings")` = CONFIRMED all-cash, not "we do not know"."""
    _make_run(str(tmp_path / "archive"), "2026-11-11", {POST: {}, META: _complete_meta()})
    book, source = temp_storage.resolve_seed_holdings(
        date_str="2026-11-12", strategy_key=STRATEGY_KEY, universe_key=UNIVERSE_KEY
    )
    assert book == {}
    assert source == "previous_post_holdings"


def test_resolve_seed_holdings_refuses_an_incomplete_book(temp_storage, tmp_path):
    _make_run(
        str(tmp_path / "archive"),
        "2026-11-11",
        {POST: {}, META: {"as_of": "2026-11-11", "complete": False}},
    )
    book, source = temp_storage.resolve_seed_holdings(
        date_str="2026-11-12", strategy_key=STRATEGY_KEY, universe_key=UNIVERSE_KEY
    )
    assert book is None
    assert source == "none"
