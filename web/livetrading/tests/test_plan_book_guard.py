"""SSOT item 10: the 「💾 保存持仓」 button must not persist a PLAN as a verified book.

FORENSIC CONTEXT (witnessed in production, 2026-11-16)
------------------------------------------------------
After a successful Stage 2 the UI repaints the holdings table from the ticket's PROJECTED
post-trade book (``post_holdings.json``) and shows a notice that the table is a plan, not a
fill receipt.  For 2026-11-16 that plan was ``600660.SH 300 / 601919.SH 900 /
601728.SH 2300 / 600900.SH 500`` -- two symbols the account never owned.

``POST /api/holdings`` then wrote whatever the table contained into
``operator_holdings.json`` with ``source: operator_manual_correction``, i.e. "a human
asserted this book against broker fills".  It had no guard at all, while the DEPLOY path
already refuses the same shape (``execution_service.UnconfirmedPlanAsInputError``).  One
click was therefore enough to convert the plan into a verified book, and because
``operator_holdings.json`` outranks the plan on both the display and the seed path, every
later round sized against a fiction.

The gate pinned here (``storage.UnconfirmedPlanBookSaveError`` -> HTTP 409) refuses a
NON-EMPTY book identical, order-insensitively and share-for-share, to the same date's
latest plan, unless the caller passes ``confirm_plan_book: true``.  This file pins:

1. the refusal itself, and that ``operator_holdings.json`` is NOT written / unchanged;
2. the ``confirm_plan_book`` escape hatch;
3. a one-share difference (the ordinary case: a real fill that did not match exactly);
4. a date with no plan at all (a brand-new session);
5. which instance is the reference -- the date's LATEST plan, with a fallback to the
   newest instance that actually HAS ``post_holdings.json``;
6. a superseded instance is never used as the reference when a newer plan exists;
7. the refusal names the date AND the instance whose plan matched;
8. an empty / all-zero book is the pre-existing empty-book path's business, never this one.

Everything is offline: ``temp_workspace`` keeps every writable path under the OS temp root
(``conftest.py``'s autouse hermetic fixture re-points the storage directories too) and no
test touches the real archive, the DuckDB cache, or the network.
"""

import json
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _p in (_WEB_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from execution_service import book_is_empty
from server import create_app
from storage import (
    OPERATOR_HOLDINGS_FILENAME,
    OPERATOR_MANUAL_CORRECTION_SOURCE,
    PLAN_BOOK_CONFIRM_FIELD,
    StorageManager,
    UnconfirmedPlanBookSaveError,
)

_STRATEGY = "chan_four_state_blend"
_UNIVERSE = "china_a_shares"
_DATE = "2026-11-16"

#: The real 2026-11-16 plan (two of these symbols the account never owned).
_PLAN_BOOK: Dict[str, int] = {
    "600660.SH": 300,
    "601919.SH": 900,
    "601728.SH": 2300,
    "600900.SH": 500,
}

#: The two instances the real date carried, OLD -> NEW.
_OLD_INSTANCE = "124508"
_NEW_INSTANCE = "142959"


@pytest.fixture
def temp_workspace():
    """Hermetic workspace (the package-wide autouse fixture is defence in depth)."""
    temp_dir = tempfile.mkdtemp()
    data_dir = os.path.join(temp_dir, "data")
    archive_dir = os.path.join(temp_dir, "runs")
    cache_dir = os.path.join(temp_dir, "duckdb_cache")
    settings_dir = os.path.join(temp_dir, "settings")
    for directory in (data_dir, archive_dir, cache_dir, settings_dir):
        os.makedirs(directory, exist_ok=True)

    storage = StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=data_dir,
        archive_dir=archive_dir,
        cache_dir=cache_dir,
        settings_dir=settings_dir,
    )
    yield storage, temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def client(temp_workspace):
    storage, _ = temp_workspace
    return TestClient(create_app(storage))


