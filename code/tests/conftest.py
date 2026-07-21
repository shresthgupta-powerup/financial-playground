"""HANDOFF conftest -- NEW file, not the repo's conftest.

The INF_APP_BETA repo's tests/conftest.py boots the whole FastAPI app
(app.main) and mocks both databases; none of that exists here. The three
copied engine-level suites (test_planning_engine / test_planning_oracle /
test_planning_invariants) are pure-computation and only need `app.planning`
and `tests.advisory_corpus` to be importable, so this conftest just puts
the `code/` directory on sys.path.

Run from the handoff root:  pytest code/tests -q
"""

import sys
from pathlib import Path

_CODE_DIR = str(Path(__file__).resolve().parent.parent)  # .../code
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)
