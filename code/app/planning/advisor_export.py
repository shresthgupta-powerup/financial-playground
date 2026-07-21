"""Build the multi-sheet advisor Excel workbook (LP-015 C2, Plan 203 Ph2).

Faithful port of ``C:\\Punit Patel\\Financial Planning v3\\advisor_export.py`` at
commit ``1515f1e`` (D-P203-12). The only substantive change from the v3 source is
that ``infinite_id`` is used instead of ``M3_ID`` and ``family_name`` instead of
``client_name`` — both are injected into the config by the route before this
module is called (D-P203-11), so callers see no API difference.

Sheet layout (Plan 213 — replaces Plan 211 raw ledger sheets):
  1. Personal & Corpus
  2. Investment Streams
  3. One-time Investments
  4. Goals            (advisor column layout; PV→FV at start; concrete occurrences)
  5. Picklists
  6. Simulation Result
  7. Action Plan      (all movements date-sorted; advisor to-do list; Plan 213)
  8. G{nn} {goal name} sheets — one per non-replenishing goal (Plan 213)
  9. Income & Pools   (aggregate Debt/Hybrid pool with source attribution; Plan 213)
 10. Comprehensive Monthly (present only when the simulation produced output)

Returns raw ``bytes`` from a BytesIO buffer; the route sets
``Content-Disposition: attachment``.
"""

import io
import re

import pandas as pd
from dateutil.relativedelta import relativedelta

from app.planning.engine import _resolve_recurring_occurrences


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Internal goal-type strings → advisor-file casing (mirrors v3).
_GOAL_TYPE_TO_ADVISOR = {
    "Non-Negotiable": "Non-negotiable",
    "Semi-Negotiable": "Semi-negotiable",
    "Negotiable": "Negotiable",
}
_FREQ_TO_ADVISOR = {
    "Monthly": "Monthly",
    "Quarterly": "Quarterly",
    "Half-Yearly": "Half-yearly",
    "Annual": "Annual",
}
_FREQ_MONTHS = {"Monthly": 1, "Quarterly": 3, "Half-Yearly": 6, "Annual": 12}

ADVISOR_GOAL_COLUMNS = [
    "Client Name",
    "M3_ID",
    "Goal_ID",
    "Goal_name",
    "Goal_desc",
    "Goal_type",
    "Goal_nature",
    "Goal_structure",
    "Goal_start_date",
    "Goal_end_date",
    "Goal_amt_total",
    "Goal_amt_per_occurrence",
    "Goal_frequency",
    "Goal_occurrences",
    "Requirement_escalation_pct",
    "Requirement_escalation_frequency",
    "Inflation_assumption_pct",
    "Goal_status",
    "Goal_phase",
    "Created_date",
    "Last_modified_date",
    "Validation_flag",
    "Remarks_advisory",
]

# Caveat line included in every per-goal header block and the Action Plan header
# (D-P213-6).
_CAVEAT = (
    "Amounts assume the modelled returns — re-export before executing a future "
    "movement. Dates are firm; rupee amounts are projections."
)

# Excel sheet-name invalid characters (D-P213-3).
_EXCEL_INVALID_RE = re.compile(r"[\[\]:*?/\\]")


# ---------------------------------------------------------------------------
# Sheet builders (faithful port from v3 advisor_export.py)
# ---------------------------------------------------------------------------

def _fmt_mon_yyyy(val) -> str | None:
    """Format a Timestamp/date/string as 'Mon YYYY' (e.g. 'Jun 2026').

    Used for all user-facing date columns in the advisor Excel workbook (D-P223-9).
    The day component is always the 1st after the month-grid invariant (Plan 223),
    so displaying it is meaningless.
    """
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return pd.Timestamp(val).strftime("%b %Y")
    except Exception:
        return None


def _personal_sheet(config: dict) -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("Current Date", _fmt_mon_yyyy(config.get("current_date"))),
            ("Current Age", config.get("current_age")),
            ("Target Lifetime (yrs)", config.get("target_lifetime")),
            ("Current Corpus (₹)", config.get("current_corpus")),
        ],
        columns=["Field", "Value"],
    )


def _investment_sheet(config: dict) -> pd.DataFrame:
    rows = []
    for s in config.get("investment_streams", []) or []:
        end_mode = s.get("end_date_mode", "At retirement")
        rows.append(
            {
                "Name": s.get("name"),
                "Amount (₹/month at start)": s.get("amount"),
                "Start Date": _fmt_mon_yyyy(s.get("start_date")),
                "End Mode": end_mode,
                "End Date": _fmt_mon_yyyy(s.get("end_date"))
                if end_mode == "Fixed" and s.get("end_date")
                else None,
                "Step-up %": s.get("step_up_percent"),
                "Step-up Frequency": s.get("step_up_frequency"),
                "Step-up Anchor": _fmt_mon_yyyy(s.get("step_up_date")),
            }
        )
    cols = [
        "Name",
        "Amount (₹/month at start)",
        "Start Date",
        "End Mode",
        "End Date",
        "Step-up %",
        "Step-up Frequency",
        "Step-up Anchor",
    ]
    return pd.DataFrame(rows, columns=cols)


