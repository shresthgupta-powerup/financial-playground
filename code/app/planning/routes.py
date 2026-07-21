"""Financial-planning API routes (LP-015 C2+C4, Plan 203+206).

Endpoints (D-P203-11, D-P206-3/5, D-P211-3):
  POST /api/families/{infinite_id}/plan/simulate          -- synchronous simulation (C2)
  POST /api/families/{infinite_id}/plan/export            -- advisor Excel download (C2; +glide/pool sheets Plan 211)
  POST /api/families/{infinite_id}/plan/comprehensive-csv -- monthly view CSV (Plan 211)
  POST /api/families/{infinite_id}/plan                   -- save plan (C4, D-P206-3)
  GET  /api/families/{infinite_id}/plan                   -- load latest plan (C4, D-P206-5)

All endpoints:
  - Verify family existence via PG (404 if not found).
  - Inject ``client_name``/``m3_id`` from the family record into the engine config.
  - Convert validated Pydantic -> plain dict before calling engine/service (D-P202-1).
  - Catch ``PlanValidationError`` -> HTTP 422 with ``{"errors": [...]}`` (D-P203-10).

Save endpoint (D-P206-3): re-runs simulation server-side (authoritative); persists
only when the result is feasible (success=True, D-P206-4); stamps engine_version +
glidepath_version.

Load-latest endpoint (D-P206-5): returns MAX(version_no) row, or HTTP 204 when
the family has no saved plan.

``simulate`` and ``save`` are synchronous ``def`` routes (D-P203-1): FastAPI
offloads them to the threadpool, keeping the event loop free for the engine call.
"""

import io
import json
import logging
from datetime import date as _date

import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, JSONResponse

