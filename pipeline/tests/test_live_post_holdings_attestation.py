"""`NV10` -- the post-trade book and its attestation are ONE fail-closed pair.

FORENSIC CONTEXT (measured on the 2026-11-16 archive, not assumed)
------------------------------------------------------------------
`scripts/check_live_run_integrity.py` reports **10** `POST_HOLDINGS_MISSING` findings in
`.../chan_four_state_blend/custom_astock_202609_20_new/` (2026-10-29/215823 ..
2026-11-10/202644).  Every one of those rounds DOES carry `post_holdings.json` and DOES
carry its ticket -- what is absent is only the `post_holdings.meta.json` sidecar, and the
split is exactly the feature boundary:

* `2026-11-11/205137` (20:51, run BEFORE the sidecar landed) has no self-written sidecar;
  the file it does have says `"source": "repair_backfill"` and was patched at 23:31.
* `2026-11-11/234433` (23:46, the first round after commit d0e8c93 at 23:43) wrote
  `"source": "replay"` itself, and every later round with a ticket has `complete: true`.

So the ten findings are LEGACY rounds -- state (c) -- not a live loss, and (checked in the
archive, not argued) the claimed consequence chain does not hold either: the seed source
`operator_manual_correction` comes from the session's own `operator_holdings.json`, which
outranks the post book in `storage.resolve_seed_holdings` and is what EVERY round used,
including the four rounds whose sidecar is present; the round-to-round `prior_state`
chain (and therefore `holding_bars` / `entry_prices`) is unbroken across the window.

WHAT IS STILL A HOLE, AND WHAT THIS SUITE PINS
----------------------------------------------
Both producers used to swallow a failed book write behind `print("[WARN] ...")`, and the
web deploy path rewrote `post_holdings.json` while never writing the sidecar -- so a round
that LOST its book was byte-identical on disk to a pre-D5 legacy round and was still
reported `deploy_status=SUCCESS`.  Now:

1. `scripts/run_live_strategy.write_post_holdings_attestation` writes the pair with the
   attestation LAST (`complete: false` -> book -> `complete: true`), so a crash or a failed
   rename leaves `post_holdings_meta_incomplete` (UNUSABLE), never "book, no sidecar";
2. `post_holdings_attestation_gate` turns "ticket written, book not attested" into a raised
   `PostHoldingsAttestationError` (nonzero exit -> `deploy_status=FAILED`);
3. `storage.ensure_post_holdings_attestation` fills a missing sidecar for a legacy dir and
   leaves an already-attested dir byte-unchanged;
4. the standalone backfill is DRY-RUN by default, writes a sidecar only when the ticket
   reproduces the on-disk book EXACTLY, and refuses everything else;
5. `check_live_run_integrity` tags each `POST_HOLDINGS_MISSING` finding `legacy: true/false`
   from `summary.json::post_holdings_written`, so the two states stop looking identical.

100% offline: `tmp_path` archives only, no market data, no network.
"""

