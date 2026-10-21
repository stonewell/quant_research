import os
import sys
import pytest
import pandas as pd
from typing import Dict, Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))  # pipeline/
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from pipeline.research.blend.blend_base import BaseBlendAllocationStrategy
    from pipeline.research.blend.chan_four_state_blend import ChanFourStateBlendStrategy
    from pipeline.research.blend.chan_risk_managed_blend import ChanRiskManagedBlendStrategy
    from pipeline.research.blend.chan_crisis_shield_blend import ChanCrisisShieldBlendStrategy
    from pipeline.research.blend.chan_four_state_shield_blend import ChanFourStateShieldBlendStrategy
    from pipeline.research.blend.chan_dual_hybrid_blend import ChanDualHybridBlendStrategy
    from pipeline.research.blend.generic_blend import GenericBlendStrategy
except ImportError:
    from research.blend.blend_base import BaseBlendAllocationStrategy
    from research.blend.chan_four_state_blend import ChanFourStateBlendStrategy
    from research.blend.chan_risk_managed_blend import ChanRiskManagedBlendStrategy
    from research.blend.chan_crisis_shield_blend import ChanCrisisShieldBlendStrategy
    from research.blend.chan_four_state_shield_blend import ChanFourStateShieldBlendStrategy
    from research.blend.chan_dual_hybrid_blend import ChanDualHybridBlendStrategy
    from research.blend.generic_blend import GenericBlendStrategy


def test_blend_base_versioning_and_properties():
    """Verify BaseBlendAllocationStrategy class-level BASE_VERSION and instance versioning."""
    class DummyBlend(BaseBlendAllocationStrategy):
        def get_sub_strategies(self, universe, params):
            return {}
        def get_sub_strategy_weights(self, params):
            return {}

    b1 = DummyBlend()
    assert b1.BASE_VERSION == "1.3.0"
    assert b1.version == "1.0.0"
    assert b1.base_version == "1.3.0"
    assert b1.version_string == "v1.0.0 [base: v1.3.0]"

    # Subclass with custom version
    class CustomBlend(BaseBlendAllocationStrategy):
        version = "2.0.0"
        def get_sub_strategies(self, universe, params):
            return {}
        def get_sub_strategy_weights(self, params):
            return {}

    b2 = CustomBlend()
    assert b2.version == "2.0.0"
    assert b2.base_version == "1.3.0"
    assert b2.version_string == "v2.0.0 [base: v1.3.0]"

    # Custom version passed at instantiation
    b3 = CustomBlend(version="2.1.0")
    assert b3.version == "2.1.0"
    assert b3.base_version == "1.3.0"
    assert b3.version_string == "v2.1.0 [base: v1.3.0]"


def test_six_institutional_blend_subclasses():
    """Verify all 6 institutional blend subclasses declare version 1.0.0 and introspect sub-strategies."""
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
        assert inst.version == "1.0.0", f"{b_cls.__name__} version should be '1.0.0'"
        assert inst.base_version == "1.3.0"
        assert "v1.0.0 [base: v1.3.0]" in inst.version_string

        v_info = inst.get_version_info()
        assert isinstance(v_info, dict)
        assert v_info["version"] == "1.0.0"
        assert v_info["base_version"] == "1.3.0"
        assert "sub_strategies" in v_info
        assert isinstance(v_info["sub_strategies"], dict)

        # Verify sub-strategies have their own versions
        for sname, sinfo in v_info["sub_strategies"].items():
            assert "class" in sinfo
            assert "version" in sinfo
            assert sinfo["version"] is not None


def test_blend_version_info_structure():
    """Verify detailed structure of ChanFourStateBlendStrategy get_version_info()."""
    strat = ChanFourStateBlendStrategy()
    info = strat.get_version_info()
    assert info["strategy_class"] == "ChanFourStateBlendStrategy"
    assert info["version"] == "1.0.0"
    assert info["base_version"] == "1.3.0"
    assert "chan_four_state" in info["sub_strategies"]
    assert "chan_three_type" in info["sub_strategies"]
    assert "chan_vaa_compound" in info["sub_strategies"]
    assert info["sub_strategies"]["chan_three_type"]["version"] == "1.0.0"
