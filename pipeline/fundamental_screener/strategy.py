"""Top-level strategy module for fundamental_screener.

Re-exports `FundamentalMarginOfSafetyStrategy` and `FundamentalScreenerStrategy`.
"""

try:
    from .fscreen.strategy import FundamentalMarginOfSafetyStrategy, FundamentalScreenerStrategy
except ImportError:
    from fscreen.strategy import FundamentalMarginOfSafetyStrategy, FundamentalScreenerStrategy

__all__ = ["FundamentalMarginOfSafetyStrategy", "FundamentalScreenerStrategy"]
