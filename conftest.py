"""Root conftest: ensure this worktree's src/ is on sys.path first.

The shared venv's editable install points at /home/pi/pairflow/src.
When tests run inside a worktree, we need *this* worktree's src to take
precedence so changes under development here are what gets tested.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Prepend the worktree's own src/ directory so it shadows the main-repo
# editable install for the duration of this test run.
_src = str(Path(__file__).parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)
