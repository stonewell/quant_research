"""`NV10` -- a deploy round may not end with a book but no attestation.

FORENSIC CONTEXT (the hole this suite closes)
---------------------------------------------
`ExecutionService.run_live_deploy` re-derived the planned post-trade book from the child's
ticket and wrote `post_holdings.json` -- and NEVER its `post_holdings.meta.json` sidecar.
On disk that is byte-identical to a pre-D5 LEGACY round (`book present, no sidecar`), which
`classify_post_holdings_meta` deliberately treats as usable for backward compatibility.  So
a round whose sidecar was lost could not be told apart from old data, and the round was
still reported `deploy_status=SUCCESS`: the 10 `POST_HOLDINGS_MISSING` findings in the
2026-11-16 archive are all the LEGACY case (every one of them predates commit d0e8c93),
which is exactly why the two states must stop looking the same.

WHAT IS PINNED HERE
-------------------
* a round whose child shipped a ticket but left no book gets BOTH files, `complete: true`,
  and `summary.json::post_holdings_written = true`;
* a round whose child already attested its own pair is left byte-unchanged
  (`post_holdings_reason = post_holdings_meta_already_complete`, `source` preserved), so the
  child's `source: "replay"` and `generated_at` are never overwritten;
* a FAILED attestation fails the ROUND (`deploy_status = FAILED`,
  `error_kind = PostHoldingsAttestationError`) instead of a `[WARN]` print;
* an UNDERIVABLE book (ticket with no `target_shares`) is reported as
  `ticket_without_target_shares` and does NOT fail the round -- those orders are correct,
  and the state is visible to the integrity checker instead of being guessed.

100% offline: the child process is faked (it only writes the artifacts), no network, no
market data.
"""

