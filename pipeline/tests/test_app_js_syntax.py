"""Real syntax guard for ``web/livetrading/static/js/app.js`` (review finding, durability).

WHY THIS FILE EXISTS
====================
The `app.js` contract test in ``web/livetrading/tests`` is a STATIC TEXT test: it greps the
file for known strings. That stays green while the file stops PARSING -- a missing brace,
an unterminated template literal, a stray ``</script>`` -- so the whole live-trading UI can
be a blank page with every Python assertion passing. Text checks cannot see grammar;
``node --check`` can, and it needs no browser, no server and no network.

The check is deliberately tiny and offline:
* ``node --check <file>`` parses only -- it never executes the module, so no imports, no
  DOM, no ``fetch`` and no clock are involved;
* ``pytest.skip`` when ``node`` is not installed (a research workstation without Node must
  not fail the pipeline suite), but a present-and-failing Node is a hard failure with the
  compiler's stderr shown verbatim -- a syntax error is exactly the thing to read;
* a ~30s timeout, because a hung ``node`` must not hang the suite.

Node is not a Python dependency: this file adds no import, no requirement and no data.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_JS = REPO_ROOT / "web" / "livetrading" / "static" / "js" / "app.js"

#: A parse of a ~180 KB script is milliseconds; 30s only bounds a pathological hang.
NODE_CHECK_TIMEOUT_SECONDS = 30


def test_app_js_parses_under_node_check():
    """``node --check app.js`` must exit 0, with its own stderr shown when it does not."""
    node = shutil.which("node")
    if node is None:
        pytest.skip(
            "node is not installed: the real app.js syntax guard cannot run here "
            "(the static text contract test remains the only check)"
        )

    assert APP_JS.is_file(), f"the live-trading UI script is missing: {APP_JS}"

    try:
        completed = subprocess.run(
            [node, "--check", str(APP_JS)],
            capture_output=True,
            text=True,
            timeout=NODE_CHECK_TIMEOUT_SECONDS,
            # No shell, no cwd dependence: the path is absolute and `--check` only parses.
            cwd=str(REPO_ROOT),
        )
    except subprocess.TimeoutExpired:  # pragma: no cover - a hung node, not a syntax bug
        pytest.fail(
            f"`node --check {APP_JS}` did not finish within "
            f"{NODE_CHECK_TIMEOUT_SECONDS}s; treating an unparseable/hanging UI script as a "
            "failure rather than a pass"
        )

    assert completed.returncode == 0, (
        f"`node --check {APP_JS}` failed (exit {completed.returncode}): the live-trading UI "
        "script does not parse, so every static-text assertion about it is meaningless.\n"
        f"--- node stderr ---\n{completed.stderr or '<empty>'}\n"
        f"--- node stdout ---\n{completed.stdout or '<empty>'}"
    )