def _one_time_investments_sheet(config: dict) -> pd.DataFrame:
    rows = []
    for w in config.get("one_time_investments", []) or []:
        rows.append(
            {
                "Name": w.get("name"),
                "Date": _fmt_mon_yyyy(w.get("date")),
                "Amount (₹ FV at date)": w.get("amount"),
            }
        )
    return pd.DataFrame(rows, columns=["Name", "Date", "Amount (₹ FV at date)"])


def _resolved_goal_start(goal: dict, retirement_date) -> pd.Timestamp:
    """Return the goal's resolved start date.

    When ``start_date_mode == 'At retirement'`` and a retirement_date is known,
    use the retirement_date; otherwise fall back to the explicit ``start_date``.
    """
    if (
        str(goal.get("start_date_mode", "Fixed")).lower() == "at retirement"
        and retirement_date is not None
    ):
        return pd.Timestamp(retirement_date)
    return pd.Timestamp(goal["start_date"])


def _goals_sheet(config: dict, retirement_date, death_date: pd.Timestamp) -> pd.DataFrame:
    """Goals sheet matching the advisor column layout.

    Amounts converted PV -> FV at start. Recurring goals have their ``end_mode``
    (Occurrences / Fixed date / Lifetime) collapsed into concrete ``Goal_occurrences``
    and ``Goal_end_date`` for the advisor.

    Mirrors v3 ``advisor_export._goals_sheet`` exactly.
    """
    today = pd.Timestamp(config.get("current_date"))
    now_norm = pd.Timestamp.now().normalize()
    rows = []
    for i, g in enumerate(config.get("goals", []) or [], start=1):
        start = _resolved_goal_start(g, retirement_date)
        years_to_start = max(0.0, (start - today).days / 365.25)
        fv_per_occurrence = float(g["amount"]) * (
            (1 + float(g["inflation_percent"]) / 100.0) ** years_to_start
        )

        is_lumpsum = g["structure"] == "Lumpsum"

        occurrences = None
        end_date = None
        if not is_lumpsum:
            # Resolve end_mode -> concrete occurrences via the engine helper.
            g_resolved = dict(g, start_date=start)
            occurrences = _resolve_recurring_occurrences(g_resolved, death_date)
            freq_months = _FREQ_MONTHS.get(g.get("frequency"))
            if freq_months and occurrences:
                end_date = start + relativedelta(months=freq_months * (int(occurrences) - 1))

        rows.append(
            {
                "Client Name": config.get("client_name", ""),
                "M3_ID": config.get("m3_id", ""),
                "Goal_ID": f"G{i:02d}",
                "Goal_name": g.get("name", ""),
                "Goal_desc": g.get("description", ""),
                "Goal_type": _GOAL_TYPE_TO_ADVISOR.get(g.get("type"), g.get("type")),
                "Goal_nature": g.get("nature"),
                "Goal_structure": g.get("structure"),
                "Goal_start_date": _fmt_mon_yyyy(start),
                "Goal_end_date": _fmt_mon_yyyy(end_date) if end_date is not None else None,
                "Goal_amt_total": round(fv_per_occurrence, 2) if is_lumpsum else None,
                "Goal_amt_per_occurrence": round(fv_per_occurrence, 2)
                if not is_lumpsum
                else None,
                "Goal_frequency": _FREQ_TO_ADVISOR.get(g.get("frequency"))
                if not is_lumpsum
                else None,
                "Goal_occurrences": int(occurrences) if occurrences else None,
                "Requirement_escalation_pct": float(g["inflation_percent"]) / 100.0,
                "Requirement_escalation_frequency": "Annual",
                "Inflation_assumption_pct": float(g["inflation_percent"]) / 100.0,
                "Goal_status": "Active",
                "Goal_phase": "Glide path",
                "Created_date": _fmt_mon_yyyy(now_norm),
                "Last_modified_date": _fmt_mon_yyyy(now_norm),
                "Validation_flag": "OK",
                "Remarks_advisory": "",
            }
        )
    return pd.DataFrame(rows, columns=ADVISOR_GOAL_COLUMNS)


