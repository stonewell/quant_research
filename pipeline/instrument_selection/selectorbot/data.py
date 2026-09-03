"""Fundamentals metadata lookup, thin wrapper over the shared quant-level
loader (`common/data.py`)."""

from common.data import fetch_fund_metadata as _fetch_fund_metadata


def fetch_fund_metadata(symbol: str, provider=None, **kwargs) -> dict:
    return _fetch_fund_metadata(symbol, provider=provider, **kwargs)
