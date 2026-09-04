"""Goal parking - upload a CRM purposes export, see what the grid requires.

For the advisory head planning transitions: per goal, whether anything should
already be parked, how much sits where, and every dated movement ahead. Shows
the model's truth - lump movements out of the core corpus - not a SIP
translation (operator decision, 2026-09-04).
"""
import io
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))

from app.planning import ENGINE_SOURCE_SHA  # noqa: E402
from app.planning.engine import (  # noqa: E402
    _DEFAULT_INSTRUMENT_PARAMS, LTCG_GRACE_DAYS, format_inr,
)
from app.planning.goal_grid import GOAL_REACH_YEARS  # noqa: E402
from app.planning.parking import (  # noqa: E402
    STATUS_COMPLETED, STATUS_NOT_STARTED, plan_purposes,
)

st.set_page_config(page_title="Goal parking", page_icon="🅿️", layout="wide")


def short_inr(v) -> str:
    v = float(v)
    if v >= 1e7:
        return f"₹{v / 1e7:.2f} Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:.2f} L"
    return format_inr(v)


def mon(ts) -> str:
    return pd.Timestamp(ts).strftime("%b %Y")


def month_start_today() -> pd.Timestamp:
    now = pd.Timestamp.today()
    return pd.Timestamp(now.year, now.month, 1)


def read_purposes(upload) -> pd.DataFrame:
    name = (upload.name or "").lower()
    raw = upload.getvalue()
    if name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(raw))
    else:
        df = pd.read_csv(io.BytesIO(raw))
    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = {"goal_name", "goal_negotiability", "amount_per_occurrence",
               "occurrences", "start_date", "inflation"} - set(df.columns)
    if missing:
        raise ValueError("not a CRM purposes export - missing " + ", ".join(sorted(missing)))
    return df


def movement_label(kind, bucket) -> str:
    if kind == "switch":
        return "Switch hybrid → debt"
    return f"Move into {bucket}"


def payments_label(row) -> str:
    n = int(row["payments"])
    if n == 1:
        return mon(row["first_payment"])
    return f"{n} payments, {mon(row['first_payment'])} – {mon(row['last_payment'])}"


DETAIL_MONTHS = 12   # month-by-month for the first year; yearly rollup after


def schedule_frame(monthly: pd.DataFrame) -> pd.DataFrame:
    if monthly.empty:
        return monthly
    return pd.DataFrame({
        "Month": monthly["month"].map(mon),
        "Movement": [movement_label(k, b) for k, b in zip(monthly["kind"], monthly["bucket"])],
        "Amount": monthly["amount"].map(format_inr),
        "For payment(s) on": monthly.apply(payments_label, axis=1),
        "Delivers at payment (after tax)": monthly["delivers"].map(format_inr),
    })


def yearly_frame(monthly: pd.DataFrame) -> pd.DataFrame:
    """Long series (an income goal moves money every month for decades) roll
    up to one row per year so the screen stays readable; the CSV keeps every
    month."""
    if monthly.empty:
        return monthly
    m = monthly.assign(year=monthly["month"].dt.year)
    rows = []
    for year, grp in m.groupby("year", sort=True):
        adds = grp[grp["kind"] == "add"]
        sw = grp[grp["kind"] == "switch"]
        debt = adds.loc[adds["bucket"] == "debt", "amount"].sum()
        hyb = adds.loc[adds["bucket"] == "hybrid", "amount"].sum()
        rows.append({
            "Year": int(year),
            "Into debt": format_inr(debt) if debt > 0.5 else "—",
            "Into hybrid": format_inr(hyb) if hyb > 0.5 else "—",
            "Switched hybrid → debt": format_inr(sw["amount"].sum()) if len(sw) else "—",
            "Movements": int(len(grp)),
            "Delivers at payments (after tax)": format_inr(adds["delivers"].sum()),
        })
    return pd.DataFrame(rows)


def goal_split_frame(plans, as_of: pd.Timestamp) -> pd.DataFrame:
    """The first look: every active goal, what it needs NOW, and its next move.
    Goals that need nothing yet say so, with their first movement month."""
    rows = []
    for p in plans:
        d, h, sw = p["due_now"]["debt"], p["due_now"]["hybrid"], p.get("switch_now", 0.0)
        nm = p.get("next_move")
        if p["status"] == STATUS_COMPLETED:
            status, nxt = "All payments in the past", "—"
        elif p["status"] == STATUS_NOT_STARTED:
            status = f"Nothing due yet · first movement {mon(p['first_move'])}"
            nxt = mon(p["first_move"])
        else:
            status = "Due now"
            nxt = mon(nm["month"]) if nm else "—"
        rows.append({
            "Goal": p["name"],
            "Should be in debt now": short_inr(d) if d > 0.5 else "—",
            "Should be in hybrid now": short_inr(h) if h > 0.5 else "—",
            "Switch hybrid → debt now": short_inr(sw) if sw > 0.5 else "—",
            "Next movement": nxt,
            "Status": status,
        })
    return pd.DataFrame(rows)