def _picklists_sheet() -> pd.DataFrame:
    """Mirror the advisor file's Picklists sheet for downstream tools."""
    data = {
        "Goal_type": ["Non-negotiable", "Semi-negotiable", "Negotiable", None],
        "Goal_nature": ["Non-replenishing", "Replenishing", None, None],
        "Goal_structure": ["Lumpsum", "Recurring", None, None],
        "Goal_frequency": ["Monthly", "Quarterly", "Half-yearly", "Annual"],
        "Requirement_escalation_frequency": ["Annual", "Not applicable", None, None],
        "Goal_status": ["Active", "Achieved", "Cancelled", None],
    }
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# Action Plan helpers (Plan 213)
# ---------------------------------------------------------------------------

def _sanitise_sheet_name(goal_id: str, goal_name: str) -> str:
    """Build the per-goal sheet name: ``G{nn} {goal name}`` (D-P213-3).

    Sanitises Excel-invalid chars ``[]:*?/\\`` and truncates to 31 chars.
    The ``G{nn}`` prefix guarantees uniqueness even when truncations collide.
    """
    clean = _EXCEL_INVALID_RE.sub("", goal_name).strip()
    raw = f"{goal_id} {clean}"
    return raw[:31]


def _is_pass_through(row) -> bool:
    """Return True when a chain row satisfies the D-P214-3 zero-duration collapse guard.

    A row is a pass-through — safe to collapse — when ALL three conditions hold:
    - inflow_date == outflow_date  (the holding window is zero days)
    - tax_out_of_outflow == 0      (no tax was deducted; amount passes through intact)
    - inflow_amount == total_outflow_amount  (no growth; round-trip exact equality)

    If ANY condition is violated (e.g. future engine emits nonzero tax on a
    zero-duration row) the row is rendered as raw hops, never collapsed.
    Engine guarantee (verified P-713b, 435 chains): real output always satisfies the
    guard; the check exists for defensive future-proofing only.
    """
    try:
        inflow_date = pd.Timestamp(row["inflow_date"])
        outflow_date = pd.Timestamp(row["outflow_date"])
        if pd.isna(outflow_date):
            return False
        if inflow_date != outflow_date:
            return False
        tax = float(row.get("tax_out_of_outflow") or 0)
        if tax != 0.0:
            return False
        inflow = float(row.get("inflow_amount") or 0)
        outflow = float(row.get("total_outflow_amount") or 0)
        return inflow == outflow
    except Exception:
        return False


def _resolve_effective_source(row, id_to_row: dict):
    """Walk backward from row.inflow_from, skipping zero-duration pass-throughs.

    Returns the effective source row — the first upstream row (or sentinel 'core
    corpus') that is not itself a pass-through or that is the chain head.

    Returns ``None`` for ``inflow_from == 'core corpus'`` (caller handles that case).
    Returns the first non-pass-through upstream row, OR the head of the pass-through
    chain when the chain head itself is being skipped (the caller will treat the
    effective source as Core Corpus in that case).
    """
    inflow_from = row.get("inflow_from")
    # Walk the chain.  Guard against infinite loops with a step limit.
    max_steps = 50
    for _ in range(max_steps):
        if str(inflow_from).lower() == "core corpus":
            return None  # reached the chain root — caller treats as Core Corpus
        src = id_to_row.get(inflow_from)
        if src is None:
            return None
        if not _is_pass_through(src):
            return src  # stable non-pass-through source found
        # src is a pass-through — skip it, continue to src's own source.
        inflow_from = src.get("inflow_from")
    return id_to_row.get(inflow_from)  # fallback: return whatever we reached


