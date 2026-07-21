"""Pydantic request/response models for the financial-planning API (LP-015 C2).

Kept in a dedicated module (D-P203-10) — the large nested plan schemas stay out
of the shared ``app/schemas.py``. Engine stays dict-based (D-P202-1); the route
converts a validated Pydantic model → plain dict before calling the engine.

Date serialisation follows the existing ISO convention (``date`` fields → ISO8601
string; Timestamps inside service code stay as ``pd.Timestamp`` until the service
serialises them to strings for the response).

Month-grid invariant (D-P223-2/3, Plan 223): all input dates are silently coerced
to ``day=1`` at this boundary. ``current_date`` snaps to the 1st of the current
month. A hand-crafted API call with ``day != 1`` is coerced, not rejected, so any
legacy/future input still loads. The engine adds a second defensive normalisation
at its own entry.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Risk-profile enum + canonical core-corpus return mapping (D-P208-4)
# ---------------------------------------------------------------------------

# The 5 risk profiles. Default is 'Balanced' (= the historical 12% default).
RiskProfileLiteral = Literal[
    "Very Conservative",
    "Conservative",
    "Balanced",
    "Aggressive",
    "Very Aggressive",
]

# Canonical mapping: risk_profile -> core_corpus.return (as fraction).
# Drives core_corpus.return ONLY; all other engine params stay at their defaults.
RISK_PROFILE_CORE_RETURNS: Dict[str, float] = {
    "Very Conservative": 0.08,
    "Conservative": 0.10,
    "Balanced": 0.12,
    "Aggressive": 0.135,
    "Very Aggressive": 0.15,
}


# ---------------------------------------------------------------------------
# Request models — the 5 input blocks (mirrors engine config schema)
# ---------------------------------------------------------------------------

def _snap_to_month_start(d) -> Optional[date]:
    """Coerce a date(-like) value to the 1st of its month. ``None`` passes through.

    Implements D-P223-2: all user-supplied input dates are silently snapped to
    ``day=1`` at the Pydantic schema boundary. Because this runs in ``mode="before"``
    the input may be a raw string, a ``datetime.date``, or any other supported type
    — we normalise it to a ``datetime.date`` first, then replace the day.
    The engine adds a second defensive normalisation at its own entry.
    """
    if d is None:
        return None
    # Normalise to datetime.date via string parsing to handle ISO strings,
    # datetime.date, datetime.datetime, and pandas Timestamp transparently.
    if isinstance(d, str):
        # Pydantic calls this validator before coercion, so raw strings arrive here.
        from datetime import date as _date_type
        parsed = _date_type.fromisoformat(d[:10])  # accept "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM:SS..."
        return parsed.replace(day=1)
    if isinstance(d, date):
        return d.replace(day=1)
    # Fallback: let Pydantic handle coercion; return unchanged if we can't snap.
    try:
        import datetime as _dt
        if hasattr(d, "year"):
            return _dt.date(d.year, d.month, 1)
    except Exception:
        pass
    return d


class InvestmentStreamInput(BaseModel):
    name: str
    amount: float
    start_date: date
    end_date_mode: str = "At retirement"       # "At retirement" | "Fixed"
    end_date: Optional[date] = None
    step_up_percent: float = 10.0
    step_up_frequency: str = "Annual"
    step_up_date: Optional[date] = None

    # Month-grid invariant (D-P223-2): coerce all date fields to day=1.
    @field_validator("start_date", mode="before")
    @classmethod
    def _snap_start_date(cls, v):
        return _snap_to_month_start(v) if v is not None else v

    @field_validator("end_date", mode="before")
    @classmethod
    def _snap_end_date(cls, v):
        return _snap_to_month_start(v) if v is not None else v

    @field_validator("step_up_date", mode="before")
    @classmethod
    def _snap_step_up_date(cls, v):
        return _snap_to_month_start(v) if v is not None else v


class GoalInput(BaseModel):
    name: str
    description: str = ""
    type: str = "Non-Negotiable"               # glide-path sheet key
    nature: str = "Non-replenishing"           # "Non-replenishing" | "Replenishing"
    structure: str = "Lumpsum"                 # "Lumpsum" | "Recurring"
    start_date_mode: str = "Fixed"             # "Fixed" | "At retirement"
    start_date: Optional[date] = None
    amount: float
    frequency: Optional[str] = None           # "Monthly" | "Quarterly" | "Half-Yearly" | "Annual"
    occurrences: Optional[int] = None
    end_mode: Optional[str] = None            # "Occurrences" | "Fixed date" | "Lifetime"
    end_date: Optional[date] = None
    inflation_percent: float = 6.0

    # Month-grid invariant (D-P223-2): coerce all date fields to day=1.
    @field_validator("start_date", mode="before")
    @classmethod
    def _snap_start_date(cls, v):
        return _snap_to_month_start(v) if v is not None else v

    @field_validator("end_date", mode="before")
    @classmethod
    def _snap_end_date(cls, v):
        return _snap_to_month_start(v) if v is not None else v


class OneTimeInvestmentInput(BaseModel):
    name: str
    date: date
    amount: float

    # Month-grid invariant (D-P223-2): coerce date to day=1.
    @field_validator("date", mode="before")
    @classmethod
    def _snap_date(cls, v):
        return _snap_to_month_start(v) if v is not None else v


class PlanSimulateRequest(BaseModel):
    """Full plan configuration — mirrors the 5 engine input blocks.

    ``client_name`` and ``m3_id`` are injected by the route from the family
    record (D-P203-11); they are not sent by the caller.

    ``risk_profile`` selects the core-corpus return assumption (D-P208-4/5).
    The service maps profile -> instrument_params; the engine signature is
    unchanged.  ``instrument_params`` is no longer accepted on the public
    API (D-P208-5) — any field named ``instrument_params`` in the request
    body is ignored by Pydantic (it is not declared here).

    Month-grid invariant (D-P223-2/3): ``current_date`` is coerced to the 1st
    of its month. All nested date fields are coerced by their own model validators.
    """
    current_date: date
    current_age: float
    target_lifetime: float
    current_corpus: float

    investment_streams: List[InvestmentStreamInput] = Field(default_factory=list)
    goals: List[GoalInput] = Field(default_factory=list)
    one_time_investments: List[OneTimeInvestmentInput] = Field(default_factory=list)
    risk_profile: RiskProfileLiteral = "Balanced"

    model_config = {"extra": "ignore"}   # silently drop unknown fields (e.g. legacy instrument_params)

    # Month-grid invariant (D-P223-3): current_date snaps to the 1st of its month.
    @field_validator("current_date", mode="before")
    @classmethod
    def _snap_current_date(cls, v):
        return _snap_to_month_start(v) if v is not None else v


# ---------------------------------------------------------------------------
# Response models (D-P203-10)
# ---------------------------------------------------------------------------

class SnapshotAtRetirement(BaseModel):
    """Wealth snapshot at the earliest retirement date."""
    core: float
    debt: float
    hybrid: float
    goal_debt: float
    goal_hybrid: float
    total: float


class GoalResult(BaseModel):
    name: str
    pv: float                    # today's rupees (goal['amount'])
    fv_at_start: float           # PV grown to goal start_date via inflation
    start_date: Optional[str]    # ISO date string
    nature: str
    structure: str


class WealthMonthlyRow(BaseModel):
    date: str                    # ISO date string "YYYY-MM-DD"
    total: float
    core: float
    debt: float
    hybrid: float


class FailureDiagnostic(BaseModel):
    date: str                    # ISO date string
    reason: str


class PlanSimulateResponse(BaseModel):
    success: bool
    retirement_date: Optional[str]       # ISO date string; None when infeasible
    age_at_retirement: Optional[float]   # None when infeasible
    snapshot: Optional[SnapshotAtRetirement] = None
    goals: List[GoalResult] = Field(default_factory=list)
    wealth_monthly: List[WealthMonthlyRow] = Field(default_factory=list)
    failure: Optional[FailureDiagnostic] = None


# ---------------------------------------------------------------------------
# Save / load response models (C4 -- Plan 206 Ph2)
# ---------------------------------------------------------------------------

class PlanSaveResponse(BaseModel):
    """Response from POST /api/families/{id}/plan (save endpoint).

    ``saved=True``:  a new row was inserted; version_no / created_at are set.
    ``saved=False``: the simulation ran but the result was infeasible
                     (success=False) -- D-P206-4 feasible-only gate. The
                     simulate result is still returned so the frontend can
                     display the infeasible diagnostic.
    """
    saved: bool
    version_no: Optional[int] = None           # None when saved=False
    created_at: Optional[str] = None           # ISO datetime string; None when saved=False
    engine_version: Optional[str] = None       # None when saved=False
    glidepath_version: Optional[int] = None    # None when saved=False
    # Simulation result (always present -- either the infeasible diagnostic or
    # the feasible result that was also persisted).
    result: Dict[str, Any]


class PlanLatestResponse(BaseModel):
    """Response from GET /api/families/{id}/plan (load-latest endpoint).

    Returned when the family has at least one saved plan.
    HTTP 204 is returned directly (no body) when there is no plan.
    """
    version_no: int
    created_at: str          # ISO datetime string "YYYY-MM-DDTHH:MM:SS+00:00"
    engine_version: str
    glidepath_version: int
    inputs: Dict[str, Any]   # stored inputs_json
    results: Dict[str, Any]  # stored results_json