def split_schedule(monthly: pd.DataFrame, as_of: pd.Timestamp):
    """(detail, rollup): month-level for the first DETAIL_MONTHS, yearly after."""
    if monthly.empty:
        return monthly, monthly
    cutoff = as_of + pd.DateOffset(months=DETAIL_MONTHS)
    return monthly[monthly["month"] < cutoff], monthly[monthly["month"] >= cutoff]


# ── Page ────────────────────────────────────────────────────────────────────

st.title("🅿️ Goal parking")
st.caption(
    "Upload a client's **purposes export from the CRM**. For every active goal, "
    "the goal grid's answer: what should already be parked in debt and hybrid, "
    "and every dated movement still ahead. "
    f"Engine `{ENGINE_SOURCE_SHA}`."
)

with st.expander("How to read this — and the assumptions behind every number"):
    _dp = _DEFAULT_INSTRUMENT_PARAMS
    st.markdown(
        "**These are lump movements, not a monthly SIP.** The grid moves money "
        "out of the core corpus in dated lumps: when a payment comes within reach, "
        "a share of it goes into hybrid that month; as the payment nears, further "
        "shares go into debt year by year; a non-negotiable goal's hybrid money "
        "switches into debt in its final year. That is exactly what the "
        "simulation executes, and it is what is shown here.\n\n"
        "**Reach — how far ahead parking starts:** "
        f"non-negotiable {GOAL_REACH_YEARS['non-negotiable']} years, "
        f"semi-negotiable {GOAL_REACH_YEARS['semi-negotiable']}, "
        f"negotiable {GOAL_REACH_YEARS['negotiable']}. A payment further out than "
        "that stays in the core corpus.\n\n"
        "**Already inside the window?** Then the movements that should have happened "
        "are shown as **due now** — the catch-up lump.\n\n"
        "**Amounts grow from the date they were struck.** Each goal's today's-rupees "
        "amount escalates at its growth % from the CRM's `amount_as_of` date, not "
        "from today — otherwise every goal would be under-provisioned by the time "
        "elapsed since it was entered. Goals flagged `payments_fixed_at_start` "
        "escalate only to their first payment, then stay flat (EMIs, admission-locked "
        "fees); income goals keep escalating.\n\n"
        f"**Returns:** debt {_dp['debt']['return'] * 100:g}%, hybrid "
        f"{_dp['hybrid']['return'] * 100:g}%. **Tax** on every movement and payout, "
        f"equity rates for all buckets (the debt sleeve holds arbitrage funds): "
        f"{_dp['debt']['stcg_tax'] * 100:g}% under a year, "
        f"{_dp['debt']['ltcg_tax'] * 100:g}% beyond, with a {LTCG_GRACE_DAYS}-day "
        "year-boundary grace. Each lump is sized so that what it **delivers at the "
        "payment, after tax**, is exactly its share of that payment."
    )

c1, c2 = st.columns([3, 1])
upload = c1.file_uploader("CRM purposes export (CSV or XLSX)", type=["csv", "xlsx", "xls"])
as_of_in = c2.date_input("As of", value=month_start_today().date())
as_of = pd.Timestamp(as_of_in.year, as_of_in.month, 1)

if upload is None:
    st.info("Upload the purposes file downloaded from the CRM for a client to begin.")
    st.stop()

try:
    df = read_purposes(upload)
except Exception as e:  # noqa: BLE001 - surface any parse problem to the CM
    st.error(str(e))
    st.stop()

family_col = "family_name" if "family_name" in df.columns else (
    "infinite_id" if "infinite_id" in df.columns else None)
families = [(k, g) for k, g in df.groupby(family_col, sort=False)] if family_col else [("Upload", df)]

