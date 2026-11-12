"""`D5` -- `holdings_input_check` must say WHY the previous planned book is (un)usable.

FORENSIC CONTEXT
----------------
`holdings_input_check.prev_post_available` only ever answered "is there a previous planned
book?", so a `post_holdings.json` carrying `{}` because the day fully liquidated, a book
re-derived from the ticket because the file was never written, and an incomplete sidecar all
read identically -- and `n_mismatched: 0` printed as a CLEAN PASS (2026-10-29 archive).

`StorageManager.post_holdings_meta_status` already returns `available` / `complete` /
`reason` from the single classifier (`classify_post_holdings_meta`); this suite pins that
`ExecutionService._check_holdings_input` now surfaces those verdicts as an ADDITIVE
`prev_post_status` block, without changing any existing key's meaning:

* `prev_post_available` keeps meaning "a reference exists";
* `prev_post_status.reason` names WHICH kind of reference it is;
* `prev_post_status.complete` is `None` when unknown (legacy archive), never `False`.

100% offline: `tmp_path` archives only, no market data, no network.
"""

import json
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execution_service  # noqa: E402
import storage  # noqa: E402

STRATEGY_KEY = "strat_x"
UNIVERSE_KEY = "univ_y"
PREV_DATE = "2026-11-10"
AS_OF = "2026-11-11"


@pytest.fixture
def temp_storage(tmp_path):
    for name in ("data", "archive", "cache", "settings"):
        (tmp_path / name).mkdir(exist_ok=True)
    return storage.StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(tmp_path / "data"),
        archive_dir=str(tmp_path / "archive"),
        cache_dir=str(tmp_path / "cache"),
        settings_dir=str(tmp_path / "settings"),
    )


@pytest.fixture
def service(temp_storage):
    return execution_service.ExecutionService(temp_storage)


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _previous_run(store, *, book, meta):
    """Previous session's run dir with `post_holdings.json` and an optional sidecar."""
    run_dir = os.path.join(
        store.archive_dir, STRATEGY_KEY, UNIVERSE_KEY, PREV_DATE
    )
    os.makedirs(run_dir, exist_ok=True)
    _write_json(
        os.path.join(run_dir, "summary.json"),
        {"date": PREV_DATE, "strategy_key": STRATEGY_KEY, "universe_key": UNIVERSE_KEY},
    )
    _write_json(os.path.join(run_dir, storage.POST_HOLDINGS_FILENAME), book)
    if meta is not None:
        _write_json(os.path.join(run_dir, storage.POST_HOLDINGS_META_FILENAME), meta)
    return run_dir


def _complete_meta(n_symbols=1, as_of=PREV_DATE):
    return {
        "as_of": as_of,
        "complete": True,
        "n_symbols": n_symbols,
        "generated_at": f"{as_of}T12:52:42+08:00",
        "source": "ticket",
    }


def _check(service, holdings):
    return service._check_holdings_input(
        holdings, AS_OF, STRATEGY_KEY, UNIVERSE_KEY
    )


# ---------------------------------------------------------------------------
# 1. No reference at all
# ---------------------------------------------------------------------------


def test_no_previous_run_reports_missing_reason_and_keeps_the_existing_keys(service):
    check = _check(service, {"600519.SH": 100})

    # Unchanged meaning of the pre-D5 keys.
    assert check["prev_post_available"] is False
    assert check["prev_post_run_dir"] is None
    assert check["n_mismatched"] == 0
    assert check["clean_pass"] is True

    # ...plus the new, additive verdict.
    assert check["prev_post_status"] == {
        "available": False,
        "complete": None,
        "reason": storage.POST_HOLDINGS_REASON_MISSING,
        "legacy": False,
        "meta_present": False,
        "path": None,
    }


# ---------------------------------------------------------------------------
# 2. A confirmed book (sidecar complete)
# ---------------------------------------------------------------------------


def test_complete_sidecar_is_reported_as_available_and_complete(service, temp_storage):
    _previous_run(temp_storage, book={"600519.SH": 100}, meta=_complete_meta(1))

    check = _check(service, {"600519.SH": 100})

    assert check["prev_post_available"] is True
    assert check["n_mismatched"] == 0
    status = check["prev_post_status"]
    assert status["available"] is True
    assert status["complete"] is True
    assert status["reason"] == storage.POST_HOLDINGS_REASON_OK
    assert status["meta_present"] is True
    assert status["legacy"] is False
    assert status["path"].endswith(storage.POST_HOLDINGS_FILENAME)


def test_confirmed_empty_book_reports_complete_zero_symbols(service, temp_storage):
    """`{}` is a legal all-cash book, not a missing one -- `complete: true` says so."""
    _previous_run(temp_storage, book={}, meta=_complete_meta(0))

    check = _check(service, {"600519.SH": 100})

    status = check["prev_post_status"]
    assert status["available"] is True
    assert status["complete"] is True
    assert status["reason"] == storage.POST_HOLDINGS_REASON_OK


# ---------------------------------------------------------------------------
# 3. A legacy archive (no sidecar) keeps pre-D5 semantics but is labelled
# ---------------------------------------------------------------------------


def test_legacy_archive_without_sidecar_is_labelled_not_rejected(service, temp_storage):
    _previous_run(temp_storage, book={"600519.SH": 100}, meta=None)

    check = _check(service, {"600519.SH": 100})

    assert check["prev_post_available"] is True
    status = check["prev_post_status"]
    assert status["available"] is True
    assert status["complete"] is None, "unknown is NOT incomplete"
    assert status["reason"] == storage.POST_HOLDINGS_REASON_LEGACY
    assert status["legacy"] is True
    assert status["meta_present"] is False


# ---------------------------------------------------------------------------
# 4. An INCOMPLETE sidecar: no reference, with the reason that explains it
# ---------------------------------------------------------------------------


def test_incomplete_sidecar_is_not_a_reference_and_says_why(service, temp_storage):
    _previous_run(
        temp_storage,
        book={"600519.SH": 100},
        meta={"as_of": PREV_DATE, "complete": False, "n_symbols": 0},
    )

    check = _check(service, {"600519.SH": 100})

    # The unusable file is not used as a reference (D5 contract).
    assert check["prev_post_available"] is False
    status = check["prev_post_status"]
    assert status["available"] is False
    assert status["complete"] is False
    assert status["reason"] == storage.POST_HOLDINGS_REASON_INCOMPLETE
    assert status["meta_present"] is True
