"""`--intraday-publish-after` on the NETTED multi-strategy runner (`scripts/run_live_multi_strategy.py`).

Why this suite exists
---------------------
Stage 1 (`scripts/check_live_portfolio_health.py`) and Stage 2 (`scripts/run_live_strategy.py`)
both expose `--intraday-publish-after`: the market-local wall clock after which the as-of
session's 5m bars are assumed published (BaoStock: ~20:00, a live observation). It does NOT
move the run window -- it only lets the runner label a 5m layer that does not reach the session
as `pending_publication` (wait and rerun) instead of `missing` (sync defect).

DISCLOSED PREMISE CORRECTION: this runner spawns NO child process. It does not assemble an
argv for `run_live_strategy.py`; it imports and calls the daily loaders/strategies IN PROCESS
and its 5m layer is config-driven (module docstring, §4.50.0). So there is nothing to
"forward to a child": the flag is applied to THIS round's own 5m observation block
(`observability.intraday_5m.publication_state`). (The real argv-forming parent is
`web/livetrading/execution_service.py::run_multi_strategy_netted_deploy`, which does not pass
this flag yet -- reported, not changed here.)

Backward compatibility contract asserted below: UNSET (`default=None`) => no publication
assessment is computed at all and the persisted block keeps exactly the keys it had before
this flag existed. Stage 2's `20:00` default is deliberately NOT imposed on this path.

100% offline: no market data, no network; the 5m loader/enricher are doubled, and every clock
read is the injected `now`.
"""

import datetime as dt
import os
import sys

import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import run_live_multi_strategy as multi  # noqa: E402

_UTC = dt.timezone.utc
#: 2026-10-23 is a Friday; 07:30 UTC == 15:30 Beijing, i.e. AFTER the daily close+grace but
#: BEFORE the ~20:00 5m publication habit -- the exact window this axis exists for.
_AS_OF = "2026-10-23"
_NOW = dt.datetime(2026, 10, 23, 7, 30, tzinfo=_UTC)
_SYMBOLS = ["600000.SH", "601857.SH"]

#: Keys the block has always carried (pre-flag shape). Used to prove the unset path did not
#: gain a key. `warning` is CONDITIONAL on the block (`requested and not enabled`), so the
#: assertion is "legacy keys present, nothing beyond legacy + warning".
_LEGACY_BLOCK_KEYS = {
    "requested",
    "enabled",
    "rows_5m",
    "as_of_date",
    "round_id",
    "generated_at",
    "symbols_with_rows",
    "symbols_unavailable",
    "reason",
    "source",
}
_CONDITIONAL_BLOCK_KEYS = {"warning"}


def _assert_pre_flag_shape(block):
    assert _LEGACY_BLOCK_KEYS <= set(block)
    assert set(block) <= (_LEGACY_BLOCK_KEYS | _CONDITIONAL_BLOCK_KEYS)
    assert "publication_state" not in block


def _daily_frame():
    idx = pd.DatetimeIndex([pd.Timestamp(_AS_OF)])
    return pd.DataFrame(
        {"Open": [10.0], "High": [10.5], "Low": [9.8], "Close": [10.2], "Volume": [1000.0]},
        index=idx,
    )


@pytest.fixture
def _stubbed_5m(monkeypatch):
    """Double the 5m load/enrich seams so nothing touches a provider or a DuckDB cache.

    An EMPTY 5m panel is the interesting case: it is precisely what a 15:30 run sees before
    BaoStock publishes, and therefore what the publication axis has to classify.
    """
    monkeypatch.setattr(multi, "load_intraday_5m_panel", lambda *a, **k: {})
    monkeypatch.setattr(
        multi,
        "enrich_intraday_5m_panel",
        lambda universe, universe_5m, **k: dict(universe),
    )


