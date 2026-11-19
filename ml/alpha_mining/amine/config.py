"""Validated configuration dataclasses for the ML1 Alpha Mining pipeline.

Academic & Engineering Grounding:
- Purged Group TimeSeries CV + Embargo: Lopez de Prado (2018), *Advances in
  Financial Machine Learning*, ch. 7.
- Two-stage GP formulaic discovery + GBDT non-linear combination with
  multi-objective turnover/correlation/parsimony regularization:
  Kakushadze (2016), Harvey-Liu-Zhu (2016), and ML1 design spec (`docs/development_plan.md` §2.23).

Design Note:
- Default label is ``raw_return`` with ``exec_lag=1`` (next-close execution) to
  avoid same-close execution bias. Residual labels (``market_residual``,
  ``specific_residual``) are opt-in because ``common.specific_alpha`` is a
  simplified offline approximation marked advisory-only.
- ``lowdin_for_combiner`` defaults to ``False`` (tree models are axis-aligned
  and can lose interpretability on rotated bases); ``lowdin_for_gp_library``
  defaults to ``True`` for the linear/equal-weight baseline comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

VALID_LABEL_MODES = ("raw_return", "market_residual", "specific_residual")
VALID_TRAIN_SIDES = ("past", "both")
VALID_OBJECTIVES = ("lambdarank", "regression")
DEFAULT_GP_WINDOWS: Tuple[int, ...] = (3, 5, 10, 20, 40, 60)


@dataclass
class GPConfig:
    """Hyperparameters for Stage-1 typed Genetic Programming factor discovery."""

    population_size: int = 200
    n_generations: int = 20
    tournament_size: int = 7
    crossover_prob: float = 0.7
    mutation_prob: float = 0.3
    elitism_ratio: float = 0.05
    max_depth: int = 6
    max_nodes: int = 30
    top_k: int = 10
    max_abs_corr: float = 0.6
    lambda_turnover: float = 0.5
    lambda_corr: float = 1.0
    lambda_length: float = 0.05
    min_assets: int = 3
    max_nan_fraction: float = 0.5
    max_zero_std_fraction: float = 0.5
    windows: Tuple[int, ...] = DEFAULT_GP_WINDOWS
    seed: int = 42

    def __post_init__(self) -> None:
        if self.population_size < 2:
            raise ValueError(f"GPConfig.population_size must be >= 2, got {self.population_size}")
        if self.n_generations < 1:
            raise ValueError(f"GPConfig.n_generations must be >= 1, got {self.n_generations}")
        if self.tournament_size < 2:
            raise ValueError(f"GPConfig.tournament_size must be >= 2, got {self.tournament_size}")
        if not (0.0 <= self.crossover_prob <= 1.0):
            raise ValueError(f"GPConfig.crossover_prob must be in [0, 1], got {self.crossover_prob}")
        if not (0.0 <= self.mutation_prob <= 1.0):
            raise ValueError(f"GPConfig.mutation_prob must be in [0, 1], got {self.mutation_prob}")
        if not (0.0 <= self.elitism_ratio < 1.0):
            raise ValueError(f"GPConfig.elitism_ratio must be in [0, 1), got {self.elitism_ratio}")
        if self.max_depth < 2:
            raise ValueError(f"GPConfig.max_depth must be >= 2, got {self.max_depth}")
        if self.max_nodes < 3:
            raise ValueError(f"GPConfig.max_nodes must be >= 3, got {self.max_nodes}")
        if self.top_k < 1:
            raise ValueError(f"GPConfig.top_k must be >= 1, got {self.top_k}")
        if not (0.0 < self.max_abs_corr < 1.0):
            raise ValueError(f"GPConfig.max_abs_corr must be in (0, 1), got {self.max_abs_corr}")
        if self.lambda_turnover < 0.0 or self.lambda_corr < 0.0 or self.lambda_length < 0.0:
            raise ValueError("GPConfig fitness penalty lambdas must be >= 0")
        if self.min_assets < 2:
            raise ValueError(f"GPConfig.min_assets must be >= 2, got {self.min_assets}")
        if not (0.0 < self.max_nan_fraction <= 1.0):
            raise ValueError(f"GPConfig.max_nan_fraction must be in (0, 1], got {self.max_nan_fraction}")
        if not (0.0 < self.max_zero_std_fraction <= 1.0):
            raise ValueError(
                f"GPConfig.max_zero_std_fraction must be in (0, 1], got {self.max_zero_std_fraction}"
            )
        if not self.windows or any(int(w) < 2 for w in self.windows):
            raise ValueError(f"GPConfig.windows must contain integers >= 2, got {self.windows}")
        self.windows = tuple(int(w) for w in self.windows)


@dataclass
class PurgedCVConfig:
    """Configuration for Purged Group TimeSeries CV + Embargo."""

    n_splits: int = 5
    embargo_days: int = 5
    train_side: str = "past"
    min_train_dates: int = 20
    min_test_dates: int = 10

    def __post_init__(self) -> None:
        if self.n_splits < 2:
            raise ValueError(f"PurgedCVConfig.n_splits must be >= 2, got {self.n_splits}")
        if self.embargo_days < 0:
            raise ValueError(f"PurgedCVConfig.embargo_days must be >= 0, got {self.embargo_days}")
        if self.train_side not in VALID_TRAIN_SIDES:
            raise ValueError(
                f"PurgedCVConfig.train_side must be one of {VALID_TRAIN_SIDES}, got {self.train_side!r}"
            )
        if self.min_train_dates < 1:
            raise ValueError(f"PurgedCVConfig.min_train_dates must be >= 1, got {self.min_train_dates}")
        if self.min_test_dates < 1:
            raise ValueError(f"PurgedCVConfig.min_test_dates must be >= 1, got {self.min_test_dates}")


@dataclass
class CombinerConfig:
    """Hyperparameters for Stage-2 native LightGBM combiner (no sklearn)."""

    objective: str = "lambdarank"
    max_depth: int = 4
    num_leaves: int = 15
    learning_rate: float = 0.03
    num_boost_round: int = 300
    feature_fraction: float = 0.7
    bagging_fraction: float = 0.8
    bagging_freq: int = 1
    min_child_samples: int = 200
    lambda_l2: float = 1.0
    eval_at: Tuple[int, ...] = (10,)
    n_relevance_bins: int = 5
    deterministic: bool = True
    num_threads: int = 1
    force_col_wise: bool = True
    verbose: int = -1
    seed: int = 42

    def __post_init__(self) -> None:
        if self.objective not in VALID_OBJECTIVES:
            raise ValueError(
                f"CombinerConfig.objective must be one of {VALID_OBJECTIVES}, got {self.objective!r}"
            )
        if self.max_depth < 1:
            raise ValueError(f"CombinerConfig.max_depth must be >= 1, got {self.max_depth}")
        if self.num_leaves < 2:
            raise ValueError(f"CombinerConfig.num_leaves must be >= 2, got {self.num_leaves}")
        if self.learning_rate <= 0.0:
            raise ValueError(f"CombinerConfig.learning_rate must be > 0, got {self.learning_rate}")
        if self.num_boost_round < 1:
            raise ValueError(f"CombinerConfig.num_boost_round must be >= 1, got {self.num_boost_round}")
        if not (0.0 < self.feature_fraction <= 1.0):
            raise ValueError(f"CombinerConfig.feature_fraction must be in (0, 1], got {self.feature_fraction}")
        if not (0.0 < self.bagging_fraction <= 1.0):
            raise ValueError(f"CombinerConfig.bagging_fraction must be in (0, 1], got {self.bagging_fraction}")
        if self.bagging_freq < 0:
            raise ValueError(f"CombinerConfig.bagging_freq must be >= 0, got {self.bagging_freq}")
        if self.min_child_samples < 1:
            raise ValueError(f"CombinerConfig.min_child_samples must be >= 1, got {self.min_child_samples}")
        if self.lambda_l2 < 0.0:
            raise ValueError(f"CombinerConfig.lambda_l2 must be >= 0, got {self.lambda_l2}")
        if self.n_relevance_bins < 2:
            raise ValueError(f"CombinerConfig.n_relevance_bins must be >= 2, got {self.n_relevance_bins}")
        if self.num_threads < 1:
            raise ValueError(f"CombinerConfig.num_threads must be >= 1, got {self.num_threads}")
        self.eval_at = tuple(int(k) for k in self.eval_at)

    def to_lgb_params(self) -> Dict[str, Any]:
        """Build native ``lightgbm.train`` parameter dict."""
        params: Dict[str, Any] = {
            "objective": self.objective,
            "max_depth": self.max_depth,
            "num_leaves": self.num_leaves,
            "learning_rate": self.learning_rate,
            "feature_fraction": self.feature_fraction,
            "bagging_fraction": self.bagging_fraction,
            "bagging_freq": self.bagging_freq,
            "min_child_samples": self.min_child_samples,
            "lambda_l2": self.lambda_l2,
            "deterministic": self.deterministic,
            "num_threads": self.num_threads,
            "force_col_wise": self.force_col_wise,
            "seed": self.seed,
            "feature_fraction_seed": self.seed,
            "bagging_seed": self.seed,
            "data_random_seed": self.seed,
            "verbose": self.verbose,
        }
        if self.objective == "lambdarank":
            params["eval_at"] = list(self.eval_at)
            params["label_gain"] = list(range(self.n_relevance_bins))
        return params

    @classmethod
    def small_data(cls, seed: int = 42, objective: str = "lambdarank") -> "CombinerConfig":
        """Compact profile for fast offline tests and small synthetic universes."""
        return cls(
            objective=objective,
            max_depth=3,
            num_leaves=7,
            learning_rate=0.05,
            num_boost_round=50,
            min_child_samples=5,
            seed=seed,
        )


@dataclass
class MonitoringConfig:
    """Thresholds for report-only production monitoring diagnostics."""

    adv_n_splits: int = 3
    adv_auc_threshold: float = 0.70
    imp_spearman_threshold: float = 0.30
    imp_top3_drop_ratio: float = 0.50
    turnover_window: int = 20
    turnover_threshold: float = 0.30

    def __post_init__(self) -> None:
        if self.adv_n_splits < 2:
            raise ValueError(f"MonitoringConfig.adv_n_splits must be >= 2, got {self.adv_n_splits}")
        if not (0.5 < self.adv_auc_threshold < 1.0):
            raise ValueError(
                f"MonitoringConfig.adv_auc_threshold must be in (0.5, 1), got {self.adv_auc_threshold}"
            )
        if not (-1.0 <= self.imp_spearman_threshold <= 1.0):
            raise ValueError(
                f"MonitoringConfig.imp_spearman_threshold must be in [-1, 1], got {self.imp_spearman_threshold}"
            )
        if not (0.0 < self.imp_top3_drop_ratio <= 1.0):
            raise ValueError(
                f"MonitoringConfig.imp_top3_drop_ratio must be in (0, 1], got {self.imp_top3_drop_ratio}"
            )
        if self.turnover_window < 1:
            raise ValueError(f"MonitoringConfig.turnover_window must be >= 1, got {self.turnover_window}")
        if not (0.0 < self.turnover_threshold <= 1.0):
            raise ValueError(
                f"MonitoringConfig.turnover_threshold must be in (0, 1], got {self.turnover_threshold}"
            )


@dataclass
class AlphaMiningConfig:
    """Top-level configuration for the ML1 GP + LightGBM alpha mining pipeline."""

    holding_period: int = 5
    exec_lag: int = 1
    label_mode: str = "raw_return"
    sector_neutral: bool = False
    sector_map_path: Optional[str] = None
    sector_fallback: str = "auto"
    residual_min_obs: int = 30
    lowdin_for_gp_library: bool = True
    lowdin_for_combiner: bool = False
    discovery_ratio: float = 0.45
    combiner_ratio: float = 0.35
    holdout_ratio: float = 0.20
    top_n: int = 5
    seed: int = 42
    gp: GPConfig = field(default_factory=GPConfig)
    cv: PurgedCVConfig = field(default_factory=PurgedCVConfig)
    combiner: CombinerConfig = field(default_factory=CombinerConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)

    @property
    def label_horizon(self) -> int:
        """Total forward dependency span h = holding_period + exec_lag."""
        return self.holding_period + self.exec_lag

    def __post_init__(self) -> None:
        if self.holding_period < 1:
            raise ValueError(f"AlphaMiningConfig.holding_period must be >= 1, got {self.holding_period}")
        if self.exec_lag < 0:
            raise ValueError(f"AlphaMiningConfig.exec_lag must be >= 0, got {self.exec_lag}")
        if self.label_mode not in VALID_LABEL_MODES:
            raise ValueError(
                f"AlphaMiningConfig.label_mode must be one of {VALID_LABEL_MODES}, got {self.label_mode!r}"
            )
        if self.residual_min_obs < 3:
            raise ValueError(
                f"AlphaMiningConfig.residual_min_obs must be >= 3, got {self.residual_min_obs}"
            )
        if self.top_n < 1:
            raise ValueError(f"AlphaMiningConfig.top_n must be >= 1, got {self.top_n}")
        ratios = (self.discovery_ratio, self.combiner_ratio, self.holdout_ratio)
        if any(r <= 0.0 for r in ratios):
            raise ValueError(f"Timeline split ratios must all be > 0, got {ratios}")
        if abs(sum(ratios) - 1.0) > 1e-6:
            raise ValueError(
                f"Timeline split ratios must sum to 1.0, got {ratios} (sum={sum(ratios):.6f})"
            )

    @classmethod
    def small_data(cls, seed: int = 42) -> "AlphaMiningConfig":
        """Compact profile for fast offline synthetic testing."""
        return cls(
            holding_period=5,
            exec_lag=1,
            residual_min_obs=5,
            seed=seed,
            gp=GPConfig(
                population_size=30,
                n_generations=3,
                tournament_size=4,
                top_k=5,
                seed=seed,
            ),
            cv=PurgedCVConfig(
                n_splits=3,
                embargo_days=3,
                min_train_dates=10,
                min_test_dates=5,
            ),
            combiner=CombinerConfig.small_data(seed=seed),
        )
