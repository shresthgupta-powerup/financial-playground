"""Financial-planning simulation engine (LP-015 C1).

Ported near-verbatim from the standalone sales-meeting simulator
``C:\\Punit Patel\\Financial Planning v3\\main_v2.py`` at commit
``1515f1e`` (branch ``feature/income-model-rework``). See
``.context/modules/financial-planning.md`` for input + solver logic and the
port's source-of-truth references.

Public entrypoints:
    - ``find_retirement_date(config, instrument_params=None, glide_paths=None)``
      — binary-search solver for the earliest feasible retirement date.
    - ``run_simulation(config, retirement_date, instrument_params, glide_paths=None)``
      — single deterministic simulation for a candidate retirement date.
    - ``validate_plan_config(config)`` — server-side input validation
      (raises ``PlanValidationError``).
    - ``get_glide_paths()`` + ``GLIDEPATH_VERSION`` — checked-in glide-path data.

The engine consumes a plain dict config (no Pydantic) and is UI-/framework-agnostic.
"""

from .glide_paths import GLIDEPATH_VERSION, get_glide_paths
from .validation import PlanValidationError, validate_plan_config, MAX_NONREPLENISHING_SPAN_MONTHS
from .engine import (
    ENGINE_SOURCE_SHA,
    find_retirement_date,
    run_simulation,
    TaxLot,
    InvestmentPool,
)

__all__ = [
    "GLIDEPATH_VERSION",
    "get_glide_paths",
    "PlanValidationError",
    "validate_plan_config",
    "MAX_NONREPLENISHING_SPAN_MONTHS",
    "ENGINE_SOURCE_SHA",
    "find_retirement_date",
    "run_simulation",
    "TaxLot",
    "InvestmentPool",
]
