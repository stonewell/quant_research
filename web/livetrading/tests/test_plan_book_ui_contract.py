"""SSOT item 10, FRONTEND half: 「💾 保存持仓」 must never announce a save it did not make.

WHY THIS FILE EXISTS
--------------------
The backend half (``tests/test_plan_book_guard.py``) pins the gate itself: a NON-EMPTY book
identical to the same date's plan is refused with HTTP 409
(``storage.UnconfirmedPlanBookSaveError``) unless the caller passes
``confirm_plan_book: true``.  The guard is worthless if the client does not read the status
code: ``saveHoldings()`` in ``static/js/app.js`` used to ignore ``res.ok`` entirely, so a 409
still printed ``[SUCCESS] 持仓配置已成功保存``, cleared the "this table is a plan" notice and
left the operator believing a plan-shaped book had been persisted.  A false confirmation is
worse than no guard, because the operator then trades on a book nobody verified.

This suite is a STATIC CONTRACT TEST, not a behavioural one: there is no JS test runner in
this repository (and adding one for a single button is out of scope), so nothing here
executes ``app.js``.  It parses the source as TEXT, isolates the ``saveHoldings`` function
body by brace matching, and asserts the ORDER of tokens inside that body:

(a) a response-status check (``res.ok``) exists, and every such check precedes the success
    log -- deleting the check makes the first assertion red, and moving the log above it
    makes the ordering assertion red;
(b) the success log string therefore cannot be reached before the check;
(c) ``confirm_plan_book`` lives in the SAVE PAYLOAD (after the ``fetch("/api/holdings"``
    call site, before the 409 branch) and is conditional -- an always-on key would license
    the plan on every ordinary save;
(d) the 409 branch exists and forwards the server's ``detail`` verbatim (via
    ``refusalDetail``), and the confirmed re-submit is a second ``postHoldings(true)`` call;
plus the untouched-on-refusal guarantees: the plan notice is cleared only after the status
check, and no table refresh is issued from the save path at all.

The assertions are structural on purpose -- they pin relative ordering, not formatting, so a
future edit that drops the check fails here even if the code still "looks right".  The real
browser behaviour remains uncovered (``D14`` in ``docs/development_plan.md``); this is a
source-shape guard, disclosed as such.

Re-break evidence is recorded per test below (each was verified by re-running the same test
against a mutated COPY of ``app.js`` in a temp dir, then discarding the copy -- no production
file was left modified).
"""

import os

_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The UI source under test (``saveHoldings`` / the empty-book guard live here).
_APP_JS_PATH = os.path.join(_WEB_DIR, "static", "js", "app.js")

#: The function whose body is audited, and its opening signature line.
_SAVE_SIGNATURE = "async function saveHoldings()"

#: The success line whose position is the whole point of the guard.
_SUCCESS_LOG = "[SUCCESS] 持仓配置已成功保存"


def _read_app_js() -> str:
    with open(_APP_JS_PATH, encoding="utf-8") as f:
        return f.read()


def _function_body(source: str, signature: str) -> str:
    """Return ``source``'s ``signature`` function body, braces included.

    Deliberately shallow: string literals (single/double/backtick), line comments and block
    comments are skipped so their braces/quotes cannot desynchronise the depth counter.  It
    does not parse JS -- it only has to be exact for the one function this file audits.
    """
    start = source.find(signature)
    assert start != -1, f"`{signature}` not found in {_APP_JS_PATH} -- rename updated?"
    open_brace = source.find("{", start)
    assert open_brace != -1, f"`{signature}` has no body in {_APP_JS_PATH}"

    depth = 0
    i = open_brace
    n = len(source)
    while i < n:
        ch = source[i]
        pair = source[i : i + 2]
        if pair == "//":
            newline = source.find("\n", i)
            i = n if newline == -1 else newline + 1
            continue
        if pair == "/*":
            end = source.find("*/", i)
            i = n if end == -1 else end + 2
            continue
        if ch in ("'", '"'):
            i += 1
            while i < n and source[i] != ch:
                i += 2 if source[i] == "\\" else 1
            i += 1
            continue
        if ch == "`":
            # Nested template literals are not used in this function; a flat skip keeps the
            # brace count honest for every form that does appear (`${expr}` included).
            i += 1
            while i < n and source[i] != "`":
                i += 2 if source[i] == "\\" else 1
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace : i + 1]
        i += 1

    raise AssertionError(f"unbalanced braces: `{signature}` body never closes in {_APP_JS_PATH}")