def _build_action_rows(
    goal_dfs: dict,
    goals_config: list,
) -> pd.DataFrame:
    """Walk every non-replenishing tranche chain and emit Invest/Switch/Pay out rows.

    Chain-walk rules (D-P213-1):
    - **Invest**: a chain head whose ``inflow_from == 'core corpus'``
      (case-insensitive).  date = inflow_date; gross = net = inflow_amount; tax = 0;
      From = "Core Corpus"; To = bucket name (title-cased place).
    - **Switch**: a non-head, non-goal row (``place != 'goal'``) whose source
      row has a nonzero tax.  date = *source* row's outflow_date; From = source
      bucket; To = target bucket; gross = source total_outflow_amount;
      tax = source tax_out_of_outflow; net = this row's inflow_amount.
    - **Pay out**: a row whose ``place == 'goal'``.  date = inflow_date
      (= goal occurrence date); From = source bucket; To = "Goal payout";
      gross = source total_outflow_amount; tax = source tax_out_of_outflow;
      net = this row's inflow_amount.

    Zero-duration collapse (D-P214-2/3): any intermediate bucket row that satisfies
    the D-P214-3 exactness guard (inflow_date==outflow_date, tax==0, in==out) is a
    pass-through and is skipped; its downstream rows are connected directly to its
    effective source, collapsing the phantom hop.  A pass-through chain head
    (Core→bucket on the same day as the bucket's outflow) collapses so that the
    downstream bucket row becomes the new Invest from Core Corpus.  Consecutive
    pass-throughs collapse transitively.  Any violation of the guard renders the raw
    hops without modification.

    Aggregation (D-P213-4): rows are summed by (date, goal, action, from, to).

    ``goal_dfs`` keys have the format ``"{goal_name}"`` (single-tranche) or
    ``"{goal_name} ({i}/{n})"`` (multi-tranche) — verified against engine line 916.
    We strip the tranche suffix to recover the canonical goal name.
    """
    ACTION_COLS = [
        "Date", "Goal", "Action", "From", "To",
        "Gross (Rs)", "Tax (Rs)", "Net (Rs)",
    ]

    if not goal_dfs:
        return pd.DataFrame(columns=ACTION_COLS)

    # Build a lookup from goal name -> Goal_ID from goals_config (for ordering).
    # Non-replenishing goals only (chains exist only for those).
    goal_id_map: dict[str, str] = {}
    idx = 1
    for g in goals_config or []:
        gid = f"G{idx:02d}"
        idx += 1
        if str(g.get("nature", "")).lower() != "replenishing":
            goal_id_map[g.get("name", "")] = gid

    raw_rows: list[dict] = []

    for label, gdf in (goal_dfs or {}).items():
        # Recover goal name from label (strip multi-tranche suffix).
        # Engine label format: "Name (i/n)" for multi-tranche, "Name" otherwise.
        import re as _re
        name_match = _re.match(r"^(.+?) \(\d+/\d+\)$", label)
        goal_name = name_match.group(1) if name_match else label
        goal_id = goal_id_map.get(goal_name, "")

        # Build id -> row dict for chain-walking (keyed by row['id']).
        id_to_row: dict = {r["id"]: r for _, r in gdf.iterrows()}

        for ridx, r in gdf.iterrows():
            place = str(r.get("place", "")).lower()
            inflow_from = r.get("inflow_from")
            is_head = str(inflow_from).lower() == "core corpus"

            if is_head:
                # --- Invest (chain head: inflow_from == 'core corpus') ---
                # D-P214-2: if this head row is itself a pass-through, skip it —
                # the downstream bucket row will be emitted as a new Invest from
                # Core Corpus (resolved via _resolve_effective_source on that row).
                if _is_pass_through(r):
                    continue  # downstream row picks this up
                raw_rows.append({
                    "Date": _fmt_mon_yyyy(r.get("inflow_date")),
                    "Goal": goal_name,
                    "Goal_ID_order": goal_id,
                    "Action": "Invest",
                    "From": "Core Corpus",
                    "To": str(r.get("place", "")).title(),
                    "Gross (Rs)": float(r.get("inflow_amount") or 0),
                    "Tax (Rs)": 0.0,
                    "Net (Rs)": float(r.get("inflow_amount") or 0),
                })
            elif place == "goal":
                # --- Pay out ---
                # Resolve the effective source, skipping pass-throughs (D-P214-2).
                eff_src = _resolve_effective_source(r, id_to_row)
                if eff_src is None:
                    # Effective source is Core Corpus (all intermediates collapsed).
                    src_place = "Core Corpus"
                    gross = float(r.get("inflow_amount") or 0)
                    tax = 0.0
                    net = float(r.get("inflow_amount") or 0)
                else:
                    src_place = str(eff_src.get("place", "")).title()
                    gross = float(eff_src.get("total_outflow_amount") or 0)
                    tax = float(eff_src.get("tax_out_of_outflow") or 0)
                    net = float(r.get("inflow_amount") or 0)
                raw_rows.append({
                    "Date": _fmt_mon_yyyy(r.get("inflow_date")),
                    "Goal": goal_name,
                    "Goal_ID_order": goal_id,
                    "Action": "Pay out",
                    "From": src_place,
                    "To": "Goal payout",
                    "Gross (Rs)": gross,
                    "Tax (Rs)": tax,
                    "Net (Rs)": net,
                })
            else:
                # --- Invest or Switch (non-head, non-goal bucket row) ---
                # D-P214-2: if this row is itself a pass-through, skip it — its
                # downstream row will resolve back to the true source.
                if _is_pass_through(r):
                    continue  # downstream row picks this up

                # Resolve the effective source, skipping pass-throughs (D-P214-2).
                eff_src = _resolve_effective_source(r, id_to_row)

                if eff_src is None:
                    # Effective source is Core Corpus (this row's chain head, or all
                    # intermediates up to the head, are pass-throughs).  Emit as Invest.
                    raw_rows.append({
                        "Date": _fmt_mon_yyyy(r.get("inflow_date")),
                        "Goal": goal_name,
                        "Goal_ID_order": goal_id,
                        "Action": "Invest",
                        "From": "Core Corpus",
                        "To": str(r.get("place", "")).title(),
                        "Gross (Rs)": float(r.get("inflow_amount") or 0),
                        "Tax (Rs)": 0.0,
                        "Net (Rs)": float(r.get("inflow_amount") or 0),
                    })
                else:
                    # Non-Core effective source: emit as Switch.
                    eff_src_place = str(eff_src.get("place", "")).lower()
                    if eff_src_place == "goal":
                        continue  # shouldn't happen; guard it
                    gross = float(eff_src.get("total_outflow_amount") or 0)
                    tax = float(eff_src.get("tax_out_of_outflow") or 0)
                    net = float(r.get("inflow_amount") or 0)
                    raw_rows.append({
                        "Date": _fmt_mon_yyyy(eff_src.get("outflow_date")),
                        "Goal": goal_name,
                        "Goal_ID_order": goal_id,
                        "Action": "Switch",
                        "From": str(eff_src.get("place", "")).title(),
                        "To": str(r.get("place", "")).title(),
                        "Gross (Rs)": gross,
                        "Tax (Rs)": tax,
                        "Net (Rs)": net,
                    })

    if not raw_rows:
        return pd.DataFrame(columns=ACTION_COLS)

    df = pd.DataFrame(raw_rows)

    # Aggregate by (Date, Goal, Action, From, To) — sum gross/tax/net.
    agg = (
        df.groupby(["Date", "Goal", "Goal_ID_order", "Action", "From", "To"], dropna=False)
        .agg({"Gross (Rs)": "sum", "Tax (Rs)": "sum", "Net (Rs)": "sum"})
        .reset_index()
    )

    # Sort chronologically, then by Goal_ID order, then action.
    agg = agg.sort_values(["Date", "Goal_ID_order", "Action"]).reset_index(drop=True)
    agg = agg.drop(columns=["Goal_ID_order"])

    return agg[ACTION_COLS]


