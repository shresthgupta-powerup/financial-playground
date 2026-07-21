"""Financial-planning simulation service (LP-015 C2, Plan 203 Ph1).

Pure-function service layer — no FastAPI dependency. Accepts a plain dict config
(engine contract, D-P202-1) and returns a plain dict response (D-P203-10).

``simulate_plan(config)`` is the single public entrypoint.

Response shape (D-P203-10):
    {
      "success": bool,
      "retirement_date": "YYYY-MM-DD" | None,
      "age_at_retirement": float | None,
      "snapshot": {core, debt, hybrid, goal_debt, goal_hybrid, total} | None,
      "goals": [{name, pv, fv_at_start, start_date, nature, structure}, ...],
      "wealth_monthly": [{date, total, core, debt, hybrid}, ...],
      "failure": {"date": "YYYY-MM-DD", "reason": str} | None,
    }

Mirrors the logic in the v3 ``streamlit_app.render_results`` and
``render_failure_diagnostics`` functions — adapted from UI rendering to plain-dict
output (D-P203-10).

``PlanValidationError`` is intentionally re-raised to the caller (the route handles
it → HTTP 422).
"""

import pandas as pd

from app.planning.engine import (
    find_retirement_date,
    run_simulation,
    _DEFAULT_INSTRUMENT_PARAMS,
)
from app.planning.glide_paths_repo import load_glide_paths
from app.planning.schemas import RISK_PROFILE_CORE_RETURNS
from app.planning.validation import PlanValidationError  # noqa: F401 — re-exported for route


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ts_to_iso(val) -> str | None:
    """Convert a Timestamp/date/string/None to an ISO date string 'YYYY-MM-DD'."""
    if val is None:
        return None
    try:
        return pd.Timestamp(val).strftime("%Y-%m-%d")
    except Exception:
        return None


def _resolve_instrument_params(config: dict) -> dict:
    """Return the instrument-params dict the engine should use.

    Reads the ``risk_profile`` key from the config (D-P208-4/5):
    - Maps the profile to a ``core_corpus.return`` fraction via
      ``RISK_PROFILE_CORE_RETURNS``.
    - All other engine params (debt/hybrid/goal returns + all tax rates)
      stay at ``_DEFAULT_INSTRUMENT_PARAMS``.
    - Falls back to 'Balanced' (= 0.12, the historical default) when
      ``risk_profile`` is absent or unrecognised.
    """
    profile = config.get("risk_profile", "Balanced") or "Balanced"
    core_return = RISK_PROFILE_CORE_RETURNS.get(profile, RISK_PROFILE_CORE_RETURNS["Balanced"])
    merged = {k: dict(v) for k, v in _DEFAULT_INSTRUMENT_PARAMS.items()}
    merged["core_corpus"]["return"] = core_return
    return merged


def _build_snapshot(comprehensive_df: pd.DataFrame, retirement_date: pd.Timestamp) -> dict | None:
    """Extract wealth snapshot at retirement from the comprehensive_df.

    Mirrors ``streamlit_app.render_results`` snapshot block. Returns None when
    there are no rows >= retirement_date (unexpected but safe).
    """
    df = comprehensive_df.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    snap = df[df["Date"] >= retirement_date].head(1)
    if snap.empty:
        return None

    row = snap.iloc[0]
    core = float(row.get("Core Corpus Value", 0) or 0)
    debt = float(row.get("Debt Pool Value", 0) or 0)
    hybrid = float(row.get("Hybrid Pool Value", 0) or 0)
    goal_debt = float(sum(row.get(c, 0) or 0 for c in row.index if c.endswith(" Debt Value")))
    goal_hybrid = float(sum(row.get(c, 0) or 0 for c in row.index if c.endswith(" Hybrid Value")))
    total = core + debt + hybrid + goal_debt + goal_hybrid
    return {
        "core": round(core, 2),
        "debt": round(debt, 2),
        "hybrid": round(hybrid, 2),
        "goal_debt": round(goal_debt, 2),
        "goal_hybrid": round(goal_hybrid, 2),
        "total": round(total, 2),
    }


def _build_goal_results(config: dict, retirement_date: pd.Timestamp | None) -> list:
    """Build per-goal result rows.

    Mirrors ``streamlit_app.render_results`` goal-status block:
    PV = goal['amount'] in today's rupees; FV at start = PV grown by inflation
    to the resolved start_date.
    """
    current_date = pd.Timestamp(config["current_date"])
    results = []
    for goal in config.get("goals", []) or []:
        if goal.get("start_date_mode", "Fixed").lower() == "at retirement" and retirement_date is not None:
            start = pd.Timestamp(retirement_date)
        elif goal.get("start_date") is not None:
            start = pd.Timestamp(goal["start_date"])
        else:
            start = current_date

        years = max(0.0, (start - current_date).days / 365.25)
        pv = float(goal.get("amount", 0) or 0)
        inflation = float(goal.get("inflation_percent", 0) or 0)
        fv_at_start = pv * ((1 + inflation / 100) ** years)
        results.append({
            "name": goal.get("name", ""),
            "pv": round(pv, 2),
            "fv_at_start": round(fv_at_start, 2),
            "start_date": _ts_to_iso(start),
            "nature": goal.get("nature", ""),
            "structure": goal.get("structure", ""),
        })
    return results