def _save_body() -> str:
    return _function_body(_read_app_js(), _SAVE_SIGNATURE)


def test_save_holdings_checks_response_status_before_the_success_log():
    """(a)+(b): a status check exists AND sits above the success log.

    Re-break (a): delete every ``res.ok`` line from the body copy -> the first assert fires.
    Re-break (b): move the ``_SUCCESS_LOG`` line above the ``if (!res.ok)`` block -> the
    ordering assert fires.
    """
    body = _save_body()

    assert "res.ok" in body, (
        "saveHoldings() no longer inspects the response status: the server's 409 refusal "
        "would be reported as a successful save -- the exact false confirmation SSOT item "
        f"10 exists to remove ({_APP_JS_PATH})"
    )
    assert _SUCCESS_LOG in body, (
        "the success log line disappeared; this audit pins its position, so re-point "
        f"`_SUCCESS_LOG` if the line was intentionally reworded ({_APP_JS_PATH})"
    )

    success_at = body.index(_SUCCESS_LOG)
    # Stricter than "one check exists": EVERY status check in the body must precede the
    # success log, so a new unguarded success branch cannot slip in beneath one good check.
    check_positions = []
    cursor = 0
    while True:
        found = body.find("res.ok", cursor)
        if found == -1:
            break
        check_positions.append(found)
        cursor = found + len("res.ok")

    late = [pos for pos in check_positions if pos > success_at]
    assert not late, (
        "a `res.ok` check sits AFTER the success log, so at least one path can still log "
        f"[SUCCESS] for a non-2xx response ({_APP_JS_PATH})"
    )
    assert body.count(_SUCCESS_LOG) == 1, (
        "expected exactly one success log line in saveHoldings(); a second one is an "
        f"unguarded success announcement ({_APP_JS_PATH})"
    )


def test_save_holdings_does_not_clear_the_plan_notice_or_refresh_before_the_check():
    """A refusal must leave the plan notice and the table exactly as they were.

    The notice says "this table is a plan, not a fill receipt"; clearing it on a 409 would
    erase the only on-screen warning that the book was never verified.  Likewise the save
    path must not repaint/refresh the table as if a write had landed.

    Re-break: move ``clearHoldingsPlanNotice()`` above the status check -> red.  Add a
    ``renderHoldingsTable(...)`` call to the save path -> red.
    """
    body = _save_body()

    assert "clearHoldingsPlanNotice()" in body, (
        "the plan notice is never cleared any more -- the ordinary (accepted) save must "
        f"still drop the 'planned' label ({_APP_JS_PATH})"
    )
    assert "res.ok" in body, f"no status check to order against ({_APP_JS_PATH})"
    assert body.index("res.ok") < body.index("clearHoldingsPlanNotice()"), (
        "the plan notice is cleared before the response status is checked, so a refused "
        f"save still erases the 'this is a plan' warning ({_APP_JS_PATH})"
    )

    for refresh in ("renderHoldingsTable(", "loadHoldings(", "fetchQuotesForCurrentHoldings("):
        assert refresh not in body, (
            f"saveHoldings() now calls `{refresh}`; the save path must not repaint the book "
            f"as if the write had been persisted ({_APP_JS_PATH})"
        )