def _build_action_plan_sheet(
    goal_dfs: dict,
    pool_movements_df: pd.DataFrame | None,
    goals_config: list,
) -> pd.DataFrame:
    """Action Plan sheet (D-P213-2): all movements date-sorted, advisor to-do list.

    Combines non-replenishing goal chain actions (from ``_build_action_rows``)
    with pool actions from ``pool_movements_df`` (Income & Pools source data).
    """
    action_rows = _build_action_rows(goal_dfs, goals_config)

    pool_action_rows = _build_pool_action_rows(pool_movements_df)

    if action_rows.empty and pool_action_rows.empty:
        return pd.DataFrame(columns=[
            "Date", "Goal", "Action", "From", "To",
            "Gross (Rs)", "Tax (Rs)", "Net (Rs)",
        ])

    combined = pd.concat([action_rows, pool_action_rows], ignore_index=True)
    combined = combined.sort_values("Date").reset_index(drop=True)
    return combined


def _build_pool_action_rows(pool_movements_df: pd.DataFrame | None) -> pd.DataFrame:
    """Derive advisor action rows from pool_movements_df (D-P213-5).

    Column semantics (from simulate_pool's log_movement calls):
    - Inflow to Debt > 0, Outflow from Hybrid > 0  -> Switch (Hybrid -> Debt)
    - Inflow to Debt > 0, Outflow from Hybrid == 0  -> Pool top-up (Core Corpus -> Debt)
    - Inflow to Hybrid > 0                           -> Pool top-up (Core Corpus -> Hybrid)
    - Outflow from Debt > 0                          -> Pay out (Debt -> Goal payout)

    D-P213-5 honesty: the pool_movements_df genuinely does carry enough signal
    to distinguish Hybrid->Debt transfers (both Inflow to Debt AND Outflow from
    Hybrid are non-zero on the same row) from Core refills (only the Inflow column
    is non-zero). This is evidenced directly in simulate_pool:
      log_movement(sim_date, debt_in=net_proceeds, hybrid_out=transfer_gross)  # transfer
      log_movement(sim_date, debt_in=debt_shortfall)                           # core->debt
      log_movement(sim_date, hybrid_in=hybrid_shortfall)                       # core->hybrid
    Labels are therefore NOT fabricated; they are read from the data.
    """
    ACTION_COLS = [
        "Date", "Goal", "Action", "From", "To",
        "Gross (Rs)", "Tax (Rs)", "Net (Rs)",
    ]

    if pool_movements_df is None or pool_movements_df.empty:
        return pd.DataFrame(columns=ACTION_COLS)

    rows = []
    for _, r in pool_movements_df.iterrows():
        date = _fmt_mon_yyyy(r.get("Date"))
        debt_in = float(r.get("Inflow to Debt") or 0)
        debt_out = float(r.get("Outflow from Debt") or 0)
        hybrid_in = float(r.get("Inflow to Hybrid") or 0)
        hybrid_out = float(r.get("Outflow from Hybrid") or 0)

        # Hybrid -> Debt transfer
        if debt_in > 0 and hybrid_out > 0:
            rows.append({
                "Date": date,
                "Goal": "Replenishing (pool)",
                "Action": "Switch",
                "From": "Hybrid",
                "To": "Debt",
                "Gross (Rs)": hybrid_out,
                "Tax (Rs)": 0.0,
                "Net (Rs)": debt_in,
            })

        # Core Corpus -> Debt top-up
        elif debt_in > 0:
            rows.append({
                "Date": date,
                "Goal": "Replenishing (pool)",
                "Action": "Pool top-up",
                "From": "Core Corpus",
                "To": "Debt",
                "Gross (Rs)": debt_in,
                "Tax (Rs)": 0.0,
                "Net (Rs)": debt_in,
            })

        # Core Corpus -> Hybrid top-up
        if hybrid_in > 0:
            rows.append({
                "Date": date,
                "Goal": "Replenishing (pool)",
                "Action": "Pool top-up",
                "From": "Core Corpus",
                "To": "Hybrid",
                "Gross (Rs)": hybrid_in,
                "Tax (Rs)": 0.0,
                "Net (Rs)": hybrid_in,
            })

        # Debt -> Goal payout
        if debt_out > 0:
            rows.append({
                "Date": date,
                "Goal": "Replenishing (pool)",
                "Action": "Pay out",
                "From": "Debt",
                "To": "Goal payout",
                "Gross (Rs)": debt_out,
                "Tax (Rs)": 0.0,
                "Net (Rs)": debt_out,
            })

    if not rows:
        return pd.DataFrame(columns=ACTION_COLS)

    return pd.DataFrame(rows, columns=ACTION_COLS)