def _write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def _seed_instance(
    storage: StorageManager,
    *,
    date_str: str = _DATE,
    instance: Optional[str] = None,
    post_book: Optional[Dict[str, Any]] = None,
    timestamp: Optional[str] = None,
    strategy_key: str = _STRATEGY,
    universe_key: str = _UNIVERSE,
) -> str:
    """Create one run dir; `post_book=None` leaves it WITHOUT `post_holdings.json`."""
    run_dir = os.path.join(storage.archive_dir, strategy_key, universe_key, date_str)
    if instance:
        run_dir = os.path.join(run_dir, instance)
    os.makedirs(run_dir, exist_ok=True)
    if timestamp:
        _write_json(
            os.path.join(run_dir, "summary.json"),
            {"date": date_str, "timestamp": timestamp},
        )
    if post_book is not None:
        _write_json(os.path.join(run_dir, "post_holdings.json"), post_book)
    return run_dir


def _date_dir(storage: StorageManager, date_str: str = _DATE) -> str:
    return os.path.join(storage.archive_dir, _STRATEGY, _UNIVERSE, date_str)


def _save_payload(
    holdings: Dict[str, Any],
    *,
    date_str: str = _DATE,
    confirm: bool = False,
    include_selection: bool = True,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"holdings": holdings, "date": date_str}
    if include_selection:
        payload["strategy_key"] = _STRATEGY
        payload["universe_key"] = _UNIVERSE
    if confirm:
        payload[PLAN_BOOK_CONFIRM_FIELD] = True
    return payload


# ---------------------------------------------------------------------------
# 1. The refusal itself
# ---------------------------------------------------------------------------
def test_plan_shaped_save_is_refused_and_writes_nothing(client, temp_workspace):
    storage, _ = temp_workspace
    _seed_instance(storage, instance=_NEW_INSTANCE, post_book=_PLAN_BOOK)

    res = client.post("/api/holdings", json=_save_payload(dict(reversed(list(_PLAN_BOOK.items())))))

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert _DATE in detail and _NEW_INSTANCE in detail

    # A refused save must leave the archive byte-identical: no holdings.json, no sidecar.
    date_dir = _date_dir(storage)
    assert not os.path.exists(os.path.join(date_dir, "holdings.json"))
    assert not os.path.exists(os.path.join(date_dir, OPERATOR_HOLDINGS_FILENAME))
    assert sorted(os.listdir(date_dir)) == [_NEW_INSTANCE]


def test_plan_shaped_save_raises_the_dedicated_error_at_the_storage_layer(temp_workspace):
    storage, _ = temp_workspace
    _seed_instance(storage, instance=_NEW_INSTANCE, post_book=_PLAN_BOOK)

    with pytest.raises(UnconfirmedPlanBookSaveError) as excinfo:
        storage.save_holdings(
            _PLAN_BOOK,
            date_str=_DATE,
            strategy_key=_STRATEGY,
            universe_key=_UNIVERSE,
        )

    assert excinfo.value.date_str == _DATE
    assert excinfo.value.plan_run_dir == os.path.join(_date_dir(storage), _NEW_INSTANCE)
    # The deploy-side gate keeps its OWN error type; this one is a save-path refusal.
    assert type(excinfo.value).__name__ == "UnconfirmedPlanBookSaveError"


# ---------------------------------------------------------------------------
# 2. The escape hatch
# ---------------------------------------------------------------------------
def test_plan_shaped_save_succeeds_with_confirm_flag(client, temp_workspace):
    storage, _ = temp_workspace
    _seed_instance(storage, instance=_NEW_INSTANCE, post_book=_PLAN_BOOK)

    res = client.post("/api/holdings", json=_save_payload(_PLAN_BOOK, confirm=True))

    assert res.status_code == 200
    # The success payload is unchanged field-for-field (only the request gained a field).
    assert res.json() == {
        "status": "success",
        "message": "Holdings saved successfully",
        "holdings": _PLAN_BOOK,
    }
    date_dir = _date_dir(storage)
    sidecar_path = os.path.join(date_dir, OPERATOR_HOLDINGS_FILENAME)
    assert os.path.isfile(os.path.join(date_dir, "holdings.json"))
    assert os.path.isfile(sidecar_path)
    sidecar = json.load(open(sidecar_path, encoding="utf-8"))
    assert sidecar["source"] == OPERATOR_MANUAL_CORRECTION_SOURCE
    assert sidecar["holdings"] == _PLAN_BOOK


