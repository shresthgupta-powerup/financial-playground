"""Checked-in, diffable glide-path data (LP-015 C1, D-P202-4).

Converted from the binary ``Glide Paths.xlsx`` (3 sheets:
``Non-Negotiable`` / ``Semi-Negotiable`` / ``Negotiable``) shipped with the v3
simulator at commit ``1515f1e``. This replaces the xlsx-reading
``get_default_glide_paths()`` in the original ``main_v2.py``.

The format is a **tranche-and-chain cashflow script**, NOT a target-allocation
table (see the v3 ``.context/SIMULATION_MODEL.md`` § Glide paths and
``DECISIONS.md`` 2026-05-21 "Glide paths stay in tranche-and-chain format").
Each row is one cashflow event. Columns (preserved exactly):

    id                              Row identifier, unique within the sheet.
    place                           hybrid | debt | goal (terminal).
    years from inflow till end      Years before goal-end the money ARRIVES.
    years from outflow till end     Years before goal-end the money LEAVES
                                    (None/NaN for `goal` rows).
    inflow_from                     'core corpus' or another row's id (chain link).
    outflow_to                      The id this row's money flows into
                                    (None/NaN for `goal` rows).
    % of goal value                 Fraction of the goal target this chain
                                    delivers. The `goal` rows must sum to 100.

``None`` here stands in for the xlsx ``NaN`` cells; ``get_glide_paths()``
rebuilds them as ``float('nan')`` so the engine's ``pd.notna()`` checks behave
identically to reading the original workbook.

Glide paths are used only for Non-replenishing goals. Replenishing goals use
the shared pool mechanism and need no glide path.

This same data seeds C3's versioned ``inf_glide_paths`` PG table.
"""

import pandas as pd

# Bump when the glide-path data below changes. Each saved plan (C4) stamps the
# version it computed against (D-LP015-3 / D-P202-4).
GLIDEPATH_VERSION = 1

# Column order preserved byte-for-byte from the source workbook sheets.
_GLIDE_PATH_COLUMNS = [
    "id",
    "place",
    "years from inflow till end",
    "years from outflow till end",
    "inflow_from",
    "outflow_to",
    "% of goal value",
]