for family, fdf in families:
    try:
        plans, totals = plan_purposes(fdf.to_dict("records"), as_of)
    except ValueError as e:
        st.error(f"{family}: {e}")
        continue

    st.header(str(family))
    if "infinite_id" in fdf.columns and family_col != "infinite_id":
        st.caption("Client id " + ", ".join(map(str, fdf["infinite_id"].dropna().unique())))

    if not plans:
        st.warning("No active goals in this file.")
        continue

    due_total = totals["debt_now"] + totals["hybrid_now"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Should already be in debt", short_inr(totals["debt_now"]))
    m2.metric("Should already be in hybrid", short_inr(totals["hybrid_now"]))
    m3.metric("Switch hybrid → debt now", short_inr(totals["switch_now"]))
    m4.metric("Next movement", mon(totals["next_month"]) if totals["next_month"] else "—")
    if due_total + totals["switch_now"] > 0.5:
        st.markdown(
            f"As of **{mon(as_of)}**, this family's goals require "
            f"**{short_inr(due_total)}** to already be parked"
            + (f" and **{short_inr(totals['switch_now'])}** switched from hybrid to debt"
               if totals["switch_now"] > 0.5 else "")
            + ". Per goal below."
        )
    else:
        st.markdown(f"As of **{mon(as_of)}**, nothing is due yet for this family's goals.")

    split = goal_split_frame(plans, as_of)
    st.dataframe(split, use_container_width=True, hide_index=True,
                 height=38 + 35 * len(split))
    st.caption(f"{len(plans)} active goal{'s' if len(plans) != 1 else ''} in this file — "
               "cancelled and deleted goals are excluded. Detail per goal below.")

    all_rows = []
    for p in plans:
        with st.container(border=True):
            st.subheader(p["name"])
            if p["status"] == STATUS_COMPLETED:
                st.markdown("All payments for this goal are in the past — nothing to park.")
                continue

            n = len(p["remaining"])
            st.caption(
                f"{n} payment{'s' if n != 1 else ''} still ahead, "
                f"{mon(p['remaining'][0][0])}"
                + (f" – {mon(p['remaining'][-1][0])}" if n > 1 else "")
                + f", totalling **{short_inr(p['remaining_total'])}** after growth."
            )

            nm = p["next_move"]
            if p["status"] == STATUS_NOT_STARTED:
                fm = p["first_move"]
                first = [r for _, r in p["monthly"].iterrows() if r["month"] == fm]
                bits = [f"{short_inr(r['amount'])} into {r['bucket']}" for r in first
                        if r["kind"] == "add"]
                st.markdown(
                    f"**Nothing to park yet.** First movement **{mon(fm)}** — "
                    + " and ".join(bits) + "."
                )
            else:
                d, h = p["due_now"]["debt"], p["due_now"]["hybrid"]
                held = []
                if d > 0.5:
                    held.append(f"**{short_inr(d)} in debt**")
                if h > 0.5:
                    held.append(f"**{short_inr(h)} in hybrid**")
                line = (f"**Parking is already due.** As of {mon(as_of)} this goal should hold "
                        + " and ".join(held) + "." if held else
                        f"**Parking is already due** as of {mon(as_of)}.")
                if p.get("switch_now", 0) > 0.5:
                    line += (f" **{short_inr(p['switch_now'])}** should switch from "
                             "hybrid to debt now.")
                if nm:
                    parts = []
                    if nm["debt"] > 0.5:
                        parts.append(f"{short_inr(nm['debt'])} into debt")
                    if nm["hybrid"] > 0.5:
                        parts.append(f"{short_inr(nm['hybrid'])} into hybrid")
                    if nm["switch"] > 0.5:
                        parts.append(f"{short_inr(nm['switch'])} hybrid → debt")
                    line += f" Next movement **{mon(nm['month'])}** — " + ", ".join(parts) + "."
                st.markdown(line)

            detail, later = split_schedule(p["monthly"], as_of)
            if not detail.empty:
                sched = schedule_frame(detail)
                st.dataframe(sched, use_container_width=True, hide_index=True,
                             height=min(420, 38 + 35 * len(sched)))
            if not later.empty:
                st.caption(f"Beyond the next {DETAIL_MONTHS} months, by year "
                           "(every month is in the CSV download):")
                yr = yearly_frame(later)
                st.dataframe(yr, use_container_width=True, hide_index=True,
                             height=min(420, 38 + 35 * len(yr)))
            for _, r in p["monthly"].iterrows():
                all_rows.append({
                    "family": family, "purpose_id": p["purpose_id"], "goal": p["name"],
                    "month": pd.Timestamp(r["month"]).strftime("%Y-%m-01"),
                    "movement": movement_label(r["kind"], r["bucket"]),
                    "amount": round(float(r["amount"]), 2),
                    "payments": int(r["payments"]),
                    "first_payment": pd.Timestamp(r["first_payment"]).strftime("%Y-%m-01"),
                    "last_payment": pd.Timestamp(r["last_payment"]).strftime("%Y-%m-01"),
                    "delivers_after_tax": round(float(r["delivers"]), 2),
                })

    if all_rows:
        st.download_button(
            "📄 Download this family's movement schedule (CSV)",
            data=pd.DataFrame(all_rows).to_csv(index=False).encode("utf-8"),
            file_name=f"goal_parking_{str(family).replace(' ', '_')}_{as_of:%Y-%m}.csv",
            mime="text/csv", key=f"dl_{family}",
        )
