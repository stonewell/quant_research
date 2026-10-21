"""Comprehensive automated test suite for strategy versioning and multi-dimensional reason tracking.

Covers:
1. AllocationTemplate version attribute, get_version_info(), and version_string property.
2. 9 static allocation templates versioning.
3. CompositeAllocationTemplate and CompositeTimingTemplate versioning with aspect metadata.
4. BaseBlendAllocationStrategy and institutional blend subclasses versioning (v1.0.0, base: v1.0.0).
5. Research strategies (ChanThreeTypeStrategy, ChanVaaCompoundStrategy, WorldQuantAlphaStrategy, etc.).
6. instantiate_strategy_from_config_entry version propagation.
7. Live trading ticket JSON structure with multi-dimensional detail_reason and version metadata.
"""

import json
import os
import sys
import tempfile
import pytest
import pandas as pd
import numpy as np

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
for _sub in ("pipeline", "ml"):
    _p = os.path.join(_PROJECT_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from common.allocation_templates import (
    AllocationTemplate,
    EqualWeightAllocation,
    InverseVolatilityAllocation,
    CrossSectionalMomentumAllocation,
    HierarchicalRiskParityAllocation,
    DualMomentumAllocation,
    MaxDiversificationAllocation,
    MeanReversionAllocation,
    MinimumVarianceAllocation,
    BreadthGatedMomentumAllocation,
    PatternBasedAllocationTemplate,
)
from common.strategy_aspects import (
    CompositeAllocationTemplate,
    SELECTION_ASPECTS,
    WEIGHTING_ASPECTS,
)
from pipeline.research.blend.blend_base import BaseBlendAllocationStrategy
from pipeline.research.blend.chan_four_state_blend import ChanFourStateBlendStrategy
from pipeline.research.blend.chan_risk_managed_blend import ChanRiskManagedBlendStrategy
from pipeline.research.blend.chan_crisis_shield_blend import ChanCrisisShieldBlendStrategy
from pipeline.research.blend.chan_four_state_shield_blend import ChanFourStateShieldBlendStrategy
from pipeline.research.blend.chan_dual_hybrid_blend import ChanDualHybridBlendStrategy
from pipeline.research.blend.generic_blend import GenericBlendStrategy
from pipeline.research_strategy.rs.strategy import (
    ChanThreeTypeStrategy,
    instantiate_strategy_from_config_entry,
)
from pipeline.research_strategy.rs.chan_advanced_strategies import (
    ChanVaaCompoundStrategy,
    ChanFourStateExecutionStrategy,
    ChanCompositeStrategy,
)
from pipeline.research_strategy.rs.worldquant_alpha_strategy import (
    WorldQuantAlphaStrategy,
    WorldQuantMegaAlphaStrategy,
)
from pipeline.research_strategy.rs.timing_aspects import (
    CompositeTimingTemplate,
    ENTRY_SIGNAL_ASPECTS,
    EXIT_RISK_ASPECTS,
)
from scripts.run_live_strategy import resolve_strategy_definition


def test_allocation_template_base_versioning():
    """Verify base AllocationTemplate default version, get_version_info(), and version_string."""
    class DummyTemplate(AllocationTemplate):
        def generate_weights(self, universe, params):
            return pd.DataFrame()
        def explain_weights(self, params):
            return "dummy"
        def warmup_bars(self, params):
            return 10

    t1 = DummyTemplate("dummy1", {})
    assert t1.version == "1.0.0"
    assert t1.version_string == "v1.0.0"
    v_info = t1.get_version_info()
    assert isinstance(v_info, dict)
    assert v_info.get("version") == "1.0.0"

    # Custom version initialization
    t2 = DummyTemplate("dummy2", {}, version="2.1.0")
    assert t2.version == "2.1.0"
    assert t2.version_string == "v2.1.0"
    assert t2.get_version_info()["version"] == "2.1.0"


def test_nine_static_templates_versioning():
    """Verify all 9 static allocation templates define version='1.0.0'."""
    static_classes = [
        EqualWeightAllocation,
        InverseVolatilityAllocation,
        CrossSectionalMomentumAllocation,
        HierarchicalRiskParityAllocation,
        DualMomentumAllocation,
        MaxDiversificationAllocation,
        MeanReversionAllocation,
        MinimumVarianceAllocation,
        BreadthGatedMomentumAllocation,
    ]
    for cls in static_classes:
        inst = cls()
        assert inst.version == "1.0.0", f"{cls.__name__} version should be '1.0.0'"
        assert inst.version_string == "v1.0.0"
        v_info = inst.get_version_info()
        assert v_info["version"] == "1.0.0"


def test_composite_templates_versioning():
    """Verify CompositeAllocationTemplate and CompositeTimingTemplate versioning."""
    sel = SELECTION_ASPECTS["all_symbols"]
    wt = WEIGHTING_ASPECTS["inverse_vol"]
    composite = CompositeAllocationTemplate(
        selection=sel,
        weighting=wt,
        version="1.0.0",
    )
    assert composite.version == "1.0.0"
    assert composite.version_string == "v1.0.0"
    comp_info = composite.get_version_info()
    assert comp_info["version"] == "1.0.0"
    assert "aspect_keys" in comp_info
    assert comp_info["selection_aspect"] == "all_symbols"
    assert comp_info["weighting_aspect"] == "inverse_vol"

    # CompositeTimingTemplate
    entry_asp = ENTRY_SIGNAL_ASPECTS["rsi_oversold_entry"]
    exit_asp = EXIT_RISK_ASPECTS["turtle_atr_trailing_exit"]
    timing_comp = CompositeTimingTemplate(
        entry=entry_asp,
        exit_=exit_asp,
        version="1.0.0",
    )
    assert timing_comp.version == "1.0.0"
    t_info = timing_comp.get_version_info()
    assert t_info["version"] == "1.0.0"
    assert t_info["entry_aspect"] == "rsi_oversold_entry"
    assert t_info["exit_aspect"] == "turtle_atr_trailing_exit"


def test_blend_strategies_versioning():
    """Verify BaseBlendAllocationStrategy and subclasses carry base_version and version."""
    blend_classes = [
        ChanFourStateBlendStrategy,
        ChanRiskManagedBlendStrategy,
        ChanCrisisShieldBlendStrategy,
        ChanFourStateShieldBlendStrategy,
        ChanDualHybridBlendStrategy,
        GenericBlendStrategy,
    ]
    for b_cls in blend_classes:
        inst = b_cls()
        assert inst.BASE_VERSION == "1.4.0"
        assert inst.version == "1.0.0"
        assert inst.base_version == "1.4.0"
        assert "v1.0.0 [base: v1.4.0]" in inst.version_string
        v_info = inst.get_version_info()
        assert v_info["version"] == "1.0.0"
        assert v_info["base_version"] == "1.4.0"
        assert "sub_strategies" in v_info
        assert len(v_info["sub_strategies"]) > 0


def test_research_strategies_versioning():
    """Verify research strategy classes have version='1.0.0'."""
    strats = [
        ChanThreeTypeStrategy(),
        ChanVaaCompoundStrategy(),
        ChanFourStateExecutionStrategy(),
        ChanCompositeStrategy(),
        WorldQuantAlphaStrategy(),
        WorldQuantMegaAlphaStrategy(),
    ]
    for s in strats:
        assert getattr(s, "version", None) == "1.0.0", f"{type(s).__name__} should have version 1.0.0"
        assert s.version_string == "v1.0.0"


def test_instantiate_strategy_from_config_entry():
    """Verify version propagation from config entry_data to instantiated strategy."""
    entry_data = {
        "class_name": "ChanThreeTypeStrategy",
        "module": "pipeline.research_strategy.rs.strategy",
        "name": "Custom Chan Strategy",
        "version": "2.3.4",
        "base_version": "1.0.0",
        "default_params": {"fast_period": 5},
    }
    inst = instantiate_strategy_from_config_entry("custom_chan", entry_data)
    assert inst is not None
    assert inst.version == "2.3.4"
    assert getattr(inst, "base_version", None) == "1.0.0"
    v_info = inst.get_version_info()
    assert v_info["version"] == "2.3.4"


def test_live_ticket_detail_reason_and_version_structure():
    """Verify live_trading_ticket.json serialization contains detail_reason and version fields."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_ticket_json = os.path.join(tmp_dir, "live_trading_ticket.json")
        out_account_state = os.path.join(tmp_dir, "account_state.json")
        universe_file = os.path.join(_PROJECT_ROOT, "docs", "universe", "china", "core_satellite_22_stocks.txt")

        # Run live strategy check via python runner
        cmd = (
            f"pipeline/.venv/bin/python scripts/run_live_strategy.py "
            f"--strategy equal_weight --data-provider synthetic "
            f"--universe-file {universe_file} "
            f"--output-dir {tmp_dir} --account-state-file {out_account_state} "
            f"--lookback-days 300"
        )
        ret = os.system(cmd)
        assert ret == 0, f"run_live_strategy.py command failed with code {ret}"

        assert os.path.exists(out_ticket_json), "live_trading_ticket.json was not created"
        with open(out_ticket_json, "r", encoding="utf-8") as f:
            tickets = json.load(f)

        assert isinstance(tickets, list)
        assert len(tickets) > 0

        # Inspect ticket structure
        first_ticket = tickets[0]
        assert "symbol" in first_ticket
        assert "action" in first_ticket
        assert "shares" in first_ticket
        assert "summary_reason" in first_ticket
        assert "strategy_version" in first_ticket
        assert first_ticket["strategy_version"] == "1.0.0"
        assert "detail_reason" in first_ticket

        # Inspect detail_reason schema
        dr = first_ticket["detail_reason"]
        assert isinstance(dr, dict)
        required_keys = [
            "summary_reason",
            "dimension_signals",
            "inertia_check",
            "holding_lock",
            "cooldown_status",
            "sector_throttle",
            "macro_gate",
            "lot_rounding",
        ]
        for k in required_keys:
            assert k in dr, f"detail_reason missing key: {k}"

        # Check sub-schemas
        assert "delta_weight" in dr["inertia_check"]
        assert "threshold" in dr["inertia_check"]
        assert "triggered" in dr["inertia_check"]

        assert "bars_held" in dr["holding_lock"]
        assert "min_holding_bars" in dr["holding_lock"]
        assert "locked" in dr["holding_lock"]

        assert "bars_since_exit" in dr["cooldown_status"]
        assert "in_cooldown" in dr["cooldown_status"]

        assert "sector" in dr["sector_throttle"]
        assert "max_allowed" in dr["sector_throttle"]
        assert "current_sector_holdings" in dr["sector_throttle"]
        assert "throttled" in dr["sector_throttle"]

        assert "passed" in dr["macro_gate"]
        assert "breadth" in dr["macro_gate"]
        assert "thrust" in dr["macro_gate"]

        assert "target_shares_raw" in dr["lot_rounding"]
        assert "shares" in dr["lot_rounding"]
        assert "lot_size" in dr["lot_rounding"]

        # Verify account_state.json contains version info
        assert os.path.exists(out_account_state), "account_state.json was not created"
        with open(out_account_state, "r", encoding="utf-8") as f:
            acc_state = json.load(f)
        assert "strategy_version" in acc_state
        assert acc_state["strategy_version"] == "1.0.0"
        assert "strategy_version_info" in acc_state
