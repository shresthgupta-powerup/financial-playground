"""Financial Planning Playground — Streamlit host for the handed-off engine.

v1 ground rule: the engine under code/app/planning/ is a byte-identical copy of
the CRM handoff (see 00_START_HERE.md); this file is ONLY a UI wrapper. The form
model (defaults, goal templates, progressive disclosure, risk-profile mapping)
is a direct port of code/frontend/planForm.js, and the output shaping mirrors
the pure helpers in code/app/planning/service.py (_build_snapshot,
_build_goal_results, _build_wealth_monthly, append_csv_summary_cols) — service
itself is not imported because it pulls the app-coupled DB modules.
"""

import copy
import json
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "code"))

import pandas as pd
import streamlit as st

from app.planning import (
    ENGINE_SOURCE_SHA,
    GLIDEPATH_VERSION,
    PlanValidationError,
    find_retirement_date,
    get_glide_paths,
    run_simulation,
)
from app.planning.advisor_export import build_advisor_workbook
from app.planning.engine import (
    _DEFAULT_INSTRUMENT_PARAMS,
    _resolve_goals,
    expand_recurring_goal_to_tranches,
    format_inr,
)
from app.planning.schemas import RISK_PROFILE_CORE_RETURNS

# ── Picklists (mirror planForm.js) ──────────────────────────────────────────
GOAL_TYPES = ["Non-Negotiable", "Semi-Negotiable", "Negotiable"]
GOAL_NATURES = ["Non-replenishing", "Replenishing"]

# 2026-08-14: the two-layer selection is back (nature first; Non-replenishing
# goals additionally choose a Lumpsum/Recurring structure), reverting the
# 2026-07-30 payout-count rule AND the display rename that depended on it —
# with a structure picker present, nature must be shown under its own names.
# Loading files saved during the rename era still works: NATURE_FROM_DISPLAY
# maps their display words back (see form_state_from_inputs).
NATURE_FROM_DISPLAY = {"Lumpsum": "Non-replenishing", "Recurring": "Replenishing"}
GOAL_STRUCTURES = ["Lumpsum", "Recurring"]
GOAL_START_MODES = ["Fixed", "At retirement"]
GOAL_END_MODES = ["Occurrences", "Fixed date", "Lifetime"]
INVESTMENT_END_MODES = ["At retirement", "Fixed"]
RECURRING_FREQUENCIES = ["Monthly", "Quarterly", "Half-Yearly", "Annual"]
STEPUP_FREQUENCIES = ["Annual", "Half-Yearly", "Quarterly", "Monthly"]
RISK_PROFILES = list(RISK_PROFILE_CORE_RETURNS.keys())

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

GOAL_TEMPLATES = {
    "Retirement Income": "retirement_income",
    "Child Education": "child_education",
    "Marriage": "marriage",
    "Home Purchase": "home_purchase",
    "Custom": "custom",
}


# ── Date helpers (month-grid invariant: every date is the 1st of a month) ───
def month_start_today() -> pd.Timestamp:
    now = pd.Timestamp.today()
    return pd.Timestamp(now.year, now.month, 1)


def add_years(ts: pd.Timestamp, years: int) -> pd.Timestamp:
    return pd.Timestamp(ts.year + years, ts.month, 1)


def fmt_mon_yyyy(ts) -> str:
    return pd.Timestamp(ts).strftime("%b %Y")


def short_inr(amount) -> str:
    v = float(amount)
    if v >= 1e7:
        return f"₹{v / 1e7:.2f} Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:.2f} L"
    return format_inr(v)


def inr_hint(amount) -> str:
    """Grouped INR + lakh/crore hint (planForm UX note 6)."""
    try:
        v = float(amount)
    except (TypeError, ValueError):
        return ""
    if v >= 1e7:
        return f"{format_inr(v)}  ({v / 1e7:.2f} Cr)"
    if v >= 1e5:
        return f"{format_inr(v)}  ({v / 1e5:.2f} L)"
    return format_inr(v)


# ── Default form state (mirror planForm.makeDefault*) ───────────────────────
def make_default_stream(index: int, today: pd.Timestamp) -> dict:
    return {
        "name": "Salary" if index == 0 else f"Stream {index + 1}",
        "amount": 100_000 if index == 0 else 50_000,
        "start_date": today,
        "end_date_mode": "At retirement",
        "end_date": add_years(today, 30 if index == 0 else 20),
        "step_up_percent": 10.0,
        "step_up_frequency": "Annual",
        "step_up_date": today,
    }


def make_default_goal(index: int, today: pd.Timestamp) -> dict:
    return {
        "name": f"Goal {index + 1}",
        "description": "",
        "type": "Non-Negotiable",
        "nature": "Non-replenishing",
        "structure": "Lumpsum",
        "start_date_mode": "Fixed",
        "start_date": add_years(today, 15),
        "amount": 1_000_000,
        "frequency": "Annual",
        "end_mode": "Occurrences",
        "occurrences": 1,
        "end_date": None,
        "inflation_percent": 6.0,
    }


def make_goal_from_template(template_key: str, index: int, today: pd.Timestamp) -> dict:
    base = make_default_goal(index, today)
    if template_key == "retirement_income":
        base.update(
            name="Retirement Income", description="Monthly income post-retirement",
            nature="Replenishing", structure="Recurring",
            start_date_mode="At retirement", start_date=add_years(today, 30),
            amount=75_000, frequency="Monthly", end_mode="Lifetime",
            occurrences=360, end_date=None, inflation_percent=6.0,
        )
    elif template_key == "child_education":
        base.update(
            name="Child Education", description="Annual education fees",
            nature="Non-replenishing", structure="Recurring", type="Non-Negotiable",
            start_date_mode="Fixed", start_date=add_years(today, 12),
            amount=1_500_000, frequency="Annual", end_mode="Occurrences",
            occurrences=4, inflation_percent=8.0,
        )
    elif template_key == "marriage":
        base.update(
            name="Marriage", description="Wedding expenses",
            nature="Non-replenishing", structure="Lumpsum", type="Semi-Negotiable",
            start_date_mode="Fixed", start_date=add_years(today, 20),
            amount=3_000_000, inflation_percent=7.0,
        )
    elif template_key == "home_purchase":
        base.update(
            name="Home Purchase", description="Down payment / purchase",
            nature="Non-replenishing", structure="Lumpsum", type="Negotiable",
            start_date_mode="Fixed", start_date=add_years(today, 8),
            amount=5_000_000, inflation_percent=6.0,
        )
    return base


def normalise_goal(goal: dict) -> dict:
    """Progressive-disclosure reset (mirror planForm.normaliseGoal).

    Replenishing goals are always a recurring payout stream; Non-replenishing
    goals carry their own Lumpsum/Recurring structure choice.
    """
    g = dict(goal)
    if g["nature"] == "Replenishing":
        g["structure"] = "Recurring"
    if g.get("structure") not in GOAL_STRUCTURES:
        g["structure"] = "Lumpsum"
    if g["structure"] == "Lumpsum":
        g["frequency"] = None
        g["end_mode"] = None
        g["occurrences"] = 1
        g["end_date"] = None
    else:
        if g.get("frequency") not in RECURRING_FREQUENCIES:
            g["frequency"] = "Monthly"
        if g.get("end_mode") not in GOAL_END_MODES:
            g["end_mode"] = "Occurrences"
        if g["end_mode"] == "Occurrences":
            g["end_date"] = None
            if not g.get("occurrences") or g["occurrences"] < 1:
                g["occurrences"] = 1
        elif g["end_mode"] == "Fixed date":
            if not g.get("occurrences"):
                g["occurrences"] = 1
        else:  # Lifetime
            g["end_date"] = None
    return g