def _build_income_pools_sheet(pool_movements_df: pd.DataFrame | None) -> pd.DataFrame:
    """Income & Pools sheet (D-P213-5) — replaces the old Pool Movements sheet.

    Passes through the simulate_pool pool_movements_df with date formatting and
    a readable source-attribution column derived from the inflow/outflow pattern.
    """
    if pool_movements_df is None or pool_movements_df.empty:
        return pd.DataFrame(columns=[
            "Date", "Source", "Debt Pool Value (Rs)",
            "Inflow to Debt (Rs)", "Outflow from Debt (Rs)",
            "Hybrid Pool Value (Rs)", "Inflow to Hybrid (Rs)", "Outflow from Hybrid (Rs)",
        ])

    df = pool_movements_df.copy()
    if "Date" in df.columns:
        df["Date"] = df["Date"].map(_fmt_mon_yyyy)

    # Derive source attribution column.
    def _source(row) -> str:
        debt_in = float(row.get("Inflow to Debt") or 0)
        hybrid_out = float(row.get("Outflow from Hybrid") or 0)
        hybrid_in = float(row.get("Inflow to Hybrid") or 0)
        debt_out = float(row.get("Outflow from Debt") or 0)
        parts = []
        if debt_in > 0 and hybrid_out > 0:
            parts.append("Hybrid->Debt transfer")
        elif debt_in > 0:
            parts.append("Core->Debt top-up")
        if hybrid_in > 0:
            parts.append("Core->Hybrid top-up")
        if debt_out > 0:
            parts.append("Goal payout")
        return "; ".join(parts) if parts else "Idle"

    df.insert(1, "Source", df.apply(_source, axis=1))

    # Rename columns to match the new sheet style.
    col_map = {
        "Debt Pool Value": "Debt Pool Value (Rs)",
        "Inflow to Debt": "Inflow to Debt (Rs)",
        "Outflow from Debt": "Outflow from Debt (Rs)",
        "Hybrid Pool Value": "Hybrid Pool Value (Rs)",
        "Inflow to Hybrid": "Inflow to Hybrid (Rs)",
        "Outflow from Hybrid": "Outflow from Hybrid (Rs)",
    }
    df = df.rename(columns=col_map)

    ordered_cols = [
        "Date", "Source", "Debt Pool Value (Rs)",
        "Inflow to Debt (Rs)", "Outflow from Debt (Rs)",
        "Hybrid Pool Value (Rs)", "Inflow to Hybrid (Rs)", "Outflow from Hybrid (Rs)",
    ]
    # Keep only known columns (guard against schema drift).
    present = [c for c in ordered_cols if c in df.columns]
    return df[present]