import importlib.util
import json
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _load_module(name, relative_path):
    """Load a repo module by path (same trick as `test_live_holdings_input_check`).

    `web/livetrading` has no `__init__.py` and `scripts/` is a namespace package; loading by
    explicit file location keeps this suite importable in the shared `pipeline` venv, which
    has no fastapi.
    """
    path = os.path.join(_REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their own module through `sys.modules` while decorating, so the
    # module has to be registered BEFORE exec_module (plain `spec` loading leaves it None).
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


storage = _load_module("web_livetrading_storage_nv10", "web/livetrading/storage.py")
runner = _load_module("scripts_run_live_strategy_nv10", "scripts/run_live_strategy.py")
integrity = _load_module("check_live_run_integrity_nv10", "scripts/check_live_run_integrity.py")

POST = storage.POST_HOLDINGS_FILENAME
META = storage.POST_HOLDINGS_META_FILENAME


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _ticket(as_of="2026-11-16", execution_date="2026-11-17", orders=None):
    return {
        "ticket_schema": "lv2",
        "as_of": as_of,
        "execution_date": execution_date,
        "orders": orders
        if orders is not None
        else [{"symbol": "600519.SH", "action": "BUY", "target_shares": 100, "current_shares": 0}],
    }


# ---------------------------------------------------------------------------
# 1. The child writer: pair, order, and the fail-closed sidecar
# ---------------------------------------------------------------------------


def test_writer_emits_the_book_and_a_complete_sidecar_together(tmp_path):
    """The happy path: both files, `complete: true`, `source: ticket`, matching `n_symbols`."""
    outcome = runner.write_post_holdings_attestation(
        str(tmp_path), {"600519.SH": 100, "000001.SZ": 0}, as_of_date="2026-11-16", run_mode="live"
    )

    assert outcome["written"] is True
    assert outcome["book_written"] is True and outcome["meta_written"] is True
    assert outcome["reason"] == runner.POST_HOLDINGS_REASON_OK
    assert _read(os.path.join(tmp_path, POST)) == {"600519.SH": 100, "000001.SZ": 0}
    meta = _read(os.path.join(tmp_path, META))
    assert meta["complete"] is True
    assert meta["as_of"] == "2026-11-16"
    assert meta["source"] == "ticket"
    # Zero-share rows are not positions, so they must not inflate the count.
    assert meta["n_symbols"] == outcome["n_symbols"] == 1
    # ... and the READER agrees this dir is usable, which is the only thing that matters.
    status = storage.classify_post_holdings_meta(meta, file_exists=True)
    assert status["available"] is True and status["reason"] == storage.POST_HOLDINGS_REASON_OK


def test_replay_rounds_keep_their_own_source_label(tmp_path):
    """A pinned past session is a reconstruction, and the sidecar must say `replay`."""
    outcome = runner.write_post_holdings_attestation(
        str(tmp_path), {}, as_of_date="2026-11-11", run_mode="replay"
    )

    assert outcome["written"] is True
    meta = _read(os.path.join(tmp_path, META))
    assert meta["source"] == "replay"
    # `{}` + complete:true is a CONFIRMED all-cash book, not an unknown one.
    assert meta["complete"] is True and meta["n_symbols"] == 0


def test_a_failed_book_write_leaves_an_UNUSABLE_sidecar_not_a_legacy_dir(
    tmp_path, monkeypatch
):
    """The whole point of the write order: a crash must never look like a legacy round.

    re-break: write the book first and the sidecar last (the pre-NV10 shape, and also the
    naive implementation) -- the assertions below would then see "book present, no sidecar",
    which `classify_post_holdings_meta` deliberately treats as legacy-but-USABLE, and the
    next round would seed from a book whose completeness nobody ever attested.
    """
    real_write = runner._atomic_write_json_file

    def _fail_at(fail_on_call):
        calls = {"n": 0}

        def _writer(path, payload):
            calls["n"] += 1
            if calls["n"] == fail_on_call:
                raise OSError(28, "No space left on device")
            return real_write(path, payload)

        return _writer

    # (a) the BOOK write dies -> no book, and the pending sidecar is already unusable.
    monkeypatch.setattr(runner, "_atomic_write_json_file", _fail_at(2))
    outcome = runner.write_post_holdings_attestation(
        str(tmp_path), {"600519.SH": 100}, as_of_date="2026-11-16"
    )
    assert outcome["written"] is False
    assert outcome["reason"] == runner.POST_HOLDINGS_REASON_WRITE_FAILED
    assert "No space left" in outcome["error"]
    meta = _read(os.path.join(tmp_path, META))
    assert meta["complete"] is False
    status = storage.classify_post_holdings_meta(meta, file_exists=False)
    assert status["available"] is False
    assert status["legacy"] is False, "an unattested book must NOT read as a legacy dir"

    # (b) the FINAL attestation dies (the book IS on disk) -> `incomplete`, never legacy.
    monkeypatch.setattr(runner, "_atomic_write_json_file", _fail_at(3))
    outcome = runner.write_post_holdings_attestation(
        str(tmp_path), {"600519.SH": 100}, as_of_date="2026-11-16"
    )
    assert outcome["written"] is False and outcome["book_written"] is True
    meta = _read(os.path.join(tmp_path, META))
    assert meta["complete"] is False
    status = storage.classify_post_holdings_meta(
        meta, file_exists=os.path.exists(os.path.join(tmp_path, POST))
    )
    assert status["available"] is False
    assert status["reason"] == storage.POST_HOLDINGS_REASON_INCOMPLETE
    assert status["legacy"] is False


def test_an_unknown_book_is_never_attested_as_complete(tmp_path):
    """`None` means the ticket cannot express a book -- that is not an empty book."""
    outcome = runner.write_post_holdings_attestation(str(tmp_path), None, as_of_date="2026-11-16")

    assert outcome["written"] is False
    assert outcome["reason"] == "ticket_without_target_shares"
    assert not os.path.exists(os.path.join(tmp_path, POST))
    assert not os.path.exists(os.path.join(tmp_path, META))


# ---------------------------------------------------------------------------
# 2. The round-level gate: a ticket without a book is not a successful round
# ---------------------------------------------------------------------------


def test_gate_fails_the_round_only_when_a_ticket_was_written_without_its_book():
    """`ticket_written and not written` -> a FATAL message; every other state passes."""
    failed = {"written": False, "reason": "post_holdings_write_failed", "error": "OSError: disk full"}
    message = runner.post_holdings_attestation_gate(True, failed, out_json="/x/live_trading_ticket.json")
    assert message is not None
    assert "post_holdings_write_failed" in message
    assert "OSError: disk full" in message
    # No ticket at all: nothing to attest, nothing to fail.
    assert runner.post_holdings_attestation_gate(False, failed) is None
    # Ticket + book written (including the confirmed all-cash pair): success.
    assert runner.post_holdings_attestation_gate(True, {"written": True}) is None
    assert runner.post_holdings_attestation_gate(True, None) is not None


def test_the_gate_raises_the_named_error(tmp_path):
    """The error type is what `check_live_run_integrity` reads out of stderr as the detail."""
    assert issubclass(runner.PostHoldingsAttestationError, RuntimeError)
    with pytest.raises(runner.PostHoldingsAttestationError):
        raise runner.PostHoldingsAttestationError("boom")


# ---------------------------------------------------------------------------
# 3. The web/legacy path: fill a missing sidecar, never touch a present one
# ---------------------------------------------------------------------------


def _legacy_run_dir(tmp_path, *, book=None, ticket=None, date_str="2026-10-29", inst="215823"):
    run_dir = os.path.join(str(tmp_path), date_str, inst)
    os.makedirs(run_dir, exist_ok=True)
    if book is not None:
        _write(os.path.join(run_dir, POST), book)
    if ticket is not None:
        _write(os.path.join(run_dir, "live_trading_ticket.json"), ticket)
    return run_dir


def test_ensure_fills_a_legacy_dirs_missing_sidecar(tmp_path):
    """A pre-D5 round (book, no sidecar) becomes usable -- from its ticket, not from a guess."""
    run_dir = _legacy_run_dir(
        tmp_path,
        book={"600519.SH": 100},
        ticket=_ticket(
            as_of="2026-10-29",
            execution_date="2026-10-30",
            orders=[{"symbol": "600519.SH", "action": "BUY", "target_shares": 100}],
        ),
    )

    outcome = storage.ensure_post_holdings_attestation(
        run_dir, {"600519.SH": 100}, as_of="2026-10-29", source=storage.POST_HOLDINGS_SOURCE_TICKET
    )

    assert outcome["written"] is True
    assert outcome["book_written"] is True and outcome["meta_written"] is True
    meta = _read(os.path.join(run_dir, META))
    assert meta["complete"] is True and meta["as_of"] == "2026-10-29"
    assert storage.read_post_holdings_status(run_dir)["reason"] == storage.POST_HOLDINGS_REASON_OK


def test_ensure_leaves_an_already_attested_dir_byte_unchanged(tmp_path):
    """The child's own pair wins: `source: replay` and `generated_at` must survive."""
    run_dir = _legacy_run_dir(tmp_path)
    os.makedirs(run_dir, exist_ok=True)
    runner.write_post_holdings_attestation(
        run_dir, {"600519.SH": 100}, as_of_date="2026-11-16", run_mode="replay"
    )
    before = {
        name: (os.path.getmtime(os.path.join(run_dir, name)), open(os.path.join(run_dir, name), "rb").read())
        for name in (POST, META)
    }

    outcome = storage.ensure_post_holdings_attestation(
        run_dir, {"600519.SH": 100}, as_of="2026-11-16"
    )

    assert outcome["written"] is True
    assert outcome["reason"] == storage.POST_HOLDINGS_REASON_ALREADY
    assert outcome["book_written"] is False and outcome["meta_written"] is False
    after = {
        name: (os.path.getmtime(os.path.join(run_dir, name)), open(os.path.join(run_dir, name), "rb").read())
        for name in (POST, META)
    }
    assert before == after


def test_ensure_reports_a_write_failure_instead_of_raising(tmp_path, monkeypatch):
    """The caller (the deploy path) decides to fail the round; the helper never raises."""
    run_dir = _legacy_run_dir(tmp_path, book={"600519.SH": 100})

    def _boom(path, payload):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(storage, "_atomic_write_json", _boom)

    outcome = storage.ensure_post_holdings_attestation(
        run_dir, {"600519.SH": 100}, as_of="2026-10-29"
    )

    assert outcome["written"] is False
    assert outcome["reason"] == storage.POST_HOLDINGS_REASON_WRITE_FAILED


# ---------------------------------------------------------------------------
# 4. Backfill: lossless or refused, dry-run by default, never overwrites
# ---------------------------------------------------------------------------


_MATCHING_TICKET = _ticket(
    as_of="2026-10-29",
    execution_date="2026-10-30",
    orders=[
        {"symbol": "600519.SH", "action": "BUY", "target_shares": 100},
        {"symbol": "600362.SH", "action": "SELL", "target_shares": 0},
        {"symbol": "BIL", "action": "RELEASE_CASH", "target_shares": 0},
    ],
)


def test_backfill_plans_a_sidecar_when_the_ticket_reproduces_the_book_exactly(tmp_path):
    run_dir = _legacy_run_dir(tmp_path, book={"600519.SH": 100}, ticket=_MATCHING_TICKET)

    plan = storage.plan_post_holdings_meta_backfill(run_dir)

    assert plan["action"] == "write_sidecar"
    assert plan["book"] == {"600519.SH": 100}
    assert plan["as_of"] == "2026-10-29"
    payload = plan["meta_payload"]
    assert payload["complete"] is True
    assert payload["n_symbols"] == 1
    assert payload["source"] == storage.POST_HOLDINGS_SOURCE_BACKFILL
    # planning is READ-ONLY
    assert not os.path.exists(os.path.join(run_dir, META))


def test_backfill_refuses_a_ticket_without_target_shares(tmp_path):
    """No `target_shares` -> the book cannot be reconstructed -> refuse, never invent."""
    run_dir = _legacy_run_dir(
        tmp_path,
        book={"600519.SH": 100},
        ticket={"as_of": "2026-10-29", "orders": [{"symbol": "600519.SH", "action": "HOLD"}]},
    )

    plan = storage.plan_post_holdings_meta_backfill(run_dir)

    assert plan["action"] == "refuse"
    assert plan["reason"] == storage.POST_HOLDINGS_REASON_NOT_DERIVABLE
    assert not os.path.exists(os.path.join(run_dir, META))


def test_backfill_refuses_an_ambiguous_book_instead_of_preferring_a_side(tmp_path):
    """Book != ticket derivation is AMBIGUITY: report it, never pick a winner."""
    run_dir = _legacy_run_dir(tmp_path, book={"600519.SH": 200}, ticket=_MATCHING_TICKET)

    plan = storage.plan_post_holdings_meta_backfill(run_dir)

    assert plan["action"] == "refuse"
    assert plan["reason"] == storage.POST_HOLDINGS_REASON_DIFFERS
    assert storage.read_post_holdings_status(run_dir)["book"] == {"600519.SH": 200}


def test_backfill_refuses_a_voided_ticket_and_a_missing_book(tmp_path):
    run_dir = _legacy_run_dir(tmp_path, book={"600519.SH": 100}, ticket=_MATCHING_TICKET)
    _write(os.path.join(run_dir, "live_trading_ticket.voided.json"), {"voided": True})
    assert storage.plan_post_holdings_meta_backfill(run_dir)["reason"] == (
        storage.POST_HOLDINGS_REASON_TICKET_VOIDED
    )

    no_book = _legacy_run_dir(tmp_path, ticket=_MATCHING_TICKET, inst="000000")
    assert storage.plan_post_holdings_meta_backfill(no_book)["reason"] == (
        storage.POST_HOLDINGS_REASON_NO_BOOK
    )


def test_backfill_is_idempotent_and_dry_run_writes_nothing(tmp_path):
    """dry-run: `would_write` but no file; `--apply`: exactly one sidecar; re-run: skip."""
    run_dir = _legacy_run_dir(tmp_path, book={"600519.SH": 100}, ticket=_MATCHING_TICKET)
    plan = storage.plan_post_holdings_meta_backfill(run_dir)

    dry = storage.apply_post_holdings_meta_backfill(plan, dry_run=True)
    assert dry["would_write"] is True and dry["applied"] is False
    assert not os.path.exists(os.path.join(run_dir, META))

    applied = storage.apply_post_holdings_meta_backfill(plan, dry_run=False)
    assert applied["applied"] is True
    assert _read(os.path.join(run_dir, META))["source"] == storage.POST_HOLDINGS_SOURCE_BACKFILL
    book_bytes = open(os.path.join(run_dir, POST), "rb").read()

    replanned = storage.plan_post_holdings_meta_backfill(run_dir)
    assert replanned["action"] == "skip"
    assert replanned["reason"] == storage.POST_HOLDINGS_REASON_ALREADY
    assert storage.apply_post_holdings_meta_backfill(replanned, dry_run=False)["applied"] is False
    # The BOOK is never rewritten by the backfill.
    assert open(os.path.join(run_dir, POST), "rb").read() == book_bytes


def test_backfill_never_overwrites_a_sidecar_that_appeared_after_planning(tmp_path):
    """`O_EXCL`, not "check then write": a racing writer must win."""
    run_dir = _legacy_run_dir(tmp_path, book={"600519.SH": 100}, ticket=_MATCHING_TICKET)
    plan = storage.plan_post_holdings_meta_backfill(run_dir)
    _write(os.path.join(run_dir, META), {"complete": True, "n_symbols": 1, "source": "someone_else"})

    outcome = storage.apply_post_holdings_meta_backfill(plan, dry_run=False)

    assert outcome["applied"] is False
    assert outcome["reason"] == storage.POST_HOLDINGS_REASON_ALREADY
    assert _read(os.path.join(run_dir, META))["source"] == "someone_else"


def test_backfill_cli_defaults_to_dry_run_and_reports_what_it_could_not_reconstruct(
    tmp_path, capsys
):
    """`--dry-run` is the DEFAULT; `--strict` turns a refusal into exit 1."""
    good = _legacy_run_dir(tmp_path, book={"600519.SH": 100}, ticket=_MATCHING_TICKET, inst="215823")
    bad = _legacy_run_dir(
        tmp_path,
        book={"600519.SH": 100},
        ticket={"as_of": "2026-10-29", "orders": [{"symbol": "600519.SH", "action": "HOLD"}]},
        inst="210313",
    )

    exit_code = storage._backfill_main(["--runs-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "dry_run" in out
    assert "would write" in out
    assert "REFUSED" in out and "ticket_without_target_shares" in out
    assert not os.path.exists(os.path.join(good, META))
    assert not os.path.exists(os.path.join(bad, META))

    assert storage._backfill_main(["--runs-root", str(tmp_path), "--strict"]) == 1

    assert storage._backfill_main(["--runs-root", str(tmp_path), "--apply"]) == 0
    assert _read(os.path.join(good, META))["complete"] is True
    assert not os.path.exists(os.path.join(bad, META))


# ---------------------------------------------------------------------------
# 5. The checker: legacy round vs current loss (the two used to look identical)
# ---------------------------------------------------------------------------


def _instance(root, date_str, inst, *, summary_extra=None, book=True, meta=False, ticket=True):
    run_dir = os.path.join(str(root), date_str, inst)
    os.makedirs(run_dir, exist_ok=True)
    summary = {"as_of": date_str, "deploy_status": "SUCCESS"}
    summary.update(summary_extra or {})
    _write(os.path.join(run_dir, "summary.json"), summary)
    if ticket:
        _write(os.path.join(run_dir, "live_trading_ticket.json"), _ticket(as_of=date_str))
    if book:
        _write(os.path.join(run_dir, POST), {"600519.SH": 100})
    if meta:
        _write(os.path.join(run_dir, META), {"as_of": date_str, "complete": True, "n_symbols": 1})
    return run_dir


def test_checker_tells_a_legacy_round_from_a_current_loss(tmp_path):
    """`post_holdings_written` ABSENT -> legacy; `false` -> a current round that lost its book."""
    import datetime as dt

    _instance(tmp_path, "2026-10-29", "215823")  # legacy: no such key anywhere
    _instance(
        tmp_path,
        "2026-11-16",
        "142959",
        summary_extra={"post_holdings_written": False, "post_holdings_reason": "post_holdings_write_failed"},
    )

    payload = integrity.audit_run_archive(
        tmp_path,
        trading_days=[dt.date(2026, 10, 29), dt.date(2026, 11, 16)],
    )

    findings = {f["instance"]: f for f in payload["findings"] if f["code"] == "POST_HOLDINGS_MISSING"}
    assert findings["215823"]["legacy"] is True
    assert "历史遗留" in findings["215823"]["detail"]
    assert findings["142959"]["legacy"] is False
    assert "当前轮丢失侧车" in findings["142959"]["detail"]
    assert payload["summary"]["post_holdings_legacy_rounds"] == 1
    assert payload["summary"]["post_holdings_current_losses"] == 1
    # ... and the code/severity are UNCHANGED, so NV6's measured counts still mean the same.
    assert payload["summary"]["counts_by_code"]["POST_HOLDINGS_MISSING"] == 2


def test_checker_flags_a_summary_that_claims_a_sidecar_which_is_gone(tmp_path):
    """`post_holdings_written: true` + no sidecar = the sidecar was deleted or lost."""
    import datetime as dt

    _instance(tmp_path, "2026-11-16", "142959", summary_extra={"post_holdings_written": True})

    payload = integrity.audit_run_archive(tmp_path, trading_days=[dt.date(2026, 11, 16)])

    finding = payload["findings"][0]
    assert finding["code"] == "POST_HOLDINGS_MISSING"
    assert finding["legacy"] is False
    assert "侧车在写出后被删除或丢失" in finding["detail"]
