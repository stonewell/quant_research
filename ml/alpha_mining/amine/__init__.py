"""Two-stage Genetic Programming + LightGBM cross-sectional alpha mining (ML1).

Stage 1 discovers formulaic cross-sectional factors via typed Genetic
Programming over ``common/alpha101_operators.py`` primitives with a
multi-objective fitness (RankIC * sqrt(daily IR) net of turnover, library
correlation, and expression-length penalties).

Stage 2 combines the discovered factors with LightGBM under strict Purged
Group TimeSeries CV + Embargo, keeping GP discovery, combiner OOF evaluation,
and final holdout strictly separated by ``h + embargo`` temporal gaps.

Import Isolation:
- This ``__init__.py`` deliberately imports ONLY pure-stdlib ``amine.config``
  eagerly and resolves ``AlphaMiningStrategy`` lazily via PEP 562 ``__getattr__``
  so importing ``amine.strategy`` NEVER triggers a ``lightgbm`` import.
"""

from amine.config import (
    AlphaMiningConfig,
    CombinerConfig,
    GPConfig,
    MonitoringConfig,
    PurgedCVConfig,
)

__all__ = [
    "AlphaMiningConfig",
    "AlphaMiningStrategy",
    "CombinerConfig",
    "GPConfig",
    "MonitoringConfig",
    "PurgedCVConfig",
]


def __getattr__(name: str):
    if name == "AlphaMiningStrategy":
        from amine.strategy import AlphaMiningStrategy

        return AlphaMiningStrategy
    raise AttributeError(f"module 'amine' has no attribute {name!r}")
