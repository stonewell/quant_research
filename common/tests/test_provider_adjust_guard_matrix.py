"""Offline acceptance/rejection matrix for the per-provider adjust-basis guard.

`_normalize_adjust_basis` (M3.9, `common/financial_api.py`) is fail-closed: a typo must raise
instead of silently asking for unadjusted bars, which is what the old `else -> raw` fallbacks did.
These two matrices pin (a) the accepted-token -> canonical-basis -> legacy-attribute mapping and
(b) the rejection surface, for all four providers, without opening a DuckDB file, a socket, or an
HTTP session.
"""

import os
import sys

import pytest

# Same pytest-path bootstrap the sibling suites use: `common/` has no venv of its own.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common import financial_api as fa  # noqa: E402

_KWARG = {
    "MarketDBDataProvider": "adjust",
    "FuyaoDataProvider": "adjust",
    "BaoStockDataProvider": "adjustflag",
    "EastMoneyDataProvider": "adjust",
}


def _build(provider, token):
    """Construct a provider with no real resources (no DB file, no socket, no session)."""
    kwarg = _KWARG[provider]
    if provider == "MarketDBDataProvider":
        # `con=object()` takes the ":memory:" branch: `_resolve_duckdb_path` is never reached,
        # so no file is opened (and no FileNotFoundError can mask the ValueError we assert on).
        return fa.MarketDBDataProvider(con=object(), **{kwarg: token})
    if provider == "FuyaoDataProvider":
        # prefer_local=False -> the local MarketDB fallback provider is never constructed.
        return fa.FuyaoDataProvider(prefer_local=False, **{kwarg: token})
    if provider == "EastMoneyDataProvider":
        # An injected session skips `requests.Session()` and its header setup.
        return fa.EastMoneyDataProvider(session=object(), **{kwarg: token})
    # BaoStock logs in lazily; without `cache_dir`/`cache` no DuckDBCache is opened either.
    return fa.BaoStockDataProvider(**{kwarg: token})


# (token, canonical basis, legacy `adjust` spelling) for the two `adjust` providers.
_LEGACY_ALIASES = [
    ("none", "raw", "none"),
    ("qfq", "qfq", "forward"),
    ("forward", "qfq", "forward"),
    ("hfq", "hfq", "backward"),
    ("backward", "hfq", "backward"),
]

ACCEPT_CASES = [
    (p, token, basis, {"adjust": legacy})
    for p in ("MarketDBDataProvider", "FuyaoDataProvider")
    for token, basis, legacy in _LEGACY_ALIASES
] + [
    ("BaoStockDataProvider", "1", "hfq", {"adjustflag": "1"}),
    ("BaoStockDataProvider", "2", "qfq", {"adjustflag": "2"}),
    ("BaoStockDataProvider", "3", "raw", {"adjustflag": "3"}),
    ("BaoStockDataProvider", "none", "raw", {"adjustflag": "3"}),
    ("BaoStockDataProvider", "qfq", "qfq", {"adjustflag": "2"}),
    ("BaoStockDataProvider", "hfq", "hfq", {"adjustflag": "1"}),
    ("EastMoneyDataProvider", "none", "raw", {"adjust": "none", "fqt": "0"}),
    ("EastMoneyDataProvider", "qfq", "qfq", {"adjust": "qfq", "fqt": "1"}),
    ("EastMoneyDataProvider", "hfq", "hfq", {"adjust": "hfq", "fqt": "2"}),
    # Case is folded (`value.lower()`), so an upper-case token is a valid alias, not a rejection.
    ("EastMoneyDataProvider", "QFQ", "qfq", {"adjust": "qfq", "fqt": "1"}),
]


@pytest.mark.parametrize("provider,token,basis,legacy", ACCEPT_CASES)
def test_acceptance_matrix(provider, token, basis, legacy):
    prov = _build(provider, token)
    assert prov.native_adjust_basis == basis
    for attr, expected in legacy.items():
        assert getattr(prov, attr) == expected, f"{provider}.{attr} for {token!r}"
    if provider == "BaoStockDataProvider":
        # Must stay a protocol digit: it is passed straight to query_history_k_data_plus.
        assert prov.adjustflag in {"1", "2", "3"}


_COMMON_REJECT = ["", "   ", "invalid", "fq", "99", None, 0]
_EXTRA_REJECT = {
    "MarketDBDataProvider": ["raw"],
    "FuyaoDataProvider": ["raw"],
    "BaoStockDataProvider": ["0", "4", "raw"],
    # "QFQ" is deliberately absent: lower-casing makes it a valid alias (see ACCEPT_CASES).
    "EastMoneyDataProvider": ["forward", "raw", "QFQ ", "0", "1", "2", "3"],
}

REJECT_CASES = [
    (provider, token)
    for provider, extra in _EXTRA_REJECT.items()
    for token in _COMMON_REJECT + extra
]


@pytest.mark.parametrize("provider,token", REJECT_CASES)
def test_rejection_matrix(provider, token):
    with pytest.raises(ValueError):
        _build(provider, token)
