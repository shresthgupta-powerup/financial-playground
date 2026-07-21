"""DB-backed glide-path loader (LP-015 C3, Plan 205 Ph2).

Reads versioned glide-path rows from ``inf_glide_paths`` (PG) and converts
them into the same ``{glide_type: DataFrame}`` dict that ``get_glide_paths()``
returns, using the identical ``_sheet_to_frame`` helper so coercion is
identical by construction (D-P205-4).

Design decisions in effect:
  D-P205-1 -- "current version" = MAX(version); ``load_glide_paths(version=None)``.
  D-P205-2 -- Zero rows or missing table -> WARNING log + return ``get_glide_paths()``.
  D-P205-4 -- Reuse ``_sheet_to_frame``; do NOT hand-roll DataFrame construction.
  D-P205-5 -- Only this file imports from ``app.database_pg``; engine/glide_paths
               stay DB-free.

Column mapping (DB -> raw-row dict):
  row_id                      -> id              (int)
  place                       -> place           (str)
  years_from_inflow_till_end  -> years from inflow till end  (int)
  years_from_outflow_till_end -> years from outflow till end (None when NULL)
  inflow_from                 -> inflow_from  ('core corpus' stays str; else int())
  outflow_to                  -> outflow_to   (None when NULL; else float())
  pct_of_goal_value           -> % of goal value  (int)

NULL is stored as NULL in PG (D-P205-4 -- NaN columns stored as NULL).
``_sheet_to_frame`` converts None to float('nan') via the astype(float) calls.
"""

import logging
from typing import Optional

import psycopg2.errors

from app.database_pg import execute_query_pg
from app.planning.glide_paths import (
    GLIDEPATH_VERSION,
    _sheet_to_frame,
    get_glide_paths,
)

logger = logging.getLogger(__name__)

# Ordered sheet names (CHECK constraint in migration 020).
_GLIDE_TYPES = ("Non-Negotiable", "Semi-Negotiable", "Negotiable")

_SELECT_CURRENT_VERSION = """
    SELECT MAX(version) AS max_version
    FROM inf_glide_paths
"""

_SELECT_ROWS = """
    SELECT
        glide_type,
        row_id,
        place,
        years_from_inflow_till_end,
        years_from_outflow_till_end,
        inflow_from,
        outflow_to,
        pct_of_goal_value
    FROM inf_glide_paths
    WHERE version = %s
    ORDER BY glide_type, row_id
"""

_SELECT_MAX_VERSION_ROWS = """
    SELECT
        glide_type,
        row_id,
        place,
        years_from_inflow_till_end,
        years_from_outflow_till_end,
        inflow_from,
        outflow_to,
        pct_of_goal_value
    FROM inf_glide_paths
    WHERE version = (SELECT MAX(version) FROM inf_glide_paths)
    ORDER BY glide_type, row_id
"""


def _db_row_to_raw(row: dict) -> dict:
    """Convert one UPPERCASE-key DB row to the raw-row dict format ``_sheet_to_frame`` expects.

    inflow_from: 'core corpus' (str) stays str; any other value is cast to int
    (the migration stores int chain-link IDs as text -- e.g. '1', '2').
    years_from_outflow_till_end / outflow_to: None when NULL (``_sheet_to_frame``
    converts None -> float('nan') via astype(float)).
    """
    inflow_raw = row["INFLOW_FROM"]
    if inflow_raw == "core corpus":
        inflow_from = "core corpus"
    else:
        inflow_from = int(inflow_raw)

    outflow_raw = row["OUTFLOW_TO"]
    outflow_to = None if outflow_raw is None else float(outflow_raw)

    years_out_raw = row["YEARS_FROM_OUTFLOW_TILL_END"]
    years_from_outflow = None if years_out_raw is None else years_out_raw  # kept as-is; _sheet_to_frame does astype(float)

    return {
        "id": int(row["ROW_ID"]),
        "place": row["PLACE"],
        "years from inflow till end": int(row["YEARS_FROM_INFLOW_TILL_END"]),
        "years from outflow till end": years_from_outflow,
        "inflow_from": inflow_from,
        "outflow_to": outflow_to,
        "% of goal value": int(row["PCT_OF_GOAL_VALUE"]),
    }


def _rows_to_sheets(db_rows: list[dict]) -> dict:
    """Group DB rows by glide_type and convert each group to a raw-row list.

    Returns ``{glide_type: [raw_row, ...]}`` preserving the sheet order in
    ``_GLIDE_TYPES``.
    """
    by_type: dict[str, list] = {gt: [] for gt in _GLIDE_TYPES}
    for row in db_rows:
        gt = row["GLIDE_TYPE"]
        if gt in by_type:
            by_type[gt].append(_db_row_to_raw(row))
    return by_type


def load_glide_paths(version: Optional[int] = None) -> dict:
    """Load versioned glide paths from ``inf_glide_paths`` (PG).

    Args:
        version: Integer version to load. ``None`` (default) resolves to
            ``MAX(version)`` -- the current live version (D-P205-1).

    Returns:
        ``{glide_type: DataFrame}`` dict identical in shape to
        ``get_glide_paths()`` (D-P205-4). Each DataFrame is a fresh copy --
        callers may mutate in place (same contract as ``get_glide_paths()``).

    Fallback (D-P205-2):
        If ``inf_glide_paths`` does not exist (``psycopg2.errors.UndefinedTable``)
        or returns zero rows for the requested version, a WARNING is logged and
        the literal fallback from ``get_glide_paths()`` is returned.

    Other DB errors propagate -- they are NOT caught here.
    """
    try:
        if version is None:
            db_rows = execute_query_pg(_SELECT_MAX_VERSION_ROWS)
        else:
            db_rows = execute_query_pg(_SELECT_ROWS, (version,))
    except psycopg2.errors.UndefinedTable:
        logger.warning(
            "inf_glide_paths table not found (migration 020 not yet applied); "
            "falling back to get_glide_paths() literals. version=%s",
            version,
        )
        return get_glide_paths()

    if not db_rows:
        logger.warning(
            "inf_glide_paths returned 0 rows for version=%s; "
            "falling back to get_glide_paths() literals.",
            version,
        )
        return get_glide_paths()

    sheets = _rows_to_sheets(db_rows)
    return {glide_type: _sheet_to_frame(rows) for glide_type, rows in sheets.items() if rows}


def get_current_glidepath_version() -> int:
    """Return MAX(version) from ``inf_glide_paths``, or ``GLIDEPATH_VERSION`` fallback.

    Used by C4 when saving a plan -- stamps the version the simulation ran against.
    Falls back to ``GLIDEPATH_VERSION`` (the literal constant) when the table is
    absent or empty (mirrors the D-P205-2 fallback logic in ``load_glide_paths``).
    """
    try:
        rows = execute_query_pg(_SELECT_CURRENT_VERSION)
    except psycopg2.errors.UndefinedTable:
        logger.warning(
            "inf_glide_paths table not found; returning GLIDEPATH_VERSION fallback=%s",
            GLIDEPATH_VERSION,
        )
        return GLIDEPATH_VERSION

    if not rows or rows[0].get("MAX_VERSION") is None:
        logger.warning(
            "inf_glide_paths is empty; returning GLIDEPATH_VERSION fallback=%s",
            GLIDEPATH_VERSION,
        )
        return GLIDEPATH_VERSION

    return int(rows[0]["MAX_VERSION"])
