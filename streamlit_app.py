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
import sys
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
from app.planning.engine import _DEFAULT_INSTRUMENT_PARAMS, format_inr
from app.planning.schemas import RISK_PROFILE_CORE_RETURNS

# ── Picklists (mirror planForm.js) ──────────────────────────────────────────
GOAL_TYPES = ["Non-Negotiable", "Semi-Negotiable", "Negotiable"]
GOAL_NATURES = ["Non-replenishing", "Replenishing"]
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
            nature="Replenishing", structure="Recurring", type="Non-Negotiable",
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

    Playground rule: structure is derived from nature, never chosen —
    Replenishing (many payouts) is always Recurring, Non-replenishing (a single
    payout) is always Lumpsum. The engine still accepts the other two
    combinations; this UI layer simply never produces them.
    """
    g = dict(goal)
    g["structure"] = "Recurring" if g["nature"] == "Replenishing" else "Lumpsum"
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
        "Nature", GOAL_NATURES, index=GOAL_NATURES.index(g["nature"]), key=f"g_nature_{uid}",
        help="Non-replenishing = a single one-time payout (provisioned via a glide path). "
             "Replenishing = multiple payouts over time (funded via the Debt/Hybrid pools).",
    )
    # Structure is derived from nature, not chosen: Replenishing -> Recurring,
    # Non-replenishing -> Lumpsum. The engine still accepts the other
    # combinations; the playground UI just doesn't offer them.
    if g["nature"] == "Replenishing":
        g["structure"] = "Recurring"
        r2c2.caption("Structure: **Recurring**")
    else:
        g["structure"] = "Lumpsum"
        r2c2.caption("Structure: **Lumpsum**")
        g["type"] = r2c3.selectbox(
            "Type", GOAL_TYPES, index=GOAL_TYPES.index(g["type"]), key=f"g_type_{uid}",
            help="Selects the glide-path sheet used to provision this goal.",
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
        # Read only by the Excel export headers — the engine ignores both.
        "client_name": personal["client_name"] or "Playground",
        "m3_id": "playground",
        "investment_streams": streams,
        "goals": goals,
        "one_time_investments": one_time,
    }


def run_plan(config: dict) -> dict:
    """Solve + simulate; returns everything the results pane needs."""
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
        _s, _t, failure, _p, _g, _c = run_simulation(
            config, death_date, instrument_params, glide_paths
        )
        return {
            "kind": "infeasible",
            "config": config,
            "failure": failure,
            "solver_failure": solved.get("failure"),
            "goal_table": build_goal_results(config, None),
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
    d1, d2 = st.columns(2)
    d1.download_button(
        "📗 Advisor workbook (Excel)", data=out["workbook"],
        file_name="financial_plan_advisor.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    d2.download_button(
        "📄 Comprehensive monthly (CSV)", data=out["csv"],
        file_name="financial_plan_monthly.csv", mime="text/csv",
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

    # Sidebar: personal & corpus + risk profile
    with st.sidebar:
        st.header("Personal & Corpus")
        client_name = st.text_input("Client name (Excel header only)", value="")
        current_date = month_year_input(st, "Plan start", today, "p_curdate")
        c1, c2 = st.columns(2)
        current_age = c1.number_input("Current age", min_value=0, max_value=110, value=30, step=1)
        target_lifetime = c2.number_input("Target lifetime", min_value=1, max_value=120, value=90, step=1)
        current_corpus = money_input(st, "Current corpus (₹)", 10_000_000, "p_corpus")
        risk_profile = st.selectbox("Risk profile", RISK_PROFILES,
                                    index=RISK_PROFILES.index("Balanced"))
        st.caption(
            f"Core-corpus return the engine will use: "
            f"**{RISK_PROFILE_CORE_RETURNS[risk_profile] * 100:g}%** · "
            "fixed pool returns: debt 6%, hybrid 10%."
        )

    personal = {
        "client_name": client_name,
        "current_date": current_date,
        "current_age": current_age,
        "target_lifetime": target_lifetime,
        "current_corpus": current_corpus,
        "risk_profile": risk_profile,
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

    if st.session_state.run_output is not None:
        st.caption("Results reflect the inputs at the last Run — re-run after editing.")
        render_results(st.session_state.run_output)


if __name__ == "__main__":
    main()