from app.database_pg import execute_query_pg
from app.planning.engine import ENGINE_SOURCE_SHA
from app.planning.glide_paths_repo import load_glide_paths, get_current_glidepath_version
from app.planning.plans_repo import insert_plan_version, get_latest_plan
from app.planning.schemas import PlanSimulateRequest, PlanSimulateResponse
from app.planning.service import simulate_plan, append_csv_summary_cols
from app.planning.validation import PlanValidationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/families", tags=["planning"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_family_or_404(infinite_id: str) -> dict:
    """Fetch family name from PG. Raises HTTP 404 when not found.

    Uses the standard PG-existence pattern
    (``SELECT ... FROM inf_families WHERE infinite_id = %s``).
    """
    rows = execute_query_pg(
        "SELECT family_name FROM inf_families WHERE infinite_id = %s",
        (infinite_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Family '{infinite_id}' not found")
    return rows[0]


def _request_to_config(req: PlanSimulateRequest, infinite_id: str, family_name: str) -> dict:
    """Convert a validated PlanSimulateRequest to an engine plain-dict config.

    Injects ``client_name`` (family_name) and ``m3_id`` (infinite_id) per D-P203-11.
    Dates serialised to pd.Timestamp for the engine (engine expects Timestamp/date).
    """
    def _ts(d):
        return pd.Timestamp(d) if d is not None else None

    streams = [
        {
            "name": s.name,
            "amount": s.amount,
            "start_date": _ts(s.start_date),
            "end_date_mode": s.end_date_mode,
            "end_date": _ts(s.end_date),
            "step_up_percent": s.step_up_percent,
            "step_up_frequency": s.step_up_frequency,
            "step_up_date": _ts(s.step_up_date) if s.step_up_date else _ts(s.start_date),
        }
        for s in req.investment_streams
    ]

    goals = [
        {
            "name": g.name,
            "description": g.description,
            "type": g.type,
            "nature": g.nature,
            "structure": g.structure,
            "start_date_mode": g.start_date_mode,
            "start_date": _ts(g.start_date),
            "amount": g.amount,
            "frequency": g.frequency,
            "occurrences": g.occurrences,
            "end_mode": g.end_mode,
            "end_date": _ts(g.end_date),
            "inflation_percent": g.inflation_percent,
        }
        for g in req.goals
    ]

    one_time = [
        {
            "name": w.name,
            "date": _ts(w.date),
            "amount": w.amount,
        }
        for w in req.one_time_investments
    ]

    return {
        "current_date": _ts(req.current_date),
        "current_age": req.current_age,
        "target_lifetime": req.target_lifetime,
        "current_corpus": req.current_corpus,
        "investment_streams": streams,
        "goals": goals,
        "one_time_investments": one_time,
        # risk_profile rides the config dict → persists in inputs_json (D-P208-6).
        "risk_profile": req.risk_profile,
        "client_name": family_name,
        "m3_id": infinite_id,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/{infinite_id}/plan/simulate")
def simulate_endpoint(infinite_id: str, body: PlanSimulateRequest):
    """POST /api/families/{id}/plan/simulate — synchronous simulation (D-P203-1).

    Returns the D-P203-10 response shape on success; 422 on invalid config;
    404 on unknown family.
    """
    fam = _get_family_or_404(infinite_id)
    family_name = fam.get("FAMILY_NAME", "")

    config = _request_to_config(body, infinite_id, family_name)

    try:
        result = simulate_plan(config)
    except PlanValidationError as e:
        raise HTTPException(status_code=422, detail={"errors": e.errors})
    except Exception:
        logger.exception("Unexpected error in simulate_endpoint infinite_id=%s", infinite_id)
        raise

    return result


def _simulate_for_export(infinite_id: str, body: PlanSimulateRequest):
    """Shared run block for the export + comprehensive-csv endpoints (Plan 211).

    404/422 parity with the other endpoints. Returns
    ``(config, result, comprehensive_df, goal_dfs, pool_movements_df, retirement_date)``.
    """
    from app.planning.engine import find_retirement_date, run_simulation
    from app.planning.service import _resolve_instrument_params

    fam = _get_family_or_404(infinite_id)
    family_name = fam.get("FAMILY_NAME", "")

    config = _request_to_config(body, infinite_id, family_name)

    try:
        instrument_params = _resolve_instrument_params(config)
        all_glide_paths = load_glide_paths()
        result = find_retirement_date(config, instrument_params, all_glide_paths)
    except PlanValidationError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})
    except Exception:
        logger.exception("Unexpected error in export find_retirement infinite_id=%s", infinite_id)
        raise

    # Re-run the simulation at the solved date (or death_date for diagnostics).
    retirement_date = pd.Timestamp(result.get("retirement_date")) if result.get("retirement_date") else None
    current_date = pd.Timestamp(config["current_date"])
    target_lifetime = float(config.get("target_lifetime", 90))
    current_age = float(config.get("current_age", 30))
    death_date = current_date + pd.DateOffset(years=int(target_lifetime - current_age))
    run_date = retirement_date if retirement_date is not None else pd.Timestamp(death_date)

    try:
        _success, _ft, _failure, pool_movements_df, goal_dfs, comprehensive_df = run_simulation(
            config, run_date, instrument_params, all_glide_paths
        )
    except Exception:
        logger.exception("Unexpected error in export run_simulation infinite_id=%s", infinite_id)
        raise

    return config, result, comprehensive_df, goal_dfs, pool_movements_df, retirement_date


@router.post("/{infinite_id}/plan/export")
def export_endpoint(infinite_id: str, body: PlanSimulateRequest):
    """POST /api/families/{id}/plan/export — stateless advisor Excel download (D-P203-12).

    Runs the full simulation then builds the advisor workbook (incl. the Plan 211
    "Goal Glide Paths" + "Pool Movements" sheets). Returns xlsx bytes with
    ``Content-Disposition: attachment``. 422 on invalid config; 404 on unknown family.
    """
    from app.planning.advisor_export import build_advisor_workbook
    from app.planning.service import _build_snapshot

    (config, result, comprehensive_df, goal_dfs,
     pool_movements_df, retirement_date) = _simulate_for_export(infinite_id, body)

    snapshot = None
    if retirement_date is not None and not comprehensive_df.empty:
        snapshot = _build_snapshot(comprehensive_df, retirement_date)
        if snapshot:
            # Convert to the format advisor_export expects (key-value pairs)
            snapshot = {
                "Core Corpus (₹)": snapshot["core"],
                "Debt Pool (₹)": snapshot["debt"],
                "Hybrid Pool (₹)": snapshot["hybrid"],
                "Goal Debt Tranches (₹)": snapshot["goal_debt"],
                "Goal Hybrid Tranches (₹)": snapshot["goal_hybrid"],
                "Total Wealth (₹)": snapshot["total"],
            }

    # Build workbook (Plan 211: + glide-path / pool-movement sheets)
    export_bytes = build_advisor_workbook(
        config,
        result,
        comprehensive_df=comprehensive_df if not comprehensive_df.empty else None,
        snapshot=snapshot,
        goal_dfs=goal_dfs,
        pool_movements_df=pool_movements_df,
    )

    # Filename: financial_plan_{infinite_id}_{YYYYMM}.xlsx
    yyyymm = _date.today().strftime("%Y%m")
    filename = f"financial_plan_{infinite_id}_{yyyymm}.xlsx"

    return Response(
        content=export_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/{infinite_id}/plan/comprehensive-csv")
def comprehensive_csv_endpoint(infinite_id: str, body: PlanSimulateRequest):
    """POST /api/families/{id}/plan/comprehensive-csv — monthly view as CSV (Plan 211, D-P211-3).

    Same request body as export; re-runs the simulation and streams the
    comprehensive month-by-month DataFrame (the v3 "Comprehensive monthly view")
    as ``text/csv``. 422 on invalid config; 404 on unknown family.

    Infeasible configs are served too (Plan 249, D-P249-2): the diagnostic run
    at death_date produces a full monthly view for the Core-Corpus-depletion
    failure class. The debt-pool-depletion class early-returns an EMPTY
    comprehensive view from the engine -- that case gets HTTP 409 (never a
    blank file). 422 stays reserved for PlanValidationError.
    """
    config, _result, comprehensive_df, _gd, _pm, _rd = _simulate_for_export(infinite_id, body)

    if comprehensive_df is None or comprehensive_df.empty:
        # D-P249-2: pool-depletion failures abort the engine run before any
        # monthly view is built. A zero-byte CSV download is never useful.
        raise HTTPException(
            status_code=409,
            detail=(
                "Monthly view unavailable for this failure mode "
                "(pool depletion before any monthly view could be built)"
            ),
        )

    # Append derived summary columns before serialising (D-P226-1).
    # comprehensive_df is guaranteed non-empty here (the 409 guard above).
    csv_df = append_csv_summary_cols(comprehensive_df)
    csv_text = csv_df.to_csv(index=False)

    yyyymm = _date.today().strftime("%Y%m")
    filename = f"financial_plan_monthly_{infinite_id}_{yyyymm}.csv"

    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/{infinite_id}/plan")
def save_plan_endpoint(infinite_id: str, body: PlanSimulateRequest):
    """POST /api/families/{id}/plan -- save plan (LP-015 C4, D-P206-3).

    Re-runs the simulation server-side (authoritative -- D-P206-3). Persists
    a new version row only when the result is feasible (success=True, D-P206-4).
    Infeasible results are returned to the FE with ``saved=False`` so the
    lifecycle UI can display the diagnostic without saving.

    Returns:
        200 + PlanSaveResponse with saved=True (feasible) or saved=False (infeasible).
        422 on validation error (PlanValidationError -- bad inputs).
        404 on unknown family.
    """
    fam = _get_family_or_404(infinite_id)
    family_name = fam.get("FAMILY_NAME", "")

    config = _request_to_config(body, infinite_id, family_name)

    try:
        result = simulate_plan(config)
    except PlanValidationError as e:
        raise HTTPException(status_code=422, detail={"errors": e.errors})
    except Exception:
        logger.exception("Unexpected error in save_plan_endpoint simulate infinite_id=%s", infinite_id)
        raise

    if not result.get("success"):
        # Infeasible result -- D-P206-4: return the diagnostic without saving.
        return {"saved": False, "result": result}

    # Feasible -- persist and stamp versions.
    # inputs_json = the request body as a serialisable dict (ISO-date fields).
    inputs_dict = json.loads(body.model_dump_json())
    engine_ver = ENGINE_SOURCE_SHA
    glide_ver = get_current_glidepath_version()

    row = insert_plan_version(
        infinite_id=infinite_id,
        inputs_json=inputs_dict,
        results_json=result,
        engine_version=engine_ver,
        glidepath_version=glide_ver,
    )

    created_at_str = (
        row["CREATED_AT"].isoformat() if hasattr(row["CREATED_AT"], "isoformat") else str(row["CREATED_AT"])
    )

    return {
        "saved": True,
        "version_no": row["VERSION_NO"],
        "created_at": created_at_str,
        "engine_version": row["ENGINE_VERSION"],
        "glidepath_version": row["GLIDEPATH_VERSION"],
        "result": result,
    }


@router.get("/{infinite_id}/plan")
def load_plan_endpoint(infinite_id: str):
    """GET /api/families/{id}/plan -- load latest saved plan (LP-015 C4, D-P206-5).

    Returns the MAX(version_no) row for the family, or HTTP 204 No Content
    when the family has no saved plan.

    Returns:
        200 + PlanLatestResponse with version_no, created_at, engine_version,
            glidepath_version, inputs (inputs_json), results (results_json).
        204 when the family has no saved plan.
        404 on unknown family.
    """
    _get_family_or_404(infinite_id)

    row = get_latest_plan(infinite_id)
    if row is None:
        return Response(status_code=204)

    created_at_str = (
        row["CREATED_AT"].isoformat() if hasattr(row["CREATED_AT"], "isoformat") else str(row["CREATED_AT"])
    )

    # INPUTS_JSON and RESULTS_JSON may be returned as dicts (psycopg2 JSONB) or
    # as JSON strings depending on the psycopg2 version. Normalise to dict.
    inputs = row["INPUTS_JSON"]
    results = row["RESULTS_JSON"]
    if isinstance(inputs, str):
        inputs = json.loads(inputs)
    if isinstance(results, str):
        results = json.loads(results)

    return {
        "version_no": row["VERSION_NO"],
        "created_at": created_at_str,
        "engine_version": row["ENGINE_VERSION"],
        "glidepath_version": row["GLIDEPATH_VERSION"],
        "inputs": inputs,
        "results": results,
    }