_POOL_VALUE_COLS = frozenset({"Core Corpus Value", "Debt Pool Value", "Hybrid Pool Value"})


def append_csv_summary_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Append derived summary columns to the comprehensive monthly CSV DataFrame.

    Two columns are appended (D-P226-1):
    - "Total Wealth (Rs)": row-sum of every column whose name ends with "Value".
      Mirrors _build_wealth_monthly's value_cols rule so the CSV Total == the
      on-screen Total for the same engine run.
    - "Goal Tranches (Rs)": row-sum of the goal tranche *Value columns only,
      i.e. all *Value columns EXCLUDING the three pool columns
      ("Core Corpus Value", "Debt Pool Value", "Hybrid Pool Value").
      Equivalently: Total Wealth minus the three pool balances.

    Args:
        df: The raw comprehensive_df produced by the engine. Must not be empty.
            A copy is returned; the input is not modified.

    Returns:
        DataFrame with two extra columns appended at the right.
    """
    out = df.copy()
    value_cols = [c for c in out.columns if c.endswith("Value")]
    goal_value_cols = [c for c in value_cols if c not in _POOL_VALUE_COLS]

    def _safe_sum(row, cols):
        return float(sum(row.get(c, 0) or 0 for c in cols))

    out["Total Wealth (Rs)"] = out.apply(lambda row: _safe_sum(row, value_cols), axis=1)
    out["Goal Tranches (Rs)"] = out.apply(lambda row: _safe_sum(row, goal_value_cols), axis=1)
    return out


def _build_wealth_monthly(comprehensive_df: pd.DataFrame, death_date: pd.Timestamp) -> list:
    """Build the monthly wealth table rows up to the target lifetime.

    Mirrors the ``streamlit_app`` wealth breakdown table. Each row:
        {date, total, core, debt, hybrid}
    where ``total`` is the sum of ALL *Value columns.
    """
    df = comprehensive_df.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    df = df[df["Date"] <= death_date]

    value_cols = [c for c in df.columns if c.endswith("Value")]
    rows = []
    for _, row in df.iterrows():
        total = float(sum(row.get(c, 0) or 0 for c in value_cols))
        rows.append({
            "date": row["Date"].strftime("%Y-%m-%d"),
            "total": round(total, 2),
            "core": round(float(row.get("Core Corpus Value", 0) or 0), 2),
            "debt": round(float(row.get("Debt Pool Value", 0) or 0), 2),
            "hybrid": round(float(row.get("Hybrid Pool Value", 0) or 0), 2),
        })
    return rows


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def simulate_plan(config: dict) -> dict:
    """Run the financial-planning simulation and return a plain-dict response.

    Args:
        config: Plain dict matching the engine input schema (5 blocks). Must
            already have ``client_name`` and ``m3_id`` injected by the route
            (D-P203-11). ``instrument_params`` is optional — engine defaults
            apply when absent.

    Returns:
        Plain dict matching the D-P203-10 response shape.

    Raises:
        PlanValidationError: when ``validate_plan_config`` (called inside
            ``find_retirement_date``) rejects the config. The route converts
            this to HTTP 422.
    """
    instrument_params = _resolve_instrument_params(config)
    all_glide_paths = load_glide_paths()

    # -- Step 1: solve for earliest retirement date --
    # find_retirement_date calls validate_plan_config (raises PlanValidationError
    # on bad input) then binary-searches via _solver_search.
    result = find_retirement_date(config, instrument_params, all_glide_paths)

    current_date = pd.Timestamp(config["current_date"])
    current_age = float(config.get("current_age", 30))
    target_lifetime = float(config.get("target_lifetime", 90))
    death_date = current_date + pd.DateOffset(years=int(target_lifetime - current_age))

    if not result["success"]:
        # -- Infeasible path: run diagnostic at death_date --
        # Mirrors streamlit_app.render_failure_diagnostics.
        _success, _ft, failure_details, _pm, _gd, _cd = run_simulation(
            config, pd.Timestamp(death_date), instrument_params, all_glide_paths
        )
        failure_out = None
        if failure_details:
            failure_out = {
                "date": _ts_to_iso(failure_details.get("date")),
                "reason": str(failure_details.get("description", "Corpus depletion")),
            }
        return {
            "success": False,
            "retirement_date": None,
            "age_at_retirement": None,
            "snapshot": None,
            "goals": _build_goal_results(config, retirement_date=None),
            "wealth_monthly": [],
            "failure": failure_out,
        }

    # -- Step 2: success path — re-run at the chosen date for full output --
    retirement_date = pd.Timestamp(result["retirement_date"])
    years_passed = (retirement_date - current_date).days / 365.25
    age_at_retirement = current_age + years_passed

    _success, _ft, _failure, _pm, _gd, comprehensive_df = run_simulation(
        config, retirement_date, instrument_params, all_glide_paths
    )

    snapshot = _build_snapshot(comprehensive_df, retirement_date) if not comprehensive_df.empty else None
    goal_results = _build_goal_results(config, retirement_date)
    wealth_monthly = _build_wealth_monthly(comprehensive_df, death_date)

    return {
        "success": True,
        "retirement_date": _ts_to_iso(retirement_date),
        "age_at_retirement": round(age_at_retirement, 1),
        "snapshot": snapshot,
        "goals": goal_results,
        "wealth_monthly": wealth_monthly,
        "failure": None,
    }
