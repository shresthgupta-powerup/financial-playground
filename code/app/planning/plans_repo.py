"""Plan persistence repository (LP-015 C4, Plan 206 Ph2).

Provides two public functions for the ``inf_financial_plans`` table:

- ``insert_plan_version`` -- compute version_no = COALESCE(MAX,0)+1 for the
  family, INSERT the new row, and return the inserted row (D-P206-2).
- ``get_latest_plan`` -- return the MAX-version_no row for a family, or None
  when the family has no saved plans (D-P206-5).

Design decisions in effect:
  D-P206-1 -- ``inf_financial_plans``: BIGSERIAL PK; FK to inf_families;
               UNIQUE (infinite_id, version_no); index (infinite_id, version_no DESC).
  D-P206-2 -- version_no = COALESCE(MAX(version_no), 0) + 1 per family at
               INSERT time (read-then-insert; a concurrent double-save yields
               two consecutive versions -- UNIQUE guards against corruption).
  D-P206-5 -- load-latest = ORDER BY version_no DESC LIMIT 1.

Both functions use ``execute_returning_pg`` (RETURNING clause) or
``execute_query_pg`` (SELECT) from ``app.database_pg`` -- no direct psycopg2
import here.

Column name note: ``execute_query_pg`` / ``execute_returning_pg`` return
UPPERCASE-key dicts (D24 convention; see database_pg.py). The returned dict
keys are therefore ID, INFINITE_ID, VERSION_NO, etc.
"""

import json
import logging

from app.database_pg import execute_query_pg, execute_returning_pg

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_NEXT_VERSION_NO_SQL = """
    SELECT COALESCE(MAX(version_no), 0) + 1 AS next_version
    FROM inf_financial_plans
    WHERE infinite_id = %s
"""

_INSERT_PLAN_SQL = """
    INSERT INTO inf_financial_plans
        (infinite_id, version_no, inputs_json, results_json,
         engine_version, glidepath_version)
    VALUES (%s, %s, %s, %s, %s, %s)
    RETURNING
        id, infinite_id, version_no, inputs_json, results_json,
        engine_version, glidepath_version, created_at, created_by
"""

_GET_LATEST_PLAN_SQL = """
    SELECT
        id, infinite_id, version_no, inputs_json, results_json,
        engine_version, glidepath_version, created_at, created_by
    FROM inf_financial_plans
    WHERE infinite_id = %s
    ORDER BY version_no DESC
    LIMIT 1
"""


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def insert_plan_version(
    infinite_id: str,
    inputs_json: dict,
    results_json: dict,
    engine_version: str,
    glidepath_version: int,
) -> dict:
    """Insert a new plan version row and return the inserted row.

    Args:
        infinite_id:       Family identifier (FK to inf_families).
        inputs_json:       Plain-dict serialisable ``PlanSimulateRequest``
                           inputs re-run by the server (D-P206-3).
        results_json:      Plain-dict ``simulate_plan()`` response (success=True;
                           D-P206-4 feasible-only guard is enforced by the caller).
        engine_version:    ``ENGINE_SOURCE_SHA`` at save time.
        glidepath_version: MAX(version) from inf_glide_paths at save time
                           (``get_current_glidepath_version()`` C3 hook).

    Returns:
        Dict with UPPERCASE keys matching the RETURNING clause:
        ID, INFINITE_ID, VERSION_NO, INPUTS_JSON, RESULTS_JSON,
        ENGINE_VERSION, GLIDEPATH_VERSION, CREATED_AT, CREATED_BY.

    Raises:
        Any psycopg2/DB error propagates to the caller (route handles it as 500).
    """
    # Step 1: resolve next version_no (D-P206-2: read-then-insert).
    version_rows = execute_query_pg(_NEXT_VERSION_NO_SQL, (infinite_id,))
    next_version = int(version_rows[0]["NEXT_VERSION"])

    # Step 2: INSERT ... RETURNING the new row.
    row = execute_returning_pg(
        _INSERT_PLAN_SQL,
        (
            infinite_id,
            next_version,
            json.dumps(inputs_json),
            json.dumps(results_json),
            engine_version,
            glidepath_version,
        ),
    )
    logger.info(
        "insert_plan_version: infinite_id=%s version_no=%s engine=%s glidepath_v=%s",
        infinite_id,
        next_version,
        engine_version,
        glidepath_version,
    )
    return row[0]


def get_latest_plan(infinite_id: str) -> dict | None:
    """Return the MAX-version_no plan row for a family, or None when no plan exists.

    Args:
        infinite_id: Family identifier.

    Returns:
        Dict with UPPERCASE keys (ID, INFINITE_ID, VERSION_NO, INPUTS_JSON,
        RESULTS_JSON, ENGINE_VERSION, GLIDEPATH_VERSION, CREATED_AT, CREATED_BY)
        when a plan exists, or None when the family has no saved plans.

    Raises:
        Any psycopg2/DB error propagates to the caller.
    """
    rows = execute_query_pg(_GET_LATEST_PLAN_SQL, (infinite_id,))
    if not rows:
        return None
    return rows[0]
