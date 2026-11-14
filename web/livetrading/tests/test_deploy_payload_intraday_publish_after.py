"""P1-1 parity at the UI layer: BOTH `/api/deploy/multi` payloads send
`intraday_publish_after`.

FORENSIC CONTEXT (the reachability gap this closes)
---------------------------------------------------
The server side of `intraday_publish_after` was already complete on all three request
shapes: `RunHealthRequest` / `RunDeployRequest` carry the field,
`MultiStrategyDeployRequest` was brought to parity (models.py field + server.py
`normalize_intraday_publish_after` validation + forwarding), and
`run_multi_strategy_netted_deploy` appends `--intraday-publish-after <value>` to the child
argv only for a non-empty value.  The operator still could not use it on the multi path:
the two multi payloads in `app.js` are built FIELD-BY-FIELD (`current_holdings`,
`data_provider`, `min_holding_bars`, ...), not by spreading `getExecutionParams()`, so the
key the single path inherits through its `...baseParams` spread was simply never named.
Pydantic accepted the request, defaulted the field to `None`, and the child ran with its
own default -- the same "model field silently dropped" trap that lost `cash` and
`confirm_empty_book` before it.

WHAT IS PINNED HERE
-------------------
* every multi-deploy payload site carries the key, sourced from the SAME place as the
  single path (`baseParams.intraday_publish_after`, i.e. the object `getExecutionParams()`
  builds from the operator's `inputIntradayPublishAfter`, `null` when left blank);
* the number of multi-deploy payload sites is EXACTLY 2, so a future third site fails here
  instead of silently regressing;
* the same check on the single-deploy sites, which already comply by spreading
  `baseParams` -- pinned so a later refactor of `getExecutionParams()` cannot quietly
  delete the key that BOTH paths depend on.

HONEST SCOPE: this is a STATIC SOURCE ASSERTION, like
`test_live_book_integrity.py::test_every_deploy_payload_sends_the_confirmed_empty_book`.
The repository has no JS test framework (and adding one is out of scope), so nothing here
executes `app.js`; it pins the shape and the site count only.  The runtime contract
(`null` -> flag absent, value -> flag appended verbatim) is pinned hermetically in
`test_multi_deploy_intraday_publish_after.py` and `test_p1_intraday_publish_after.py`.

re-break: delete `intraday_publish_after: baseParams.intraday_publish_after,` from ONE
multi payload -- the per-site assert fires name the payload variable and line (verified
red, then restored byte-identically).  Add a third multi payload that omits the key -- the
`len(...) == 2` assert fires (verified red, then restored).  Drop the key from
`getExecutionParams()`'s returned object -- the single-deploy test fires as well, because
the multi sites read it from that object.
"""

import os
import re

_APP_JS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "js", "app.js"
)

_KEY = "intraday_publish_after"

_MULTI_ROUTE = "/api/deploy/multi"
_SINGLE_ROUTE = "/api/run-deploy"

#: `body: JSON.stringify(<local variable>)` -- the payload actually put on the wire.
_STRINGIFY_RE = re.compile(r"body:\s*JSON\.stringify\(\s*([A-Za-z_$][\w$]*)\s*\)")

#: A property DEFINITION (`key: value` at the start of a line), never a prose mention.
_KEY_PROPERTY_RE = re.compile(
    r"^[ \t]*" + re.escape(_KEY) + r"[ \t]*:[ \t]*(.+?),?[ \t]*$", re.M
)

_SPREAD_RE = re.compile(r"^[ \t]*\.\.\.([A-Za-z_$][\w$]*)[ \t]*,?[ \t]*$")


def _source_lines():
    with open(_APP_JS_PATH, encoding="utf-8") as f:
        return f.read().splitlines()


def _fetch_sites(lines, route):
    """Line indices (0-based) of `fetch("<route>", ...)` calls."""
    pattern = re.compile(r"fetch\(\s*['\"`]" + re.escape(route) + r"['\"`]")
    return [i for i, line in enumerate(lines) if pattern.search(line)]