def _goal_header_block(
    goal_id: str,
    g: dict,
    retirement_date,
    death_date: pd.Timestamp,
    today: pd.Timestamp,
) -> list[tuple]:
    """Return a list of (Field, Value) rows for the per-goal header block (D-P213-3/6)."""
    start = _resolved_goal_start(g, retirement_date)
    years_to_start = max(0.0, (start - today).days / 365.25)
    fv_at_start = round(
        float(g["amount"]) * ((1 + float(g["inflation_percent"]) / 100.0) ** years_to_start),
        2,
    )

    is_lumpsum = g.get("structure") == "Lumpsum"
    occurrences = None
    end_date = None
    if not is_lumpsum:
        g_resolved = dict(g, start_date=start)
        occurrences = _resolve_recurring_occurrences(g_resolved, death_date)
        freq_months = _FREQ_MONTHS.get(g.get("frequency"))
        if freq_months and occurrences:
            end_date = start + relativedelta(months=freq_months * (int(occurrences) - 1))

    category = _GOAL_TYPE_TO_ADVISOR.get(g.get("type"), g.get("type") or "")

    return [
        ("Goal", f"{goal_id} {g.get('name', '')}"),
        ("Category", category),
        ("Nature", g.get("nature", "")),
        ("Structure", g.get("structure", "")),
        ("Amount per occurrence (Rs)", round(float(g.get("amount", 0)), 2) if not is_lumpsum else None),
        ("Total amount (Rs)", round(float(g.get("amount", 0)), 2) if is_lumpsum else None),
        ("FV at start (Rs)", fv_at_start),
        ("Start date", _fmt_mon_yyyy(start)),
        ("End date", _fmt_mon_yyyy(end_date) if end_date else None),
        ("Frequency", _FREQ_TO_ADVISOR.get(g.get("frequency")) if not is_lumpsum else None),
        ("Occurrences", int(occurrences) if occurrences else None),
        ("Caveat", _CAVEAT),
        ("", ""),  # blank spacer
    ]


def _per_goal_sheet(
    goal_id: str,
    g: dict,
    goal_dfs: dict,
    retirement_date,
    death_date: pd.Timestamp,
    today: pd.Timestamp,
) -> pd.DataFrame:
    """Build the per-goal sheet: header block + caveat + action rows (D-P213-3)."""
    header_rows = _goal_header_block(goal_id, g, retirement_date, death_date, today)

    goal_name = g.get("name", "")
    # Filter action rows for this goal only (across all tranche labels).
    goals_config = [g]  # single-goal view — goal_id_map will map correctly
    # Rebuild action rows for this specific goal by filtering goal_dfs.
    goal_specific_dfs = {
        label: df
        for label, df in (goal_dfs or {}).items()
        if _tranche_goal_name(label) == goal_name
    }

    action_df = _build_action_rows(goal_specific_dfs, [g])

    # Combine header block + action data into a single DataFrame.
    header_df = pd.DataFrame(header_rows, columns=["Field", "Value"])
    if action_df.empty:
        return header_df

    # Align action_df columns as key-value pairs for the header section, then
    # append as a table block below.
    # Write header then action table as separate sections.
    # Use a two-column key-value layout for the header, then write the action
    # table starting right below.
    return header_df, action_df


def _tranche_goal_name(label: str) -> str:
    """Strip the '(i/n)' tranche suffix from a goal_dfs key to recover goal name."""
    import re as _re
    m = _re.match(r"^(.+?) \(\d+/\d+\)$", label)
    return m.group(1) if m else label