# Raw rows per sheet. ``None`` == xlsx NaN. ``inflow_from`` carries str
# ('core corpus') or int (a chain link id); ``outflow_to`` carries float ids /
# None to match the original (xlsx integer columns with NaN become float).
_GLIDE_PATHS_RAW = {
    "Non-Negotiable": [
        {"id": 1, "place": "hybrid", "years from inflow till end": 5, "years from outflow till end": 2.0, "inflow_from": "core corpus", "outflow_to": 2.0, "% of goal value": 25},
        {"id": 2, "place": "debt", "years from inflow till end": 2, "years from outflow till end": 0.0, "inflow_from": 1, "outflow_to": 3.0, "% of goal value": 25},
        {"id": 3, "place": "goal", "years from inflow till end": 0, "years from outflow till end": None, "inflow_from": 2, "outflow_to": None, "% of goal value": 25},
        {"id": 4, "place": "debt", "years from inflow till end": 4, "years from outflow till end": 0.0, "inflow_from": "core corpus", "outflow_to": 5.0, "% of goal value": 25},
        {"id": 5, "place": "goal", "years from inflow till end": 0, "years from outflow till end": None, "inflow_from": 4, "outflow_to": None, "% of goal value": 25},
        {"id": 6, "place": "debt", "years from inflow till end": 3, "years from outflow till end": 0.0, "inflow_from": "core corpus", "outflow_to": 7.0, "% of goal value": 25},
        {"id": 7, "place": "goal", "years from inflow till end": 0, "years from outflow till end": None, "inflow_from": 6, "outflow_to": None, "% of goal value": 25},
        {"id": 8, "place": "debt", "years from inflow till end": 2, "years from outflow till end": 0.0, "inflow_from": "core corpus", "outflow_to": 9.0, "% of goal value": 25},
        {"id": 9, "place": "goal", "years from inflow till end": 0, "years from outflow till end": None, "inflow_from": 8, "outflow_to": None, "% of goal value": 25},
    ],
    "Semi-Negotiable": [
        {"id": 1, "place": "hybrid", "years from inflow till end": 4, "years from outflow till end": 0.0, "inflow_from": "core corpus", "outflow_to": 2.0, "% of goal value": 25},
        {"id": 2, "place": "goal", "years from inflow till end": 0, "years from outflow till end": None, "inflow_from": 1, "outflow_to": None, "% of goal value": 25},
        {"id": 3, "place": "debt", "years from inflow till end": 3, "years from outflow till end": 0.0, "inflow_from": "core corpus", "outflow_to": 4.0, "% of goal value": 25},
        {"id": 4, "place": "goal", "years from inflow till end": 0, "years from outflow till end": None, "inflow_from": 3, "outflow_to": None, "% of goal value": 25},
        {"id": 5, "place": "debt", "years from inflow till end": 2, "years from outflow till end": 0.0, "inflow_from": "core corpus", "outflow_to": 6.0, "% of goal value": 25},
        {"id": 6, "place": "goal", "years from inflow till end": 0, "years from outflow till end": None, "inflow_from": 5, "outflow_to": None, "% of goal value": 25},
        {"id": 7, "place": "debt", "years from inflow till end": 1, "years from outflow till end": 0.0, "inflow_from": "core corpus", "outflow_to": 8.0, "% of goal value": 25},
        {"id": 8, "place": "goal", "years from inflow till end": 0, "years from outflow till end": None, "inflow_from": 7, "outflow_to": None, "% of goal value": 25},
    ],
    "Negotiable": [
        {"id": 1, "place": "hybrid", "years from inflow till end": 3, "years from outflow till end": 0.0, "inflow_from": "core corpus", "outflow_to": 2.0, "% of goal value": 30},
        {"id": 2, "place": "goal", "years from inflow till end": 0, "years from outflow till end": None, "inflow_from": 1, "outflow_to": None, "% of goal value": 30},
        {"id": 3, "place": "hybrid", "years from inflow till end": 2, "years from outflow till end": 0.0, "inflow_from": "core corpus", "outflow_to": 4.0, "% of goal value": 10},
        {"id": 4, "place": "goal", "years from inflow till end": 0, "years from outflow till end": None, "inflow_from": 3, "outflow_to": None, "% of goal value": 10},
        {"id": 5, "place": "hybrid", "years from inflow till end": 1, "years from outflow till end": 0.0, "inflow_from": "core corpus", "outflow_to": 6.0, "% of goal value": 10},
        {"id": 6, "place": "goal", "years from inflow till end": 0, "years from outflow till end": None, "inflow_from": 5, "outflow_to": None, "% of goal value": 10},
        {"id": 7, "place": "debt", "years from inflow till end": 2, "years from outflow till end": 0.0, "inflow_from": "core corpus", "outflow_to": 8.0, "% of goal value": 30},
        {"id": 8, "place": "goal", "years from inflow till end": 0, "years from outflow till end": None, "inflow_from": 7, "outflow_to": None, "% of goal value": 30},
        {"id": 9, "place": "debt", "years from inflow till end": 1, "years from outflow till end": 0.0, "inflow_from": "core corpus", "outflow_to": 10.0, "% of goal value": 20},
        {"id": 10, "place": "goal", "years from inflow till end": 0, "years from outflow till end": None, "inflow_from": 9, "outflow_to": None, "% of goal value": 20},
    ],
}


def _sheet_to_frame(rows):
    """Build a DataFrame matching what ``pd.read_excel`` produced from the xlsx.

    ``None`` cells become ``float('nan')`` so the engine's ``pd.notna()`` checks
    on ``years from outflow till end`` / ``outflow_to`` behave identically. The
    ``outflow_to`` column is coerced to float (integer-with-NaN columns load as
    float64 from Excel).
    """
    df = pd.DataFrame(rows, columns=_GLIDE_PATH_COLUMNS)
    df["years from outflow till end"] = df["years from outflow till end"].astype(float)
    df["outflow_to"] = df["outflow_to"].astype(float)
    return df


def get_glide_paths():
    """Return the ``{type: DataFrame}`` glide-path dict the engine consumes.

    Keys are the goal ``type`` values: ``Non-Negotiable`` / ``Semi-Negotiable``
    / ``Negotiable``. Each value is a fresh DataFrame (callers may mutate /
    lower-case ``place`` in place, as ``calculate_goal_cashflows`` does).
    """
    return {name: _sheet_to_frame(rows) for name, rows in _GLIDE_PATHS_RAW.items()}