import json
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(os.path.dirname(_WEB_DIR))
for _path in (_WEB_DIR, _REPO_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import execution_service as _execution_service  # noqa: E402
import storage as _storage  # noqa: E402
from execution_service import ExecutionService  # noqa: E402
from storage import (  # noqa: E402
    POST_HOLDINGS_FILENAME,
    POST_HOLDINGS_META_FILENAME,
    StorageManager,
)

POST = POST_HOLDINGS_FILENAME
META = POST_HOLDINGS_META_FILENAME
_STRATEGY = "strat_x"
_UNIVERSE = "univ_y"


@pytest.fixture
def temp_storage(tmp_path):
    for name in ("data", "archive", "cache", "settings"):
        (tmp_path / name).mkdir(exist_ok=True)
    return StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(tmp_path / "data"),
        archive_dir=str(tmp_path / "archive"),
        cache_dir=str(tmp_path / "cache"),
        settings_dir=str(tmp_path / "settings"),
    )


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _dummy_strategy_file(tmp_path, name="nv10_strat.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"template_name": "dummy", "params": {}}), encoding="utf-8")
    return str(path)


def _ticket(target_shares=100):
    """The child's ticket: one BUY row, with or without `target_shares`."""
    row = {"symbol": "600519.SH", "name": "贵州茅台", "action": "BUY", "current_shares": 0}
    if target_shares is not None:
        row["target_shares"] = target_shares
    return {
        "ticket_schema": "lv2",
        "as_of": "2026-10-30",
        "execution_date": "2026-11-02",
        "orders": [row],
    }


def _patch_child(monkeypatch, *, ticket=None):
    """Fake the Stage-2 child: it writes a ticket and nothing else (no book, no sidecar)."""
    calls = []

    def _run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        out_dir = cmd[cmd.index("--output-dir") + 1]
        os.makedirs(out_dir, exist_ok=True)
        if ticket is not None:
            _write(os.path.join(out_dir, "live_trading_ticket.json"), ticket)

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr(_execution_service.subprocess, "run", _run)
    return calls


def _deploy_kwargs(tmp_path, **extra):
    kwargs = dict(
        strategy_file=_dummy_strategy_file(tmp_path),
        custom_symbols=["600519.SH"],
        as_of_date="2026-10-30",
        portfolio_value=100000.0,
        strategy_key=_STRATEGY,
        universe_key=_UNIVERSE,
        nav_override=True,
        data_provider="synthetic",
        current_holdings={"600519.SH": 100},
    )
    kwargs.update(extra)
    return kwargs


def _summary_of(storage, argv):
    run_dir = argv[argv.index("--output-dir") + 1]
    return run_dir, _read(os.path.join(run_dir, "summary.json"))


def test_round_whose_child_left_no_book_gets_both_files_and_says_so(
    temp_storage, tmp_path, monkeypatch
):
    """The parent fills the gap: book + `complete: true` sidecar + summary flag.

    re-break: restore the old `self.storage._save_json(.../post_holdings.json, ...)` line
    (book only) -- the sidecar assert fails and the round would be indistinguishable from a
    legacy round on disk.
    """
    calls = _patch_child(monkeypatch, ticket=_ticket())

    ExecutionService(temp_storage).run_live_deploy(**_deploy_kwargs(tmp_path))

    run_dir, summary = _summary_of(temp_storage, calls[0])
    assert _read(os.path.join(run_dir, POST)) == {"600519.SH": 100}
    meta = _read(os.path.join(run_dir, META))
    assert meta["complete"] is True
    assert meta["n_symbols"] == 1
    assert meta["as_of"] == "2026-10-30"
    assert summary["post_holdings_written"] is True
    assert summary["post_holdings_reason"] == "post_holdings_meta_complete"
    assert summary["post_holdings_meta_source"] == "ticket"
    assert summary["deploy_status"] == "SUCCESS"


def test_a_child_attested_pair_is_left_byte_unchanged(temp_storage, tmp_path, monkeypatch):
    """The child owns its own provenance (`source: replay`, `generated_at`) -- never rewrite it.

    re-break: make `ensure_post_holdings_attestation` unconditional (always write the pair) --
    a REPLAY round's sidecar would be relabelled `ticket`, i.e. a reconstruction would start
    claiming to be a fill plan.
    """
    written_by_child = {}
    calls = []

    def _run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        out_dir = cmd[cmd.index("--output-dir") + 1]
        os.makedirs(out_dir, exist_ok=True)
        _write(os.path.join(out_dir, "live_trading_ticket.json"), _ticket())
        _write(os.path.join(out_dir, POST), {"600519.SH": 100})
        _write(
            os.path.join(out_dir, META),
            {
                "as_of": "2026-10-30",
                "complete": True,
                "n_symbols": 1,
                "generated_at": "2026-10-30T14:00:00-08:00",
                "source": "replay",
            },
        )
        for name in (POST, META):
            written_by_child[name] = open(os.path.join(out_dir, name), "rb").read()

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr(_execution_service.subprocess, "run", _run)

    ExecutionService(temp_storage).run_live_deploy(**_deploy_kwargs(tmp_path))

    run_dir, summary = _summary_of(temp_storage, calls[0])
    for name in (POST, META):
        assert open(os.path.join(run_dir, name), "rb").read() == written_by_child[name]
    meta = _read(os.path.join(run_dir, META))
    assert meta["source"] == "replay", "the child's own provenance must survive"
    assert meta["generated_at"] == "2026-10-30T14:00:00-08:00"
    assert summary["post_holdings_written"] is True
    assert summary["post_holdings_reason"] == "post_holdings_meta_already_complete"
    assert summary["post_holdings_meta_source"] == "replay"


def test_a_failed_attestation_fails_the_round(temp_storage, tmp_path, monkeypatch):
    """FAIL CLOSED: a lost book is a `FAILED` round with a named cause, not a `[WARN]`.

    re-break: drop the `success = False` / `failure = {...}` block from `run_live_deploy` --
    the round would report SUCCESS while the next session has no attested book to seed from.
    """
    calls = _patch_child(monkeypatch, ticket=_ticket())

    def _boom(path, payload):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(_storage, "_atomic_write_json", _boom)

    ExecutionService(temp_storage).run_live_deploy(**_deploy_kwargs(tmp_path))

    run_dir, summary = _summary_of(temp_storage, calls[0])
    assert summary["deploy_status"] == "FAILED"
    assert summary["error_kind"] == "PostHoldingsAttestationError"
    assert "post_holdings" in summary["error_headline"]
    assert "No space left" in summary["error_detail"]
    assert summary["post_holdings_written"] is False
    assert summary["post_holdings_reason"] == "post_holdings_write_failed"
    # The ticket itself is untouched: the operator still has the order instructions.
    assert os.path.exists(os.path.join(run_dir, "live_trading_ticket.json"))


def test_an_underivable_book_is_reported_but_does_not_fail_the_round(
    temp_storage, tmp_path, monkeypatch
):
    """No `target_shares` -> UNDERIVABLE, not lost: the flag says why, the round stands.

    This is the documented boundary of the fail-closed rule (see NV10): a pre-LV2 list
    ticket cannot express a book, so failing the round would punish correct orders. The
    summary still records the state so no reader has to guess.
    """
    calls = _patch_child(monkeypatch, ticket=_ticket(target_shares=None))

    ExecutionService(temp_storage).run_live_deploy(**_deploy_kwargs(tmp_path))

    run_dir, summary = _summary_of(temp_storage, calls[0])
    assert summary["post_holdings_written"] is False
    assert summary["post_holdings_reason"] == "ticket_without_target_shares"
    assert summary["deploy_status"] == "SUCCESS"
    assert not os.path.exists(os.path.join(run_dir, META))
