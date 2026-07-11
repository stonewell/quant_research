"""swingbot package bootstrap: makes the shared `common/` package (one level
up from every project in this workspace) importable, since each project runs
in its own separate virtual environment rather than an installed package.
"""
import os
import sys

_QUANT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _QUANT_ROOT not in sys.path:
    sys.path.insert(0, _QUANT_ROOT)