# ── Output shaping (ports of service.py's pure helpers) ─────────────────────
def resolve_instrument_params(risk_profile: str) -> dict:
    merged = copy.deepcopy(_DEFAULT_INSTRUMENT_PARAMS)
    merged["core_corpus"]["return"] = RISK_PROFILE_CORE_RETURNS.get(
        risk_profile, RISK_PROFILE_CORE_RETURNS["Balanced"]
    )
    return merged


def build_snapshot(comprehensive_df: pd.DataFrame, retirement_date: pd.Timestamp) -> dict | None:
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
        "core": round(core, 2), "debt": round(debt, 2), "hybrid": round(hybrid, 2),
        "goal_debt": round(goal_debt, 2), "goal_hybrid": round(goal_hybrid, 2),
        "total": round(total, 2),
    }


def build_goal_results(config: dict, retirement_date) -> pd.DataFrame:
    current_date = pd.Timestamp(config["current_date"])
    rows = []
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
        rows.append({
            "Goal": goal.get("name", ""),
            "Nature": goal.get("nature", ""),
            "Structure": goal.get("structure", ""),
            "Starts": fmt_mon_yyyy(start),
            "Amount (today's ₹)": format_inr(pv),
            "Amount at start (FV)": format_inr(pv * ((1 + inflation / 100) ** years)),
        })
    return pd.DataFrame(rows)


def wealth_frame(comprehensive_df: pd.DataFrame, death_date: pd.Timestamp) -> pd.DataFrame:
    df = comprehensive_df.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    df = df[df["Date"] <= death_date]
    value_cols = [c for c in df.columns if c.endswith("Value")]
    out = pd.DataFrame({"Date": df["Date"]})
    out["Total wealth"] = df[value_cols].fillna(0).sum(axis=1)
    out["Core corpus"] = df.get("Core Corpus Value", 0)
    out["Debt pool"] = df.get("Debt Pool Value", 0)
    out["Hybrid pool"] = df.get("Hybrid Pool Value", 0)
    return out.set_index("Date")


_POOL_VALUE_COLS = {"Core Corpus Value", "Debt Pool Value", "Hybrid Pool Value"}


def csv_with_summary(comprehensive_df: pd.DataFrame) -> bytes:
    """Port of service.append_csv_summary_cols + to_csv."""
    out = comprehensive_df.copy()
    value_cols = [c for c in out.columns if c.endswith("Value")]
    goal_value_cols = [c for c in value_cols if c not in _POOL_VALUE_COLS]
    out["Total Wealth (Rs)"] = out[value_cols].fillna(0).sum(axis=1)
    out["Goal Tranches (Rs)"] = out[goal_value_cols].fillna(0).sum(axis=1) if goal_value_cols else 0.0
    return out.to_csv(index=False).encode("utf-8")


def new_simulation_id() -> str:
    """Readable, sortable, unique: SIM-<IST timestamp>-<random suffix>."""
    stamp = pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y%m%d-%H%M%S")
    return f"SIM-{stamp}-{uuid.uuid4().hex[:6]}"


def inputs_filename(config: dict) -> str:
    """Unique, human-sortable filename: client + timestamp, filesystem-safe."""
    raw = (config.get("client_name") or "plan").strip() or "plan"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-") or "plan"
    stamp = pd.Timestamp.now().strftime("%Y-%m-%d_%H%M")
    return f"financial_plan_inputs_{slug}_{stamp}.json"


def _iso_or_none(d):
    """Serialise a date to 'YYYY-MM-01' (month grid), or None."""
    if d is None:
        return None
    try:
        return pd.Timestamp(d).strftime("%Y-%m-01")
    except Exception:
        return None


# ── CRM goal-import contract (CRM team, 2026-07-29) ─────────────────────────
# The goals array of this file is imported into the CRM as official goals, so
# it follows the CRM's strict field contract:
# - nature is Replenishing / Non-replenishing (the same vocabulary the UI
#   shows again since the 2026-08-14 revert).
# - structure accompanies Non-replenishing goals only (ignored for
#   Replenishing); end_mode is never exported on resolved goals — a concrete
#   occurrences count takes its place.
# - type / frequency casing standardised to the CRM picklists.
_TYPE_TO_CRM = {"Non-Negotiable": "Non-negotiable", "Semi-Negotiable": "Semi-negotiable"}
_FREQ_TO_CRM = {"Half-Yearly": "Half-yearly"}
_FREQ_MONTHS = {"Monthly": 1, "Quarterly": 3, "Half-Yearly": 6, "Annual": 12}


def _crm_goal(g: dict, resolved: bool) -> dict:
    """One goals-array entry, keys ordered as in the CRM's example.

    resolved=True (successful run): contract shape — start_date_mode is always
    "Fixed" with the actual date ("At retirement" resolved to the solved
    retirement date by engine._resolve_goals), occurrences is the concrete
    count ("Lifetime" collapsed against the plan's death date), and end_date is
    the computed last-payment date. resolved=False (infeasible run — nothing
    solved to resolve against): the goal is recorded exactly as entered.
    """
    goal = {"name": g.get("name")}
    if g.get("description"):
        goal["description"] = g["description"]
    goal["type"] = _TYPE_TO_CRM.get(g.get("type"), g.get("type"))
    goal["nature"] = g.get("nature")
    if g.get("nature") != "Replenishing":
        goal["structure"] = g.get("structure")
    recurring = g.get("structure") == "Recurring"
    freq = g.get("frequency")
    if resolved:
        start = pd.Timestamp(g["start_date"])
        goal["start_date_mode"] = "Fixed"
        goal["start_date"] = start.strftime("%Y-%m-01")
        goal["amount"] = g.get("amount")
        if recurring:
            occ = int(g.get("occurrences") or 1)
            goal["frequency"] = _FREQ_TO_CRM.get(freq, freq)
            goal["occurrences"] = occ
            months = _FREQ_MONTHS.get(freq)
            if months:
                goal["end_date"] = (
                    start + pd.DateOffset(months=months * (occ - 1))
                ).strftime("%Y-%m-01")
    else:
        goal["start_date_mode"] = g.get("start_date_mode")
        goal["start_date"] = _iso_or_none(g.get("start_date"))
        goal["amount"] = g.get("amount")
        if recurring:
            goal["frequency"] = _FREQ_TO_CRM.get(freq, freq)
            goal["occurrences"] = g.get("occurrences")
            goal["end_mode"] = g.get("end_mode")
            goal["end_date"] = _iso_or_none(g.get("end_date"))
    goal["inflation_percent"] = g.get("inflation_percent")
    return goal