def _payload_var(lines, fetch_idx, window=20):
    """The local variable handed to `JSON.stringify` by the fetch at `fetch_idx`."""
    for j in range(fetch_idx, min(fetch_idx + window, len(lines))):
        m = _STRINGIFY_RE.search(lines[j])
        if m:
            return m.group(1)
    raise AssertionError(
        f"{_APP_JS_PATH}:{fetch_idx + 1}: no `body: JSON.stringify(<local>)` within "
        f"{window} lines of a fetch call -- the payload cannot be audited statically"
    )


def _declaration(lines, name, before_idx):
    """`(line index, right-hand side)` of the nearest `const <name> = ...` above the call."""
    pattern = re.compile(
        r"^[ \t]*(?:const|let|var)[ \t]+" + re.escape(name) + r"[ \t]*=[ \t]*(.*)$"
    )
    for i in range(min(before_idx, len(lines) - 1), -1, -1):
        m = pattern.match(lines[i])
        if m:
            return i, m.group(1).strip()
    raise AssertionError(
        f"{_APP_JS_PATH}: no `const/let/var {name} = ...` declaration above line "
        f"{before_idx + 1}, so the payload variable cannot be resolved"
    )


def _literal_block(lines, start_idx):
    """The text of the object literal opening on `start_idx`, through its `};` line.

    Every deploy payload in `app.js` is a FLAT literal closing on a line whose first
    non-space characters are `};`, and no nested object inside them closes that way, so a
    terminator scan is enough -- no brace counting over comments that mention `{}`.
    """
    for j in range(start_idx + 1, len(lines)):
        if lines[j].strip().startswith("};"):
            return "\n".join(lines[start_idx : j + 1])
    raise AssertionError(
        f"{_APP_JS_PATH}:{start_idx + 1}: object literal never closed by a `}};` line "
        "-- update this audit's terminator rule"
    )


def _function_return_literal(lines, func_name, depth=0):
    """The object a named function returns (`function f() { ... return { ... }; }`)."""
    if depth > 3:
        raise AssertionError(f"payload resolution recursed too deep at `{func_name}`")
    header = re.compile(
        r"^[ \t]*(?:async[ \t]+)?function[ \t]+" + re.escape(func_name) + r"[ \t]*\("
    )
    for i, line in enumerate(lines):
        if not header.match(line):
            continue
        for j in range(i + 1, len(lines)):
            if lines[j].strip().startswith("return {"):
                return _literal_block(lines, j)
        raise AssertionError(
            f"{_APP_JS_PATH}:{i + 1}: `{func_name}` has no `return {{` object literal"
        )
    raise AssertionError(f"{_APP_JS_PATH}: no `function {func_name}(` definition found")


def _effective_payload(lines, var_name, before_idx, depth=0):
    """The payload text a fetch sends, with `...spread` sources resolved in place.

    `runLiveDeploy` / `runFullWorkflow` stringify `{ ...baseParams, ... }`, so the key
    lives in `getExecutionParams()`'s returned object rather than the site itself; the
    spread is followed so the assertion still pins what actually goes over the wire.
    """
    decl_idx, value = _declaration(lines, var_name, before_idx)
    if value.startswith("{"):
        text = _literal_block(lines, decl_idx)
    else:
        call = re.match(r"^([A-Za-z_$][\w$]*)\s*\(\)\s*;?\s*$", value)
        if not call:
            raise AssertionError(
                f"{_APP_JS_PATH}:{decl_idx + 1}: `{var_name}` is neither an object literal "
                f"nor a bare `foo()` call, so its payload cannot be audited: {value!r}"
            )
        text = _function_return_literal(lines, call.group(1), depth + 1)

    parts = [text]
    for line in text.splitlines():
        m = _SPREAD_RE.match(line)
        if not m:
            continue
        parts.append(_effective_payload(lines, m.group(1), decl_idx, depth + 1))
    return "\n".join(parts)


def _sent_key_values(payload_text):
    """Every `intraday_publish_after:` property VALUE in a payload (comments excluded)."""
    return [m.group(1).strip() for m in _KEY_PROPERTY_RE.finditer(payload_text)]