def test_save_payload_carries_confirm_plan_book_only_after_the_operator_confirms():
    """(c): the licence to save a plan-shaped book lives in the PAYLOAD and is conditional.

    Re-break: drop ``confirm_plan_book`` from the payload -> red (first assert).  Make it
    unconditional (send ``true`` on the first attempt) -> red (the conditional assert), which
    matters because an always-on licence silently disables the backend gate.
    """
    body = _save_body()

    fetch_at = body.find('fetch("/api/holdings"')
    assert fetch_at != -1, (
        f"saveHoldings() no longer posts to /api/holdings ({_APP_JS_PATH})"
    )
    key_at = body.find("confirm_plan_book: true")
    assert key_at != -1, (
        "the save payload no longer carries `confirm_plan_book: true`, so the only way to "
        "save a genuinely plan-identical book (a real fill that matched exactly) is gone "
        f"({_APP_JS_PATH})"
    )
    assert fetch_at < key_at, (
        "`confirm_plan_book` appears before the save request is even built -- it must be a "
        f"field of the POST payload, not a log string or a deploy knob ({_APP_JS_PATH})"
    )
    assert "confirmPlanBook ?" in body, (
        "`confirm_plan_book` is no longer conditional: sending it on the first attempt "
        f"would licence the plan into a 'verified' book ({_APP_JS_PATH})"
    )
    assert "await postHoldings(false)" in body and "await postHoldings(true)" in body, (
        "expected an unlicensed first attempt (`postHoldings(false)`) and a re-submit "
        f"carrying the operator's confirmation (`postHoldings(true)`) ({_APP_JS_PATH})"
    )


def test_save_holdings_has_a_409_branch_that_forwards_the_server_detail():
    """(d): the 409 branch exists, is refused-not-succeeded, and carries the server text.

    The server's ``detail`` already names ``date=`` and ``instance=``, i.e. WHICH plan was
    painted into the table -- paraphrasing it would lose that.  The branch must sit before
    the success log so a refusal can never fall through to it.

    Re-break: delete the ``res.status === 409`` branch -> red.  Replace the forwarded
    ``detail`` with a fixed string -> the ``detail``/``refusalDetail`` assertion goes red.
    """
    body = _save_body()

    branch_at = body.find("=== 409")
    assert branch_at != -1, (
        "saveHoldings() no longer branches on HTTP 409, so the plan-book refusal and the "
        f"generic failure path are indistinguishable to the operator ({_APP_JS_PATH})"
    )
    assert branch_at < body.index(_SUCCESS_LOG), (
        f"the 409 branch sits after the success log ({_APP_JS_PATH})"
    )
    assert "[REFUSED]" in body and body.index("[REFUSED]") < body.index(_SUCCESS_LOG), (
        "no `[REFUSED]` console line precedes the success line: a refusal would be logged "
        f"as `[SUCCESS]` again ({_APP_JS_PATH})"
    )

    # The branch body itself (generous window) must forward the server's detail.
    branch = body[branch_at : branch_at + 900]
    assert "refusalDetail(" in branch, (
        "the 409 branch does not route the response through `refusalDetail`, so the "
        f"server's `date=` / `instance=` text may no longer reach the console ({_APP_JS_PATH})"
    )
    assert "detail" in branch, (
        f"the 409 branch no longer references the server's `detail` ({_APP_JS_PATH})"
    )
    assert "confirmPlanBookSave(" in branch, (
        "the 409 branch does not offer the explicit confirmation prompt; without it the "
        f"escape hatch is unreachable from the UI ({_APP_JS_PATH})"
    )


def test_empty_book_question_is_asked_before_anything_is_submitted():
    """The two guards stay independent, and the empty-book one is asked FIRST.

    An empty book is not the plan-book gate's business (the server skips it there); it is the
    pre-existing "is the account really all cash?" question.  Both must be able to fire, with
    the empty-book question coming before any request leaves the browser.

    Re-break: move the ``confirmEmptyBookIfNeeded(holdings)`` call below
    ``await postHoldings(false)`` -> red.
    """
    body = _save_body()

    assert "confirmEmptyBookIfNeeded(holdings)" in body, (
        "the empty-book confirmation is no longer asked on the save path (it must stay "
        f"intact and independent of the plan-book gate) ({_APP_JS_PATH})"
    )
    send_at = body.find("await postHoldings(")
    assert send_at != -1, f"no save request found ({_APP_JS_PATH})"
    assert body.index("confirmEmptyBookIfNeeded(holdings)") < send_at, (
        "the empty-book question is asked after the request has already been sent, so an "
        f"unconfirmed empty book could still reach the server ({_APP_JS_PATH})"
    )