# ---------------------------------------------------------------------------
# 3. A real (non-matching) correction is never blocked
# ---------------------------------------------------------------------------
def test_one_share_difference_saves_without_flag(client, temp_workspace):
    storage, _ = temp_workspace
    _seed_instance(storage, instance=_NEW_INSTANCE, post_book=_PLAN_BOOK)
    corrected = dict(_PLAN_BOOK)
    corrected["601728.SH"] = int(corrected["601728.SH"]) - 100  # partial fill

    res = client.post("/api/holdings", json=_save_payload(corrected))

    assert res.status_code == 200
    assert os.path.isfile(os.path.join(_date_dir(storage), OPERATOR_HOLDINGS_FILENAME))


# ---------------------------------------------------------------------------
# 4. A date with no plan at all (brand-new session)
# ---------------------------------------------------------------------------
def test_date_without_any_plan_saves_without_flag(client, temp_workspace):
    storage, _ = temp_workspace
    assert not os.path.exists(_date_dir(storage))

    res = client.post("/api/holdings", json=_save_payload(_PLAN_BOOK))

    assert res.status_code == 200
    assert os.path.isfile(os.path.join(_date_dir(storage), OPERATOR_HOLDINGS_FILENAME))


# ---------------------------------------------------------------------------
# 5. Which instance is the reference
# ---------------------------------------------------------------------------
def test_newest_instance_is_the_only_reference(client, temp_workspace):
    storage, _ = temp_workspace
    older = dict(_PLAN_BOOK)
    older["600660.SH"] = 100
    _seed_instance(
        storage,
        instance=_OLD_INSTANCE,
        post_book=older,
        timestamp="2026-11-16T12:45:08+00:00",
    )
    _seed_instance(
        storage,
        instance=_NEW_INSTANCE,
        post_book=_PLAN_BOOK,
        timestamp="2026-11-16T14:29:59+00:00",
    )

    res = client.post("/api/holdings", json=_save_payload(_PLAN_BOOK))

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert _NEW_INSTANCE in detail
    assert _OLD_INSTANCE not in detail, "a superseded plan must not be named"


def test_falls_back_to_newest_instance_that_has_a_plan(client, temp_workspace):
    storage, _ = temp_workspace
    _seed_instance(
        storage,
        instance=_OLD_INSTANCE,
        post_book=_PLAN_BOOK,
        timestamp="2026-11-16T12:45:08+00:00",
    )
    # Newest round exists but recorded NO plan (e.g. Stage 1 only) -> fall back.
    _seed_instance(
        storage,
        instance=_NEW_INSTANCE,
        post_book=None,
        timestamp="2026-11-16T14:29:59+00:00",
    )

    res = client.post("/api/holdings", json=_save_payload(_PLAN_BOOK))

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert _OLD_INSTANCE in detail
    assert _NEW_INSTANCE not in detail


# ---------------------------------------------------------------------------
# 6. A superseded plan is never the reference when a newer one exists
# ---------------------------------------------------------------------------
def test_superseded_instance_book_is_not_refused(client, temp_workspace):
    storage, _ = temp_workspace
    superseded = dict(_PLAN_BOOK)
    superseded["601919.SH"] = 800  # the OLD round's projection, no longer painted
    _seed_instance(
        storage,
        instance=_OLD_INSTANCE,
        post_book=superseded,
        timestamp="2026-11-16T12:45:08+00:00",
    )
    _seed_instance(
        storage,
        instance=_NEW_INSTANCE,
        post_book=_PLAN_BOOK,
        timestamp="2026-11-16T14:29:59+00:00",
    )

    res = client.post("/api/holdings", json=_save_payload(superseded))

    assert res.status_code == 200, "the superseded plan is not this date's plan any more"
    assert os.path.isfile(os.path.join(_date_dir(storage), OPERATOR_HOLDINGS_FILENAME))