def _simulation_result_sheet(config: dict, result: dict, snapshot=None) -> pd.DataFrame:
    rows = [
        ("Success", result.get("success")),
        ("Earliest Retirement Date", _fmt_mon_yyyy(result.get("retirement_date"))),
    ]
    if result.get("failure"):
        f = result["failure"]
        rows.append(("First Failure Date", _fmt_mon_yyyy(f.get("date"))))
        rows.append(("First Failure Reason", f.get("description", "")))
    if snapshot is not None:
        rows.append(("", ""))
        rows.append(("-- At retirement --", ""))
        for k, v in snapshot.items():
            rows.append((k, v))
    return pd.DataFrame(rows, columns=["Field", "Value"])


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def build_advisor_workbook(
    config: dict,
    result: dict,
    comprehensive_df: pd.DataFrame | None = None,
    snapshot=None,
    goal_dfs: dict | None = None,
    pool_movements_df: pd.DataFrame | None = None,
) -> bytes:
    """Build the multi-sheet workbook and return raw ``bytes`` ready for download.

    Sheet order (D-P213-7): the 6 input/result sheets -> Action Plan -> per-goal
    sheets (non-replenishing, G-prefix ordered) -> Income & Pools -> Comprehensive Monthly.

    Args:
        config: Engine config dict (must have ``client_name``/``m3_id`` injected).
        result: ``find_retirement_date`` result dict
            (``{success, retirement_date, failure}``).
        comprehensive_df: Full monthly DataFrame from ``run_simulation`` (optional;
            sheet omitted when None or empty).
        snapshot: Optional pre-computed snapshot dict (``{str: float}`` key-value
            pairs for the Simulation Result sheet); built by the route from the
            service's ``_build_snapshot`` output.
        goal_dfs: Per-tranche glide-path chain frames from ``run_simulation``
            (optional; Action Plan and per-goal sheets omitted when None/empty).
        pool_movements_df: Aggregate Debt/Hybrid pool movements from
            ``run_simulation`` (optional; Income & Pools sheet omitted when
            None/empty).

    Returns:
        Raw xlsx bytes.
    """
    retirement_date = result.get("retirement_date")
    today = pd.Timestamp(config.get("current_date"))
    death_date = today + pd.DateOffset(
        years=int(
            config.get("target_lifetime", 90) - config.get("current_age", 30)
        )
    )

    goals_config = config.get("goals", []) or []

    # Build Goal_ID -> goal mapping for non-replenishing goals (D-P213-3).
    # Uses the same 1-based sequential index as _goals_sheet.
    nonrep_goals: list[tuple[str, dict]] = []
    for i, g in enumerate(goals_config, start=1):
        if str(g.get("nature", "")).lower() != "replenishing":
            nonrep_goals.append((f"G{i:02d}", g))

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # --- 6 input/result sheets (unchanged) ---
        _personal_sheet(config).to_excel(
            writer, sheet_name="Personal & Corpus", index=False
        )
        _investment_sheet(config).to_excel(
            writer, sheet_name="Investment Streams", index=False
        )
        _one_time_investments_sheet(config).to_excel(
            writer, sheet_name="One-time Investments", index=False
        )
        _goals_sheet(config, retirement_date, death_date).to_excel(
            writer, sheet_name="Goals", index=False
        )
        _picklists_sheet().to_excel(writer, sheet_name="Picklists", index=False)
        _simulation_result_sheet(config, result, snapshot=snapshot).to_excel(
            writer, sheet_name="Simulation Result", index=False
        )

        # --- Action Plan sheet (D-P213-2) ---
        action_plan = _build_action_plan_sheet(goal_dfs, pool_movements_df, goals_config)
        # Prepend caveat row above the table (D-P213-6).
        caveat_df = pd.DataFrame([{"Date": "Note", "Goal": _CAVEAT,
                                   "Action": "", "From": "", "To": "",
                                   "Gross (Rs)": "", "Tax (Rs)": "", "Net (Rs)": ""}])
        action_plan_with_caveat = pd.concat([caveat_df, action_plan], ignore_index=True)
        action_plan_with_caveat.to_excel(writer, sheet_name="Action Plan", index=False)

        # --- Per-goal sheets (D-P213-3) — non-replenishing only ---
        for goal_id, g in nonrep_goals:
            sheet_result = _per_goal_sheet(
                goal_id, g, goal_dfs, retirement_date, death_date, today
            )
            sheet_name = _sanitise_sheet_name(goal_id, g.get("name", ""))

            if isinstance(sheet_result, tuple):
                header_df, action_df = sheet_result
                # Write header block starting at row 0.
                header_df.to_excel(writer, sheet_name=sheet_name, index=False, startrow=0)
                # Write action table starting below the header.
                action_df.to_excel(
                    writer, sheet_name=sheet_name, index=False,
                    startrow=len(header_df) + 2
                )
            else:
                sheet_result.to_excel(writer, sheet_name=sheet_name, index=False)

        # --- Income & Pools sheet (D-P213-5) ---
        if pool_movements_df is not None and not pool_movements_df.empty:
            _build_income_pools_sheet(pool_movements_df).to_excel(
                writer, sheet_name="Income & Pools", index=False
            )

        # --- Comprehensive Monthly ---
        if comprehensive_df is not None and not comprehensive_df.empty:
            comprehensive_df.to_excel(
                writer, sheet_name="Comprehensive Monthly", index=False
            )

    buf.seek(0)
    return buf.getvalue()