def _run(symbols=None, **kwargs):
    universe = {s: _daily_frame() for s in (symbols or _SYMBOLS)}
    return multi.apply_intraday_5m_features(
        universe,
        symbols=list(symbols or _SYMBOLS),
        start_date="2026-10-01",
        as_of_date=_AS_OF,
        data_provider=object(),
        cutoff_time="14:15",
        provider_label="synthetic",
        round_id="2026-10-23T07:30:00+00:00",
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
def test_arg_defaults_to_none(monkeypatch):
    """UNSET must stay `None` -- not Stage 2's `20:00`, and not an implied assessment."""
    monkeypatch.setattr(
        sys, "argv", ["run_live_multi_strategy.py", "--data-provider", "synthetic"]
    )
    args = multi.parse_args()
    assert args.intraday_publish_after is None


def test_arg_accepts_hh_mm(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_live_multi_strategy.py",
            "--data-provider",
            "synthetic",
            "--intraday-publish-after",
            "21:30",
        ],
    )
    args = multi.parse_args()
    assert args.intraday_publish_after == "21:30"


# --------------------------------------------------------------------------- #
# Block assembly: the "forwarding" seam on this runner (it has no child process)
# --------------------------------------------------------------------------- #
def test_block_omits_publication_state_when_not_assessed():
    block = multi.build_intraday_5m_block(
        requested=True,
        frames={"rows_5m": 0, "symbols_with_rows": [], "symbols_unavailable": list(_SYMBOLS)},
        as_of_date=_AS_OF,
        usable_symbols=[],
        reason="fake",
        round_id="rid",
    )
    _assert_pre_flag_shape(block)


def test_block_carries_publication_state_when_assessed():
    publication = {"status": "pending_publication", "expected_after": "21:30"}
    block = multi.build_intraday_5m_block(
        requested=True,
        frames={"rows_5m": 0, "symbols_with_rows": [], "symbols_unavailable": list(_SYMBOLS)},
        as_of_date=_AS_OF,
        usable_symbols=[],
        reason="fake",
        round_id="rid",
        publication=publication,
    )
    assert block["publication_state"] == publication


# --------------------------------------------------------------------------- #
# End-to-end (in process): 21:30 given => pending_published; unset => no key at all
# --------------------------------------------------------------------------- #
def test_unset_flag_leaves_block_byte_identical(_stubbed_5m):
    _enriched, block = _run(now=_NOW)
    _assert_pre_flag_shape(block)
    # The empty-panel honesty path is unchanged: requested but not enabled, with a warning.
    assert block["requested"] is True
    assert block["enabled"] is False
    assert block["rows_5m"] == 0
    assert "warning" in block


def test_named_publish_time_labels_pending_publication(_stubbed_5m):
    """15:30 Beijing with `--intraday-publish-after 21:30` => wait-and-rerun, not a defect."""
    _enriched, block = _run(publish_after="21:30", now=_NOW)
    state = block["publication_state"]
    assert state["status"] == "pending_publication"
    assert state["degraded_reason_code"] == "intraday_5m_pending_publication"
    assert state["expected_after"] == "21:30"
    # Same block otherwise; the axis is additive, never a re-interpretation of rows/enabled.
    assert block["requested"] is True
    assert block["enabled"] is False
    assert block["rows_5m"] == 0


def test_named_publish_time_already_passed_labels_missing(_stubbed_5m):
    """15:30 Beijing with a 09:00 publication time => the bars should be there: `missing`."""
    _enriched, block = _run(publish_after="09:00", now=_NOW)
    state = block["publication_state"]
    assert state["status"] == "missing"
    assert state["degraded_reason_code"] == "intraday_5m_stale_window"
    assert state["expected_after"] == "09:00"


def test_assess_helper_short_circuits_when_unset():
    """`None` must not read the clock at all (that is what keeps the archive unchanged)."""
    assert (
        multi.assess_round_publication(
            {},
            as_of_date=_AS_OF,
            market="china",
            now=_NOW,
            publish_after=None,
        )
        is None
    )