def build_inputs_json(config: dict, retirement_date=None) -> bytes:
    """Serialise the user's inputs as pretty JSON bytes; goals are CRM-ready.

    The goals array follows the CRM import contract (see _crm_goal): engine
    nature vocabulary, standardised casings and — when retirement_date is given
    (a successful run) — concrete dates and occurrence counts resolved by the
    engine's own _resolve_goals, so the file matches the simulation exactly.
    Everything else stays a faithful record of the form (the CRM reads only
    goals + personal.client_name + generated_at and ignores the rest):
    - dates become 'YYYY-MM-01' strings (the month grid);
    - the internal 'm3_id' the app injects for the engine is dropped.
    """
    goals_in = config.get("goals", []) or []
    if retirement_date is not None:
        death_date = pd.Timestamp(config["current_date"]) + pd.DateOffset(
            years=int(float(config.get("target_lifetime", 90))
                      - float(config.get("current_age", 30)))
        )
        goals_out = [
            _crm_goal(g, resolved=True)
            for g in _resolve_goals(goals_in, pd.Timestamp(retirement_date), death_date)
        ]
    else:
        goals_out = [_crm_goal(g, resolved=False) for g in goals_in]
    doc = {
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "simulation_id": config.get("simulation_id", ""),
        "engine_version": ENGINE_SOURCE_SHA,
        "glidepath_version": GLIDEPATH_VERSION,
        "personal": {
            "client_name": config.get("client_name", ""),
            "current_date": _iso_or_none(config.get("current_date")),
            "current_age": config.get("current_age"),
            "target_lifetime": config.get("target_lifetime"),
            "current_corpus": config.get("current_corpus"),
            "risk_profile": config.get("risk_profile"),
        },
        "investment_streams": [
            {
                "name": s.get("name"),
                "amount": s.get("amount"),
                "start_date": _iso_or_none(s.get("start_date")),
                "end_date_mode": s.get("end_date_mode"),
                "end_date": _iso_or_none(s.get("end_date")),
                "step_up_percent": s.get("step_up_percent"),
                "step_up_frequency": s.get("step_up_frequency"),
                "step_up_date": _iso_or_none(s.get("step_up_date")),
            }
            for s in config.get("investment_streams", []) or []
        ],
        "goals": goals_out,
        "one_time_investments": [
            {
                "name": w.get("name"),
                "date": _iso_or_none(w.get("date")),
                "amount": w.get("amount"),
            }
            for w in config.get("one_time_investments", []) or []
        ],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")


# ── Load a downloaded inputs JSON back into the form ────────────────────────
_TYPE_FROM_CRM = {v: k for k, v in _TYPE_TO_CRM.items()}
_FREQ_FROM_CRM = {v: k for k, v in _FREQ_TO_CRM.items()}


def form_state_from_inputs(doc: dict):
    """Map an inputs JSON (either export shape) back to form state.

    Tolerant on vocabulary: nature accepts the engine words
    (Replenishing/Non-replenishing) and the display words (Recurring/Lumpsum);
    type/frequency accept both our casing and the CRM casing. Resolved exports
    carry no end_mode — their concrete occurrences count maps to
    end_mode="Occurrences", which reproduces the same payout schedule.
    Returns (personal, streams, goals, one_time); raises ValueError on files
    that aren't an inputs JSON.
    """
    if not isinstance(doc, dict) or "personal" not in doc or "goals" not in doc:
        raise ValueError("not an inputs JSON (missing 'personal'/'goals')")
    today = month_start_today()

    def ts(v, fallback=None):
        if not v:
            return fallback
        try:
            t = pd.Timestamp(v)
            return pd.Timestamp(t.year, t.month, 1)
        except (ValueError, TypeError):
            return fallback

    p = doc.get("personal") or {}
    risk = p.get("risk_profile")
    personal = {
        "client_name": str(p.get("client_name") or ""),
        "current_date": ts(p.get("current_date"), today),
        "current_age": int(float(p.get("current_age") or 30)),
        "target_lifetime": int(float(p.get("target_lifetime") or 90)),
        "current_corpus": int(float(p.get("current_corpus") or 0)),
        "risk_profile": risk if risk in RISK_PROFILES else "Balanced",
    }

    streams = []
    for s in doc.get("investment_streams") or []:
        start = ts(s.get("start_date"), today)
        mode = s.get("end_date_mode")
        streams.append({
            "name": str(s.get("name") or f"Stream {len(streams) + 1}"),
            "amount": int(float(s.get("amount") or 0)),
            "start_date": start,
            "end_date_mode": mode if mode in INVESTMENT_END_MODES else "At retirement",
            "end_date": ts(s.get("end_date"), add_years(start, 20)),
            "step_up_percent": float(s.get("step_up_percent") or 0.0),
            "step_up_frequency": s.get("step_up_frequency")
            if s.get("step_up_frequency") in STEPUP_FREQUENCIES else "Annual",
            "step_up_date": ts(s.get("step_up_date"), start),
        })

    goals = []
    for g in doc.get("goals") or []:
        nature = NATURE_FROM_DISPLAY.get(g.get("nature"), g.get("nature"))
        if nature not in GOAL_NATURES:
            nature = "Non-replenishing"
        gtype = _TYPE_FROM_CRM.get(g.get("type"), g.get("type"))
        if gtype not in GOAL_TYPES:
            gtype = "Non-Negotiable"
        freq = _FREQ_FROM_CRM.get(g.get("frequency"), g.get("frequency"))
        end_mode = g.get("end_mode")
        if end_mode not in GOAL_END_MODES:
            end_mode = "Occurrences"
        mode = g.get("start_date_mode")
        # Structure: the file's value wins for Non-replenishing goals; files
        # from the rename era (no structure key, or display-vocab nature) fall
        # back on Lumpsum unless the goal is recurring by its fields.
        structure = g.get("structure")
        if structure not in GOAL_STRUCTURES:
            structure = "Recurring" if (g.get("frequency") or (g.get("occurrences") or 1) > 1) else "Lumpsum"
        goals.append(normalise_goal({
            "name": str(g.get("name") or f"Goal {len(goals) + 1}"),
            "description": str(g.get("description") or ""),
            "type": gtype,
            "nature": nature,
            "structure": structure,
            "start_date_mode": mode if mode in GOAL_START_MODES else "Fixed",
            "start_date": ts(g.get("start_date"), add_years(today, 15)),
            "amount": int(float(g.get("amount") or 0)),
            "frequency": freq if freq in RECURRING_FREQUENCIES else "Annual",
            "occurrences": int(float(g.get("occurrences") or 1)),
            "end_mode": end_mode,
            "end_date": ts(g.get("end_date")),
            "inflation_percent": float(g.get("inflation_percent") or 6.0),
        }))

    one_time = []
    for w in doc.get("one_time_investments") or []:
        one_time.append({
            "name": str(w.get("name") or f"Investment {len(one_time) + 1}"),
            "date": ts(w.get("date"), today),
            "amount": int(float(w.get("amount") or 0)),
        })
    return personal, streams, goals, one_time


_PERSONAL_WIDGET_KEYS = (
    "p_client", "p_curdate_m", "p_curdate_y", "p_age", "p_life", "p_corpus", "p_risk",
)


def _load_doc_into_form(doc: dict, source: str) -> None:
    """Shared by the JSON uploader and the version picker: replace form state."""
    personal, streams, goals, one_time = form_state_from_inputs(doc)
    for item in streams + goals + one_time:
        item["_uid"] = _next_uid()
    st.session_state.streams = streams
    st.session_state.goals = goals
    st.session_state.one_time = one_time
    st.session_state.personal_defaults = personal
    # Personal widgets re-initialise from the new defaults on the next render.
    for key in _PERSONAL_WIDGET_KEYS:
        st.session_state.pop(key, None)
    st.session_state.run_output = None
    st.session_state.upload_msg = (
        "success",
        f"Loaded {source} for “{personal['client_name'] or 'plan'}” — review and press Run simulation.",
    )


def _apply_uploaded_inputs():
    """st.button callback: load the uploaded JSON into the form."""
    file = st.session_state.get("inputs_uploader")
    if file is None:
        st.session_state.upload_msg = ("error", "Choose a JSON file first.")
        return
    try:
        doc = json.loads(file.getvalue().decode("utf-8"))
        _load_doc_into_form(doc, "inputs")
    except (ValueError, UnicodeDecodeError) as e:
        st.session_state.upload_msg = ("error", f"Could not read that file: {e}")


# ── Summary sheet (CM one-glance view) ──────────────────────────────────────
# Inserted as the FIRST sheet of the engine's finished workbook — the engine's
# own export code stays untouched. Any error here degrades to the original
# workbook rather than breaking the download.

def _goal_outflow_rows(config: dict, resolve_date, death_date) -> tuple[list, float]:
    """Per-goal totals using the engine's own tranche expansion (engine-exact)."""
    resolved = _resolve_goals(config.get("goals") or [], resolve_date, death_date)
    rows, grand = [], 0.0
    for g in resolved:
        tranches = expand_recurring_goal_to_tranches(g, config["current_date"])
        total = float(sum(fv for _d, fv in tranches))
        first = min((d for d, _fv in tranches), default=g.get("start_date"))
        fv_first = next((fv for d, fv in tranches if d == first), 0.0)
        grand += total
        rows.append({
            "name": g.get("name", ""), "type": g.get("type", ""),
            "nature": g.get("nature", ""), "structure": g.get("structure", ""),
            "starts": fmt_mon_yyyy(first) if first is not None else "—",
            "payments": len(tranches),
            "pv": float(g.get("amount", 0) or 0),
            "fv_first": fv_first,
            "total_fv": total,
        })
    return rows, grand


def add_summary_sheet(xlsx_bytes: bytes, config: dict, *, kind: str,
                      retirement_date=None, age_at_retirement=None, failure=None,
                      comp_df=None, snapshot=None, death_date=None) -> bytes:
    """Prepend a styled Summary sheet to the advisor workbook."""
    try:
        import io
        from openpyxl import load_workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

        NAVY, GOLD = "16294B", "C9A227"
        GREEN_BG, GREEN_TX = "E4F2E8", "1E6B3A"
        RED_BG, RED_TX = "FAE6E4", "9C2B23"
        GREY_BG = "F2F5F9"
        thin = Side(style="thin", color="C9D1DC")
        box = Border(left=thin, right=thin, top=thin, bottom=thin)

        wb = load_workbook(io.BytesIO(xlsx_bytes))
        ws = wb.create_sheet("Summary", 0)
        widths = {"A": 30, "B": 17, "C": 15, "D": 15, "E": 13, "F": 11,
                  "G": 15, "H": 16, "I": 16}
        for col, w in widths.items():
            ws.column_dimensions[col].width = w

        def put(cell, value, *, bold=False, size=11, color="1C2430", fill=None,
                align="left", italic=False, border=False):
            c = ws[cell]
            c.value = value
            c.font = Font(name="Calibri", bold=bold, size=size, color=color,
                          italic=italic)
            if fill:
                c.fill = PatternFill("solid", start_color=fill)
            c.alignment = Alignment(horizontal=align, vertical="center",
                                    wrap_text=True)
            if border:
                c.border = box
            return c

        # Title + identity
        ws.merge_cells("A1:I1")
        put("A1", "FINANCIAL PLAN — SUMMARY", bold=True, size=16,
            color="FFFFFF", fill=NAVY)
        ws.row_dimensions[1].height = 26
        ws.merge_cells("A2:I2")
        ident = [config.get("client_name") or "—"]
        if config.get("phone"):
            ident.append(f"phone {config['phone']}")
        ident += [
            f"run {pd.Timestamp.now(tz='Asia/Kolkata').strftime('%d %b %Y, %H:%M IST')}",
            f"risk profile {config.get('risk_profile', 'Balanced')} "
            f"({RISK_PROFILE_CORE_RETURNS.get(config.get('risk_profile'), 0.12) * 100:g}% core)",
            f"engine {ENGINE_SOURCE_SHA}",
        ]
        if config.get("simulation_id"):
            ident.append(config["simulation_id"])
        put("A2", "   ·   ".join(ident), italic=True, size=9, color="5A6472")

        # Verdict banner
        ws.merge_cells("A4:I4")
        ws.row_dimensions[4].height = 24
        if kind == "success":
            put("A4", f"FEASIBLE — earliest retirement {fmt_mon_yyyy(retirement_date)}"
                      f" (age {age_at_retirement:.1f})",
                bold=True, size=13, color=GREEN_TX, fill=GREEN_BG)
        else:
            f = failure or {}
            when = fmt_mon_yyyy(f["date"]) if f.get("date") else "—"
            put("A4", f"NOT FUNDABLE within the plan horizon — first failure "
                      f"{when}: {f.get('description', 'corpus depletion')} "
                      f"(diagnostic run at latest possible retirement)",
                bold=True, size=12, color=RED_TX, fill=RED_BG)

        # Key numbers
        put("A6", "KEY NUMBERS", bold=True, color=NAVY, fill=GREY_BG)
        ws.merge_cells("A6:I6")
        current_date = pd.Timestamp(config["current_date"])
        monthly_inv = sum(float(s.get("amount", 0) or 0)
                          for s in config.get("investment_streams") or []
                          if pd.Timestamp(s["start_date"]) <= current_date)
        pairs = [("Current corpus", format_inr(config.get("current_corpus", 0))),
                 ("Monthly investment today", format_inr(monthly_inv))]
        totals = None
        if comp_df is not None and not comp_df.empty:
            df = comp_df.copy()
            df["Date"] = pd.to_datetime(df["Date"])
            vc = [c for c in df.columns if c.endswith("Value")]
            totals = df[vc].fillna(0).sum(axis=1)
            peak_i = totals.idxmax()
            if kind == "success":
                years = (pd.Timestamp(retirement_date) - current_date).days / 365.25
                pairs.append(("Years to retirement", f"{years:.1f}"))
                if snapshot:
                    pairs.append(("Wealth at retirement", format_inr(snapshot["total"])))
            pairs += [
                ("Wealth at plan end", format_inr(float(totals.iloc[-1]))),
                ("Peak wealth", f"{format_inr(float(totals.loc[peak_i]))}"
                                f"  ({df['Date'].loc[peak_i].strftime('%b %Y')})"),
            ]
            if "Investment" in df.columns:
                upto = (df["Date"] <= pd.Timestamp(retirement_date)) \
                    if kind == "success" else pd.Series(True, index=df.index)
                pairs.append(("Total investments (to retirement)"
                              if kind == "success" else "Total investments (horizon)",
                              format_inr(float(df.loc[upto, "Investment"].fillna(0).sum()))))
        one_time_total = sum(float(w.get("amount", 0) or 0)
                             for w in config.get("one_time_investments") or [])
        if one_time_total:
            pairs.append(("One-time investments", format_inr(one_time_total)))
        r = 7
        for i, (label, value) in enumerate(pairs):
            col = "A" if i % 2 == 0 else "F"
            vcol = "B" if i % 2 == 0 else "G"
            put(f"{col}{r}", label, size=10, color="5A6472")
            put(f"{vcol}{r}", value, bold=True, size=10)
            if i % 2 == 1:
                r += 1
        if len(pairs) % 2 == 1:
            r += 1

        # Snapshot split (success only)
        if kind == "success" and snapshot:
            r += 1
            put(f"A{r}", "WEALTH AT RETIREMENT — SPLIT", bold=True, color=NAVY,
                fill=GREY_BG)
            ws.merge_cells(f"A{r}:I{r}")
            r += 1
            for label, key in [("Core corpus", "core"), ("Debt pool", "debt"),
                               ("Hybrid pool", "hybrid"),
                               ("Goal tranches (debt)", "goal_debt"),
                               ("Goal tranches (hybrid)", "goal_hybrid"),
                               ("Total", "total")]:
                put(f"A{r}", label, size=10,
                    color="5A6472" if key != "total" else "1C2430",
                    bold=(key == "total"))
                put(f"B{r}", format_inr(snapshot[key]), bold=(key == "total"), size=10)
                r += 1

        # Goals table
        r += 1
        put(f"A{r}", "GOALS", bold=True, color=NAVY, fill=GREY_BG)
        ws.merge_cells(f"A{r}:I{r}")
        r += 1
        resolve_date = pd.Timestamp(retirement_date) if kind == "success" \
            else pd.Timestamp(death_date)
        goal_rows, grand = _goal_outflow_rows(config, resolve_date, death_date)
        headers = ["Goal", "Type", "Nature", "Structure", "Starts", "Payments",
                   "Amount (today's ₹)", "Cost at start (FV)", "Total outflow (FV)"]
        for j, h in enumerate(headers):
            put(f"{chr(65 + j)}{r}", h, bold=True, size=9, color="FFFFFF",
                fill=NAVY, border=True)
        r += 1
        for g in goal_rows:
            vals = [g["name"], g["type"], g["nature"], g["structure"], g["starts"],
                    g["payments"], format_inr(g["pv"]), format_inr(g["fv_first"]),
                    format_inr(g["total_fv"])]
            for j, v in enumerate(vals):
                put(f"{chr(65 + j)}{r}", v, size=9, border=True,
                    align="right" if j >= 5 else "left")
            r += 1
        put(f"A{r}", "TOTAL", bold=True, size=9, border=True)
        for j in range(1, 8):
            put(f"{chr(65 + j)}{r}", "", border=True)
        put(f"I{r}", format_inr(grand), bold=True, size=9, border=True, align="right")
        r += 2

        # Money in vs money out
        put(f"A{r}", "MONEY IN vs MONEY OUT (full horizon, future value)",
            bold=True, color=NAVY, fill=GREY_BG)
        ws.merge_cells(f"A{r}:I{r}")
        r += 1
        if comp_df is not None and not comp_df.empty and "Investment" in comp_df.columns:
            inv_total = float(comp_df["Investment"].fillna(0).sum())
            put(f"A{r}", "Total investment inflows", size=10, color="5A6472")
            put(f"B{r}", format_inr(inv_total + one_time_total), bold=True, size=10)
            r += 1
        put(f"A{r}", "Total goal outflows", size=10, color="5A6472")
        put(f"B{r}", format_inr(grand), bold=True, size=10)
        r += 2
        put(f"A{r}", "Amounts are engine-computed: goal costs grown from today's "
                     "value at each goal's growth %; every figure matches the "
                     "detailed sheets that follow.",
            italic=True, size=8, color="5A6472")
        ws.merge_cells(f"A{r}:I{r}")

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except Exception:
        return xlsx_bytes  # never break the download over a summary problem


# ── Version history (Google Sheet) ──────────────────────────────────────────
# One spreadsheet row per simulation run, keyed by the client's phone number.
# The stored payload is the same inputs JSON the download/upload feature uses,
# so loading a version reuses the exact same restore path. The feature is
# DORMANT until Streamlit secrets provide [gcp_service_account] and
# versions_sheet_id — without them the UI hides and runs save nothing.
# sim_id/output_json were added 2026-08-25 and sit at the END on purpose:
# the header row auto-migrates in place, and appending columns keeps every
# pre-existing row aligned (older rows simply have the two cells empty).
VERSIONS_HEADER = ["phone", "client_name", "saved_at", "version", "note",
                   "result", "inputs_json", "sim_id", "output_json"]


def normalize_phone(raw: str) -> str | None:
    """Digits only; accept +91/0 prefixes by keeping the last 10 digits."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) > 10:
        digits = digits[-10:]
    return digits if len(digits) == 10 else None


@st.cache_resource(show_spinner=False)
def _cached_ws():
    try:
        creds = dict(st.secrets["gcp_service_account"])
        sheet_id = st.secrets["versions_sheet_id"]
    except (KeyError, FileNotFoundError):
        return None
    try:
        import gspread
        ws = gspread.service_account_from_dict(creds).open_by_key(sheet_id).sheet1
        if ws.row_values(1) != VERSIONS_HEADER:
            ws.update(values=[VERSIONS_HEADER], range_name="A1")
        return ws
    except Exception as e:
        st.session_state.versions_error = str(e)
        return None


def _versions_ws():
    """The worksheet, or None when the feature is not configured.

    Only successful connections stay cached: Streamlit Cloud applies new
    secrets WITHOUT restarting the app, so a None cached at boot (before the
    secrets existed) would otherwise stick until a manual reboot.
    """
    ws = _cached_ws()
    if ws is None:
        _cached_ws.clear()
    return ws


def versions_available() -> bool:
    return _versions_ws() is not None


@st.cache_data(ttl=45, show_spinner=False)
def fetch_versions(phone: str) -> list[dict]:
    """All saved versions for a phone, newest first."""
    ws = _versions_ws()
    if ws is None:
        return []
    rows = [r for r in ws.get_all_records() if str(r.get("phone", "")) == phone]
    return list(reversed(rows))


def result_summary(out: dict) -> str:
    if out["kind"] == "success":
        return (f"Retire {fmt_mon_yyyy(out['retirement_date'])} "
                f"(age {out['age_at_retirement']:.1f})")
    if out["kind"] == "infeasible":
        f = out.get("failure") or {}
        when = fmt_mon_yyyy(f["date"]) if f.get("date") else "?"
        return f"Infeasible — fails {when}"
    return "Validation error"


def save_version(phone: str, note: str, config: dict, out: dict) -> str | None:
    """Append one row for this run; returns the version label, or None."""
    ws = _versions_ws()
    if ws is None:
        return None
    try:
        version = f"v{len(fetch_versions(phone)) + 1}"
        saved_at = pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d %H:%M")
        ws.append_row(
            [phone, config.get("client_name", ""), saved_at, version,
             (note or "").strip(), result_summary(out),
             build_inputs_json(config).decode("utf-8"),
             config.get("simulation_id", ""),
             build_output_json(config, out).decode("utf-8")],
            value_input_option="RAW",
        )
        fetch_versions.clear()
        return version
    except Exception as e:
        st.session_state.versions_error = str(e)
        return None


def _apply_selected_version():
    """st.button callback: load the chosen saved version into the form."""
    rows = st.session_state.get("_version_rows") or []
    label = st.session_state.get("version_choice")
    row = next((r for r in rows if r["_label"] == label), None)
    if row is None:
        st.session_state.upload_msg = ("error", "Pick a version first.")
        return
    try:
        doc = json.loads(row["inputs_json"])
        _load_doc_into_form(doc, row["_label"])
    except (ValueError, KeyError) as e:
        st.session_state.upload_msg = ("error", f"Could not load that version: {e}")


# ── Widgets ─────────────────────────────────────────────────────────────────
def month_year_input(container, label: str, ts, key: str) -> pd.Timestamp:
    """Month + year picker (the engine's grid is monthly — day is always the 1st)."""
    ts = pd.Timestamp(ts) if ts is not None else month_start_today()
    c1, c2 = container.columns([3, 2])
    month = c1.selectbox(f"{label} — month", MONTH_NAMES, index=ts.month - 1, key=f"{key}_m")
    year = c2.number_input(f"{label} — year", min_value=1950, max_value=2150,
                           value=int(ts.year), step=1, key=f"{key}_y")
    return pd.Timestamp(int(year), MONTH_NAMES.index(month) + 1, 1)


def money_input(container, label: str, value, key: str, help: str | None = None) -> int:
    amt = container.number_input(label, min_value=0, value=int(value), step=50_000,
                                 key=key, help=help)
    container.caption(inr_hint(amt))
    return amt


def _next_uid() -> int:
    st.session_state.uid_counter += 1
    return st.session_state.uid_counter


def init_state() -> None:
    if "streams" in st.session_state:
        return
    today = month_start_today()
    st.session_state.uid_counter = 0
    st.session_state.today = today
    st.session_state.streams = [make_default_stream(0, today)]
    st.session_state.goals = [make_goal_from_template("retirement_income", 0, today)]
    st.session_state.one_time = []
    for item in st.session_state.streams + st.session_state.goals:
        item["_uid"] = _next_uid()
    st.session_state.run_output = None
    st.session_state.personal_defaults = {
        "client_name": "",
        "current_date": today,
        "current_age": 30,
        "target_lifetime": 90,
        "current_corpus": 10_000_000,
        "risk_profile": "Balanced",
    }


# ── Form sections ───────────────────────────────────────────────────────────
def render_stream(s: dict) -> None:
    uid = s["_uid"]
    r1c1, r1c2 = st.columns([2, 2])
    s["name"] = r1c1.text_input("Name", value=s["name"], key=f"st_name_{uid}")
    s["amount"] = money_input(r1c2, "Monthly amount (₹, as of start date)", s["amount"], f"st_amt_{uid}")
    s["start_date"] = month_year_input(st, "Starts", s["start_date"], f"st_start_{uid}")
    r2c1, r2c2 = st.columns([2, 2])
    s["end_date_mode"] = r2c1.selectbox(
        "Ends", INVESTMENT_END_MODES,
        index=INVESTMENT_END_MODES.index(s["end_date_mode"]), key=f"st_endmode_{uid}",
    )
    if s["end_date_mode"] == "Fixed":
        s["end_date"] = month_year_input(r2c2, "End", s["end_date"], f"st_end_{uid}")
    r3c1, r3c2 = st.columns([2, 2])
    s["step_up_percent"] = r3c1.number_input(
        "Step-up %", min_value=0.0, max_value=100.0, value=float(s["step_up_percent"]),
        step=0.5, key=f"st_supct_{uid}",
    )
    s["step_up_frequency"] = r3c2.selectbox(
        "Step-up frequency", STEPUP_FREQUENCIES,
        index=STEPUP_FREQUENCIES.index(s["step_up_frequency"]), key=f"st_sufreq_{uid}",
    )
    s["step_up_date"] = month_year_input(st, "Step-up anchor", s["step_up_date"], f"st_sudate_{uid}")


def render_goal(g: dict) -> None:
    uid = g["_uid"]
    r1c1, r1c2 = st.columns([2, 2])
    g["name"] = r1c1.text_input("Name", value=g["name"], key=f"g_name_{uid}")
    g["description"] = r1c2.text_input("Description", value=g["description"], key=f"g_desc_{uid}")

    r2c1, r2c2, r2c3 = st.columns(3)
    g["nature"] = r2c1.selectbox(
        "Nature", GOAL_NATURES, index=GOAL_NATURES.index(g["nature"]),
        key=f"g_nature_{uid}",
        help="Replenishing = an ongoing payout stream (e.g. retirement income), "
             "funded via the Debt/Hybrid pools — always Recurring. "
             "Non-replenishing = a save-up goal provisioned via a glide path; "
             "it can be a one-time Lumpsum or Recurring (e.g. annual fees).",
    )
    if g["nature"] == "Replenishing":
        g["structure"] = "Recurring"
        r2c2.caption("Structure: **Recurring** (always, for Replenishing)")
    else:
        g["structure"] = r2c2.selectbox(
            "Structure", GOAL_STRUCTURES,
            index=GOAL_STRUCTURES.index(g["structure"] if g["structure"] in GOAL_STRUCTURES else "Lumpsum"),
            key=f"g_struct_{uid}",
        )
    # Type shows for BOTH natures (2026-08-21): the CRM stores a priority on
    # every goal. The engine reads it only for Non-replenishing goals (glide
    # sheet selection); on Replenishing goals it is metadata for the exports.
    g["type"] = r2c3.selectbox(
        "Type", GOAL_TYPES, index=GOAL_TYPES.index(g["type"]), key=f"g_type_{uid}",
        help="Goal priority. For Non-replenishing goals it also selects the "
             "glide-path sheet used to provision the goal.",
    )

    r3c1, r3c2 = st.columns([2, 2])
    g["start_date_mode"] = r3c1.selectbox(
        "Start", GOAL_START_MODES, index=GOAL_START_MODES.index(g["start_date_mode"]),
        key=f"g_startmode_{uid}",
    )
    if g["start_date_mode"] == "Fixed":
        g["start_date"] = month_year_input(r3c2, "Start date", g["start_date"], f"g_start_{uid}")

    r4c1, r4c2 = st.columns([2, 2])
    g["amount"] = money_input(
        r4c1, "Amount (today's ₹)", g["amount"], f"g_amt_{uid}",
        help="Present value — the engine grows it to the goal date at the growth % below.",
    )
    g["inflation_percent"] = r4c2.number_input(
        "Annual growth %", min_value=0.0, max_value=100.0,
        value=float(g["inflation_percent"]), step=0.5, key=f"g_infl_{uid}",
    )

    if g["structure"] == "Recurring":
        r5c1, r5c2, r5c3 = st.columns(3)
        g["frequency"] = r5c1.selectbox(
            "Frequency", RECURRING_FREQUENCIES,
            index=RECURRING_FREQUENCIES.index(g["frequency"] if g["frequency"] in RECURRING_FREQUENCIES else "Monthly"),
            key=f"g_freq_{uid}",
        )
        g["end_mode"] = r5c2.selectbox(
            "End mode", GOAL_END_MODES,
            index=GOAL_END_MODES.index(g["end_mode"] if g["end_mode"] in GOAL_END_MODES else "Occurrences"),
            key=f"g_endmode_{uid}",
        )
        if g["end_mode"] == "Occurrences":
            g["occurrences"] = r5c3.number_input(
                "Number of payments", min_value=1, value=int(g["occurrences"] or 1),
                step=1, key=f"g_occ_{uid}",
            )
        elif g["end_mode"] == "Fixed date":
            g["end_date"] = month_year_input(
                r5c3, "End date", g["end_date"] or g["start_date"], f"g_end_{uid}"
            )
        if g["nature"] == "Non-replenishing":
            st.caption("Non-replenishing recurring goals may span at most 4 years "
                       "first-to-last payment (engine performance guard).")


def render_one_time(w: dict) -> None:
    uid = w["_uid"]
    c1, c2 = st.columns([2, 2])
    w["name"] = c1.text_input("Name", value=w["name"], key=f"ot_name_{uid}")
    w["amount"] = money_input(c2, "Amount (₹ on that date)", w["amount"], f"ot_amt_{uid}")
    w["date"] = month_year_input(st, "Date", w["date"], f"ot_date_{uid}")


def build_config(personal: dict) -> dict:
    """Assemble the engine's plain-dict config (mirror planForm.buildConfig)."""
    streams = []
    for s in st.session_state.streams:
        streams.append({
            "name": s["name"],
            "amount": float(s["amount"]),
            "start_date": s["start_date"],
            "end_date_mode": s["end_date_mode"],
            "end_date": s["end_date"] if s["end_date_mode"] == "Fixed" else None,
            "step_up_percent": float(s["step_up_percent"]),
            "step_up_frequency": s["step_up_frequency"],
            "step_up_date": s["step_up_date"],
        })
    goals = []
    for raw in st.session_state.goals:
        g = normalise_goal(raw)
        goals.append({
            "name": g["name"],
            "description": g["description"] or "",
            "type": g["type"],
            "nature": g["nature"],
            "structure": g["structure"],
            "start_date_mode": g["start_date_mode"],
            "start_date": None if g["start_date_mode"] == "At retirement" else g["start_date"],
            "amount": float(g["amount"]),
            "frequency": g["frequency"],
            "occurrences": int(g["occurrences"]) if g.get("occurrences") is not None else None,
            "end_mode": g["end_mode"],
            "end_date": g["end_date"],
            "inflation_percent": float(g["inflation_percent"]),
        })
    one_time = [
        {"name": w["name"], "date": w["date"], "amount": float(w["amount"])}
        for w in st.session_state.one_time
    ]
    return {
        "current_date": personal["current_date"],
        "current_age": float(personal["current_age"]),
        "target_lifetime": float(personal["target_lifetime"]),
        "current_corpus": float(personal["current_corpus"]),
        "risk_profile": personal["risk_profile"],
        # Read only by the Excel export headers — the engine ignores these.
        "client_name": personal["client_name"] or "Playground",
        "m3_id": "playground",
        "phone": personal.get("phone") or "",
        "investment_streams": streams,
        "goals": goals,
        "one_time_investments": one_time,
    }


def build_output_json(config: dict, out: dict) -> bytes:
    """Compact result record for the simulation log (pairs with the inputs JSON).

    Deliberately headline-level — verdict, dates, snapshot, key wealth figures,
    failure/validation detail — not the monthly ledger (which lives in the
    CSV/Excel and would not fit a sheet cell).
    """
    doc = {
        "simulation_id": config.get("simulation_id", ""),
        "generated_at": pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d %H:%M:%S"),
        "engine_version": ENGINE_SOURCE_SHA,
        "glidepath_version": GLIDEPATH_VERSION,
        "client_name": config.get("client_name", ""),
        "phone": config.get("phone", ""),
        "risk_profile": config.get("risk_profile", ""),
        "verdict": out["kind"],
    }
    if out["kind"] == "success":
        wealth = out.get("wealth")
        doc.update({
            "retirement_date": _iso_or_none(out["retirement_date"]),
            "age_at_retirement": round(out["age_at_retirement"], 1),
            "snapshot_at_retirement": out.get("snapshot"),
        })
        if wealth is not None and not wealth.empty:
            total = wealth["Total wealth"]
            doc["wealth_at_plan_end"] = round(float(total.iloc[-1]), 2)
            doc["peak_wealth"] = {
                "amount": round(float(total.max()), 2),
                "date": total.idxmax().strftime("%Y-%m-01"),
            }
    elif out["kind"] == "infeasible":
        f = out.get("failure") or {}
        doc["failure"] = {
            "date": _iso_or_none(f.get("date")),
            "description": str(f.get("description", "Corpus depletion")),
        }
    else:
        doc["validation_errors"] = list(out.get("errors") or [])
    return json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")


def run_plan(config: dict) -> dict:
    """Solve + simulate; returns everything the results pane needs."""
    config["simulation_id"] = new_simulation_id()
    instrument_params = resolve_instrument_params(config["risk_profile"])
    glide_paths = get_glide_paths()
    current_date = pd.Timestamp(config["current_date"])
    death_date = current_date + pd.DateOffset(
        years=int(config["target_lifetime"] - config["current_age"])
    )

    try:
        solved = find_retirement_date(config, instrument_params, glide_paths)
    except PlanValidationError as e:
        return {"kind": "invalid", "errors": list(e.errors)}

    if not solved["success"]:
        # Infeasible: diagnostic run at the lifetime end (mirrors service.py).
        # Its outputs are still worth downloading — the workbook carries the
        # failure rows, and the comprehensive view exists for the corpus-
        # depletion failure class (it is empty by design for debt-pool
        # depletion, in which case the CSV is simply not offered).
        _s, _t, failure, pools_df, goal_dfs, comp_df = run_simulation(
            config, death_date, instrument_params, glide_paths
        )
        diag_result = {"success": False, "retirement_date": None, "failure": failure}
        try:
            workbook = build_advisor_workbook(
                config, diag_result, comprehensive_df=comp_df, snapshot=None,
                goal_dfs=goal_dfs, pool_movements_df=pools_df,
            )
            workbook = add_summary_sheet(
                workbook, config, kind="infeasible", failure=failure,
                comp_df=comp_df, death_date=death_date,
            )
        except Exception:
            workbook = None  # degrade to no workbook rather than a dead screen
        return {
            "kind": "infeasible",
            "config": config,
            "failure": failure,
            "solver_failure": solved.get("failure"),
            "goal_table": build_goal_results(config, None),
            "workbook": workbook,
            "csv": csv_with_summary(comp_df)
            if comp_df is not None and not comp_df.empty else None,
        }

    retirement_date = pd.Timestamp(solved["retirement_date"])
    success, _trans, failure, pools_df, goal_dfs, comp_df = run_simulation(
        config, retirement_date, instrument_params, glide_paths
    )
    snapshot = build_snapshot(comp_df, retirement_date) if not comp_df.empty else None
    wealth = wealth_frame(comp_df, death_date)
    age_at_ret = float(config["current_age"]) + (retirement_date - current_date).days / 365.25

    workbook = build_advisor_workbook(
        config, solved, comprehensive_df=comp_df, snapshot=snapshot,
        goal_dfs=goal_dfs, pool_movements_df=pools_df,
    )
    workbook = add_summary_sheet(
        workbook, config, kind="success", retirement_date=retirement_date,
        age_at_retirement=age_at_ret, comp_df=comp_df, snapshot=snapshot,
        death_date=death_date,
    )
    return {
        "kind": "success",
        "config": config,
        "retirement_date": retirement_date,
        "age_at_retirement": age_at_ret,
        "snapshot": snapshot,
        "wealth": wealth,
        "goal_table": build_goal_results(config, retirement_date),
        "workbook": workbook,
        "csv": csv_with_summary(comp_df),
    }


# ── Results pane ────────────────────────────────────────────────────────────
def render_results(out: dict) -> None:
    if out["kind"] == "invalid":
        st.error("The plan inputs failed validation:\n\n" +
                 "\n".join(f"- {e}" for e in out["errors"]))
        return

    if out["kind"] == "infeasible":
        st.error("This plan is **not fundable** within the target lifetime — even "
                 "retiring at the very end of the plan horizon, the corpus fails.")
        failure = out.get("failure") or {}
        if failure:
            st.warning(
                f"First failure: **{fmt_mon_yyyy(failure.get('date'))}** — "
                f"{failure.get('description', 'Corpus depletion')}"
            )
        if not out["goal_table"].empty:
            st.dataframe(out["goal_table"], use_container_width=True, hide_index=True)
        st.caption("Try: lower goal amounts, later goal dates, higher investments, "
                   "or a more aggressive risk profile.")
        st.subheader("Downloads")
        d1, d2, d3 = st.columns(3)
        if out.get("workbook"):
            d1.download_button(
                "📗 Advisor workbook (Excel)", data=out["workbook"],
                file_name="financial_plan_advisor.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_wb_infeasible",
            )
        if out.get("csv"):
            d2.download_button(
                "📄 Comprehensive monthly (CSV)", data=out["csv"],
                file_name="financial_plan_monthly.csv", mime="text/csv",
                key="dl_csv_infeasible",
            )
        d3.download_button(
            "📥 Inputs (JSON)", data=build_inputs_json(out["config"]),
            file_name=inputs_filename(out["config"]), mime="application/json",
            key="dl_inputs_infeasible",
        )
        st.caption(
            "Diagnostic outputs: no feasible retirement exists, so the workbook/CSV "
            "reflect a run at the latest possible retirement (lifetime end), "
            "including the failure and shortfall rows."
        )
        return

    ret = out["retirement_date"]
    snap = out["snapshot"] or {}
    st.success(f"Earliest feasible retirement: **{fmt_mon_yyyy(ret)}** "
               f"(age {out['age_at_retirement']:.1f})")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Retirement", fmt_mon_yyyy(ret))
    m2.metric("Age at retirement", f"{out['age_at_retirement']:.1f}")
    m3.metric("Wealth at retirement", short_inr(snap.get("total", 0)))
    if not out["wealth"].empty:
        m4.metric("Wealth at lifetime end", short_inr(float(out["wealth"]["Total wealth"].iloc[-1])))

    st.line_chart(out["wealth"], use_container_width=True)

    with st.expander("Wealth snapshot at retirement"):
        if snap:
            rows = [
                ("Core corpus", snap["core"]), ("Debt pool", snap["debt"]),
                ("Hybrid pool", snap["hybrid"]), ("Goal debt tranches", snap["goal_debt"]),
                ("Goal hybrid tranches", snap["goal_hybrid"]), ("Total", snap["total"]),
            ]
            st.table(pd.DataFrame(
                [(k, format_inr(v)) for k, v in rows], columns=["Bucket", "Value"]
            ))

    st.subheader("Goals")
    st.dataframe(out["goal_table"], use_container_width=True, hide_index=True)

    st.subheader("Downloads")
    d1, d2, d3 = st.columns(3)
    d1.download_button(
        "📗 Advisor workbook (Excel)", data=out["workbook"],
        file_name="financial_plan_advisor.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    d2.download_button(
        "📄 Comprehensive monthly (CSV)", data=out["csv"],
        file_name="financial_plan_monthly.csv", mime="text/csv",
    )
    d3.download_button(
        "📥 Inputs (JSON)", data=build_inputs_json(out["config"], out["retirement_date"]),
        file_name=inputs_filename(out["config"]), mime="application/json",
        key="dl_inputs_success",
    )
    st.caption(
        "The JSON's goals carry concrete dates from this run — 'At retirement' "
        "becomes the solved date, Lifetime becomes a payment count — ready for CRM import."
    )


# ── Page ────────────────────────────────────────────────────────────────────
def main() -> None:
    st.set_page_config(page_title="Financial Planning Playground", page_icon="🧮", layout="wide")
    init_state()
    today = st.session_state.today

    st.title("🧮 Financial Planning Playground")
    st.caption(
        f"Engine `{ENGINE_SOURCE_SHA}` · glide paths v{GLIDEPATH_VERSION} — "
        "byte-identical copy of the production Financial Plan engine (CRM handoff, 2026-07-17)."
    )

    # Sidebar: personal & corpus + risk profile. Defaults come from
    # personal_defaults so a loaded JSON can re-seed every widget.
    d = st.session_state.personal_defaults
    with st.sidebar:
        st.header("Personal & Corpus")
        client_name = st.text_input("Client name (Excel header only)",
                                    value=d["client_name"], key="p_client")
        current_date = month_year_input(st, "Plan start", d["current_date"], "p_curdate")
        c1, c2 = st.columns(2)
        current_age = c1.number_input("Current age", min_value=0, max_value=110,
                                      value=int(d["current_age"]), step=1, key="p_age")
        target_lifetime = c2.number_input("Target lifetime", min_value=1, max_value=120,
                                          value=int(d["target_lifetime"]), step=1, key="p_life")
        current_corpus = money_input(st, "Current corpus (₹)", d["current_corpus"], "p_corpus")
        risk_profile = st.selectbox("Risk profile", RISK_PROFILES,
                                    index=RISK_PROFILES.index(d["risk_profile"]), key="p_risk")
        st.caption(
            f"Core-corpus return the engine will use: "
            f"**{RISK_PROFILE_CORE_RETURNS[risk_profile] * 100:g}%** · "
            "fixed pool returns: debt 6%, hybrid 10%."
        )
        st.divider()
        st.header("Client versions")
        phone_raw = st.text_input("Client phone number", key="p_phone",
                                  placeholder="10-digit mobile",
                                  help="Keys the saved-version history. Every Run "
                                       "with a valid number saves a version.")
        phone = normalize_phone(phone_raw)
        note = ""
        if phone_raw and phone is None:
            st.caption(":red[Enter a 10-digit mobile number.]")
        if versions_available():
            if phone:
                note = st.text_input("Note for next save (optional)", key="p_note",
                                     placeholder="e.g. final shown to client")
                rows = fetch_versions(phone)
                for r in rows:
                    bits = [str(r.get("version", "")), str(r.get("saved_at", ""))[5:],
                            str(r.get("result", ""))]
                    if r.get("note"):
                        bits.append(str(r["note"]))
                    r["_label"] = " · ".join(b for b in bits if b)
                st.session_state._version_rows = rows
                if rows:
                    st.selectbox(f"{len(rows)} saved version(s)",
                                 [r["_label"] for r in rows], key="version_choice")
                    st.button("Load selected version", use_container_width=True,
                              on_click=_apply_selected_version)
                else:
                    st.caption("No versions yet for this number — run a "
                               "simulation to save the first one.")
        else:
            err = st.session_state.get("versions_error")
            st.caption("Version history is not configured yet"
                       + (f" (error: {err})" if err else "")
                       + " — runs are not being saved.")
        st.divider()
        with st.expander("📂 Load inputs JSON"):
            st.file_uploader(
                "A financial_plan_inputs_*.json downloaded from this app",
                type=["json"], key="inputs_uploader",
            )
            st.button("Load into form", on_click=_apply_uploaded_inputs,
                      use_container_width=True)
        msg = st.session_state.pop("upload_msg", None)
        if msg is not None:
            (st.success if msg[0] == "success" else st.error)(msg[1])

    personal = {
        "client_name": client_name,
        "current_date": current_date,
        "current_age": current_age,
        "target_lifetime": target_lifetime,
        "current_corpus": current_corpus,
        "risk_profile": risk_profile,
        "phone": phone,
    }

    left, right = st.columns([1, 1], gap="large")

    with left:
        st.subheader("Investment streams")
        for i, s in enumerate(list(st.session_state.streams)):
            with st.expander(f"💰 {s['name'] or f'Stream {i + 1}'}", expanded=(len(st.session_state.streams) == 1)):
                render_stream(s)
                if st.button("Remove stream", key=f"rm_st_{s['_uid']}"):
                    st.session_state.streams.remove(s)
                    st.rerun()
        if st.button("➕ Add stream"):
            new = make_default_stream(len(st.session_state.streams), today)
            new["_uid"] = _next_uid()
            st.session_state.streams.append(new)
            st.rerun()

        st.subheader("One-time investments")
        for w in list(st.session_state.one_time):
            with st.expander(f"🪙 {w['name'] or 'One-time investment'}", expanded=True):
                render_one_time(w)
                if st.button("Remove", key=f"rm_ot_{w['_uid']}"):
                    st.session_state.one_time.remove(w)
                    st.rerun()
        if st.button("➕ Add one-time investment"):
            st.session_state.one_time.append(
                {"_uid": _next_uid(), "name": f"Investment {len(st.session_state.one_time) + 1}",
                 "date": today, "amount": 500_000}
            )
            st.rerun()

    with right:
        st.subheader("Goals")
        for i, g in enumerate(list(st.session_state.goals)):
            with st.expander(f"🎯 {g['name'] or f'Goal {i + 1}'}", expanded=(len(st.session_state.goals) == 1)):
                render_goal(g)
                if st.button("Remove goal", key=f"rm_g_{g['_uid']}"):
                    st.session_state.goals.remove(g)
                    st.rerun()
        t1, t2 = st.columns([2, 1])
        template_label = t1.selectbox("Goal template", list(GOAL_TEMPLATES.keys()),
                                      label_visibility="collapsed")
        if t2.button("➕ Add goal"):
            new = make_goal_from_template(
                GOAL_TEMPLATES[template_label], len(st.session_state.goals), today
            )
            new["_uid"] = _next_uid()
            st.session_state.goals.append(new)
            st.rerun()

    st.divider()
    if st.button("▶ Run simulation", type="primary", use_container_width=True):
        config = build_config(personal)
        with st.spinner("Solving for the earliest feasible retirement date…"):
            st.session_state.run_output = run_plan(config)
        # Auto-save this run as a version when a valid phone number is set.
        if phone and versions_available():
            version = save_version(phone, note, config, st.session_state.run_output)
            if version:
                st.toast(f"Saved {version} for {phone}", icon="💾")
            else:
                st.warning("This run could not be saved to the version history"
                           + (f": {st.session_state.get('versions_error')}"
                              if st.session_state.get("versions_error") else "."))

    if st.session_state.run_output is not None:
        out = st.session_state.run_output
        sim_id = (out.get("config") or {}).get("simulation_id", "")
        st.caption("Results reflect the inputs at the last Run — re-run after editing."
                   + (f"  ·  {sim_id}" if sim_id else ""))
        render_results(out)


if __name__ == "__main__":
    main()