# ---------------------------------------------------------------------------
# 7. The refusal names the date and the instance
# ---------------------------------------------------------------------------
def test_refusal_message_names_date_and_instance(client, temp_workspace):
    storage, _ = temp_workspace
    _seed_instance(storage, instance=_NEW_INSTANCE, post_book=_PLAN_BOOK)

    res = client.post("/api/holdings", json=_save_payload(_PLAN_BOOK))

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert f"date={_DATE!r}" in detail
    assert f"instance={_NEW_INSTANCE!r}" in detail
    assert PLAN_BOOK_CONFIRM_FIELD in detail
    # The operator is told, in both languages, that a plan is not a fill receipt.
    assert "不是成交回执" in detail
    assert "not a fill receipt" in detail


# ---------------------------------------------------------------------------
# 8. Empty books belong to the pre-existing empty-book path, not this gate
# ---------------------------------------------------------------------------
def test_empty_book_never_hits_the_plan_book_guard(client, temp_workspace):
    storage, _ = temp_workspace
    _seed_instance(storage, instance=_NEW_INSTANCE, post_book=_PLAN_BOOK)

    # The DEPLOY path already classifies this shape (`confirm_empty_book` is its licence);
    # this save-path gate must leave it alone.
    assert book_is_empty({}) is True

    for empty_book in ({}, {"600660.SH": 0}):
        res = client.post("/api/holdings", json=_save_payload(empty_book))
        assert res.status_code == 200, empty_book
        assert "detail" not in res.json()

    # ... and the storage layer agrees: no `UnconfirmedPlanBookSaveError` for an empty book.
    storage.save_holdings({}, date_str=_DATE, strategy_key=_STRATEGY, universe_key=_UNIVERSE)


# ---------------------------------------------------------------------------
# Guard integrity: a cash row, or an omitted selection, must not dodge it
# ---------------------------------------------------------------------------
def test_cash_proxy_row_does_not_dodge_the_guard(client, temp_workspace):
    storage, _ = temp_workspace
    _seed_instance(storage, instance=_NEW_INSTANCE, post_book=_PLAN_BOOK)
    with_cash = dict(_PLAN_BOOK)
    with_cash["BIL"] = 1234  # parked cash is the cash leg, never part of the plan

    res = client.post("/api/holdings", json=_save_payload(with_cash))

    assert res.status_code == 409


def test_omitted_selection_keys_still_find_the_dates_plan(client, temp_workspace):
    storage, _ = temp_workspace
    _seed_instance(storage, instance=_NEW_INSTANCE, post_book=_PLAN_BOOK)

    res = client.post("/api/holdings", json=_save_payload(_PLAN_BOOK, include_selection=False))

    assert res.status_code == 409, "dropping strategy/universe must not be a bypass"


def test_omitted_selection_keys_check_every_selections_plan(client, temp_workspace):
    """A selection-less save is compared against EVERY selection's plan for that date.

    Picking one selection's plan as "the" reference would let a table that matches the
    OTHER selection's projection through, which is the same fiction under a different name.
    """
    storage, _ = temp_workspace
    other_book = {"000858.SZ": 700, "600036.SH": 200}
    _seed_instance(storage, instance=_NEW_INSTANCE, post_book=_PLAN_BOOK)
    _seed_instance(
        storage,
        instance=_NEW_INSTANCE,
        post_book=other_book,
        strategy_key="chan_risk_managed_blend",
        universe_key=_UNIVERSE,
    )

    res = client.post("/api/holdings", json=_save_payload(other_book, include_selection=False))

    assert res.status_code == 409


def test_pinned_run_dir_save_is_not_guarded(temp_workspace):
    """The DEPLOY arm (`run_dir` pinned) keeps its own gate; this one never fires there."""
    storage, _ = temp_workspace
    run_dir = _seed_instance(storage, instance=_NEW_INSTANCE, post_book=_PLAN_BOOK)

    storage.save_holdings(
        _PLAN_BOOK,
        date_str=_DATE,
        strategy_key=_STRATEGY,
        universe_key=_UNIVERSE,
        run_dir=run_dir,
    )

    assert os.path.isfile(os.path.join(run_dir, "holdings.json"))
    assert not os.path.exists(os.path.join(run_dir, OPERATOR_HOLDINGS_FILENAME))
