from __future__ import annotations

import sys
from pathlib import Path

# ``pytest`` is normally launched from apps/api via pyproject.  This fallback
# keeps ``pytest apps/api/tests`` working from the repository root as well.
API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))