def test_both_multi_deploy_payloads_send_intraday_publish_after():
    """Both `/api/deploy/multi` payloads name the key, and there are exactly two sites.

    The multi payloads are built field-by-field, so anything not named there is simply not
    sent -- `MultiStrategyDeployRequest` then defaults the field to `None` and the child
    runs on its own 20:00 default while the operator's value sits unused in the form.

    re-break: delete the property line from ONE site -- the per-site assert fires with the
    payload variable and the fetch line. Add a third `/api/deploy/multi` fetch without the
    key -- the count assert fires.
    """
    lines = _source_lines()
    sites = _fetch_sites(lines, _MULTI_ROUTE)

    assert len(sites) == 2, (
        "expected exactly 2 `/api/deploy/multi` payload sites, found "
        f"{len(sites)}: {[i + 1 for i in sites]} -- a new multi deploy path must send "
        f"`{_KEY}` too; update this count once it does"
    )

    for fetch_idx in sites:
        var_name = _payload_var(lines, fetch_idx)
        payload = _effective_payload(lines, var_name, fetch_idx)
        values = _sent_key_values(payload)

        assert values, (
            f"{_APP_JS_PATH}: the multi payload `{var_name}` (fetch at line "
            f"{fetch_idx + 1}) never sends `{_KEY}`, so the operator's intraday publish "
            "deadline is silently dropped for the netted deploy path"
        )
        # Same source as the single path: `baseParams` is the object `getExecutionParams()`
        # returns, so the value is the operator's own input -- `null` when left blank,
        # which the route reads as "leave the child's default alone".
        assert values == ["baseParams.intraday_publish_after"], (
            f"{_APP_JS_PATH}: the multi payload `{var_name}` (fetch at line "
            f"{fetch_idx + 1}) sends `{_KEY}` from a different source: {values} -- it "
            "must mirror the single deploy path's `baseParams.intraday_publish_after`"
        )

        # ... and prove that identifier actually resolves to a payload carrying the key,
        # otherwise `baseParams.intraday_publish_after` would be `undefined` at runtime.
        base_payload = _effective_payload(lines, "baseParams", fetch_idx)
        assert _sent_key_values(base_payload) == ["getIntradayPublishAfterInput()"], (
            f"{_APP_JS_PATH}: `baseParams` (i.e. `getExecutionParams()`) no longer carries "
            f"`{_KEY}` sourced from `getIntradayPublishAfterInput()` -- both the single and "
            "the multi deploy paths read it from there"
        )


def test_both_single_deploy_payloads_send_intraday_publish_after():
    """The single-deploy sites already comply via `...baseParams`; this pins that.

    `runLiveDeploy` and the `runFullWorkflow` Stage-2 branch both stringify
    `{ ...baseParams, current_holdings: ... }`, so the key is inherited rather than named.
    Pinned for two reasons: the count keeps a future single-deploy site from being added
    without the key, and the spread resolution keeps `getExecutionParams()` from shedding
    the field that ALL deploy payloads now depend on.

    re-break: remove `intraday_publish_after: getIntradayPublishAfterInput(),` from the
    object `getExecutionParams()` returns -- both sites of this test go red.
    """
    lines = _source_lines()
    sites = _fetch_sites(lines, _SINGLE_ROUTE)

    assert len(sites) == 2, (
        "expected exactly 2 `/api/run-deploy` payload sites, found "
        f"{len(sites)}: {[i + 1 for i in sites]} -- a new single deploy path must inherit "
        f"`{_KEY}` from `getExecutionParams()`; update this count once it does"
    )

    for fetch_idx in sites:
        var_name = _payload_var(lines, fetch_idx)
        payload = _effective_payload(lines, var_name, fetch_idx)
        assert _sent_key_values(payload), (
            f"{_APP_JS_PATH}: the single-deploy payload `{var_name}` (fetch at line "
            f"{fetch_idx + 1}) no longer resolves to a `{_KEY}` property, so the operator's "
            "intraday publish deadline is dropped for the single path as well"
        )
