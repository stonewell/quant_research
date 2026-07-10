import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_QUANT_ROOT = os.path.dirname(_PROJECT_ROOT)
for _path in (_PROJECT_ROOT, _QUANT_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)
