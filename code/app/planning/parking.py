"""Goal parking: what the grid says must be moved, and when, for one goal.

The advisory head's question, per CRM goal, as of a date: is anything due
yet? If not, when is the first movement? If so, how much should ALREADY be
sitting in debt and hybrid, and what is the next move?

This reports the MODEL'S TRUTH: dated lump movements out of the core corpus,
exactly the funding events the simulator executes - a slice enters hybrid the
month its payment comes within reach, later slices enter debt year by year,
and a non-negotiable goal's hybrid converts to debt in its final year. It is
not a monthly-SIP translation (operator decision, 2026-09-04).

Pure and standalone. A contract-v2 purpose row carries a resolved start date
and a true occurrence count, so no plan context (solver, corpus, other goals)
is needed. Two conventions, both deliberate:

  - Amounts escalate from ``amount_as_of`` - the date the today's-rupees
    figure was struck - not from the as-of date. Escalating from the wrong
    date under-provisions every goal by the time elapsed since entry.
  - A payment already inside its carve window at the as-of date has its
    earlier slices clamped to the as-of month (``grid_slice_plan`` does this),
    so they surface as "due now" - the catch-up lump.
"""

import math

import pandas as pd

from .engine import (
    _DEFAULT_INSTRUMENT_PARAMS,
    _STCG_MAX_YEARS,
    _nav_value,
    expand_recurring_goal_to_tranches,
)
from .grid_engine import grid_slice_plan, slice_principal

_NEGOTIABILITY = {
    "non_negotiable": "Non-Negotiable",
    "semi_negotiable": "Semi-Negotiable",
    "negotiable": "Negotiable",
}
_FREQUENCY = {
    "monthly": "Monthly", "quarterly": "Quarterly",
    "half_yearly": "Half-Yearly", "yearly": "Annual",
}

STATUS_NOT_STARTED = "not_started"
STATUS_UNDERWAY = "underway"
STATUS_COMPLETED = "completed"


def _month(ts):
    return pd.Timestamp(ts).replace(day=1)


def _isna(v):
    try:
        return v is None or (isinstance(v, float) and math.isnan(v)) or pd.isna(v)
    except (TypeError, ValueError):
        return False


def _truthy(v):
    if _isna(v):
        return False
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "t", "yes", "y")
    return bool(v)


def goal_from_purpose_row(row):
    """One CRM purposes-export row (contract v2) -> the engine's goal shape.

    Keys are the export's own (``goal_name``, ``amount_per_occurrence``, ...),
    case-insensitive. Raises ``ValueError`` on a token the planner cannot
    represent rather than defaulting it.
    """
    r = {str(k).strip().lower(): v for k, v in dict(row).items()}
    neg = str(r.get("goal_negotiability") or "").strip().lower()
    if neg not in _NEGOTIABILITY:
        raise ValueError(f"goal {r.get('goal_name')!r}: negotiability {neg!r} unknown")
    occ = int(float(r.get("occurrences") or 1))
    recurring = occ > 1
    freq_tok = str(r.get("frequency") or "").strip().lower()
    if recurring and freq_tok not in _FREQUENCY:
        raise ValueError(f"goal {r.get('goal_name')!r}: frequency {freq_tok!r} "
                         "has no equivalent in the planner")
    inflation = float(r.get("inflation") or 0.0)
    amount_as_of = r.get("amount_as_of")
    if _isna(amount_as_of):
        amount_as_of = r.get("goal_created_at")
    return {
        "purpose_id": r.get("purpose_id"),
        "name": str(r.get("goal_name") or "").strip() or "Goal",
        "goal_category": r.get("goal_type"),
        "description": str(r.get("goal_description") or ""),
        "type": _NEGOTIABILITY[neg],
        "structure": "Recurring" if recurring else "Lumpsum",
        "start_date": _month(r["start_date"]),
        "amount": float(r.get("amount_per_occurrence") or 0),
        "frequency": _FREQUENCY.get(freq_tok) if recurring else None,
        "occurrences": occ,
        "end_mode": "Occurrences" if recurring else None,
        "lifetime": _truthy(r.get("lifetime")) if recurring else False,
        "payments_fixed_at_start": _truthy(r.get("payments_fixed_at_start")) if recurring else False,
        "inflation_percent": inflation * 100.0,
        "amount_as_of": None if _isna(amount_as_of) else _month(amount_as_of),
        "goal_status": str(r.get("goal_status") or "active").strip().lower(),
    }


def parking_plan(goal, as_of, params=_DEFAULT_INSTRUMENT_PARAMS):
    """The dated movements the grid requires for one goal, seen from *as_of*.

    Returns a dict:
      status       not_started | underway | completed
      first_move   first event month (None when completed)
      due_now      {'debt': x, 'hybrid': y} - lumps that should already be
                   parked as of the as-of month (the catch-up)
      next_move    {'month', 'debt', 'hybrid', 'switch'} for the first event
                   month AFTER as_of, or None
      events       one row per slice event: month, kind ('add'|'switch'),
                   bucket, amount, cf_date, share, delivers
      monthly      DataFrame aggregated by month x kind x bucket
      remaining    [(cf_date, fv)] still to be paid; remaining_total
    """
    as_of = _month(as_of)
    struck = goal.get("amount_as_of") or as_of
    tranches = expand_recurring_goal_to_tranches(goal, struck)
    remaining = [(pd.Timestamp(d), float(fv)) for d, fv in tranches
                 if pd.Timestamp(d) >= as_of and fv > 0]
    base = {"purpose_id": goal.get("purpose_id"), "name": goal.get("name"),
            "remaining": remaining,
            "remaining_total": sum(fv for _d, fv in remaining)}
    if not remaining:
        return {**base, "status": STATUS_COMPLETED, "first_move": None,
                "due_now": {"debt": 0.0, "hybrid": 0.0}, "next_move": None,
                "events": [], "monthly": pd.DataFrame()}

    last = max(d for d, _ in remaining)
    months = list(pd.date_range(as_of, last + pd.DateOffset(months=1), freq="MS"))
    rates = {"core": params["core_corpus"]["return"],
             "debt": params["debt"]["return"], "hybrid": params["hybrid"]["return"]}
    navs = {p: {m: _nav_value(rates[p], as_of, m) for m in months}
            for p in ("core", "debt", "hybrid")}

    events = []
    for cf_date, fv in remaining:
        for sl in grid_slice_plan(goal["type"], cf_date, as_of):
            hops = sl["hops"]
            target = sl["share"] * fv
            entry = pd.Timestamp(hops[0][1])
            events.append({
                "month": entry, "kind": "add", "bucket": hops[0][0],
                "amount": slice_principal(target, hops, entry, navs, params, _STCG_MAX_YEARS),
                "cf_date": cf_date, "share": sl["share"], "delivers": target,
            })
            if len(hops) > 1:
                conv = pd.Timestamp(hops[1][1])
                events.append({
                    "month": conv, "kind": "switch", "bucket": hops[1][0],
                    "amount": slice_principal(target, hops, conv, navs, params, _STCG_MAX_YEARS),
                    "cf_date": cf_date, "share": sl["share"], "delivers": target,
                })
    events.sort(key=lambda e: (e["month"], e["kind"] != "switch", e["bucket"]))

    first_move = min(e["month"] for e in events)
    due_now = {"debt": 0.0, "hybrid": 0.0}
    for e in events:
        if e["month"] == as_of and e["kind"] == "add":
            due_now[e["bucket"]] += e["amount"]
    switch_now = sum(e["amount"] for e in events
                     if e["month"] == as_of and e["kind"] == "switch")
    status = (STATUS_UNDERWAY if (due_now["debt"] + due_now["hybrid"] + switch_now) > 0.5
              else STATUS_NOT_STARTED)

    later = [e for e in events if e["month"] > as_of]
    next_move = None
    if later:
        m = min(e["month"] for e in later)
        nm = {"month": m, "debt": 0.0, "hybrid": 0.0, "switch": 0.0}
        for e in later:
            if e["month"] != m:
                continue
            if e["kind"] == "switch":
                nm["switch"] += e["amount"]
            else:
                nm[e["bucket"]] += e["amount"]
        next_move = nm

    ev = pd.DataFrame(events)
    monthly = (ev.groupby(["month", "kind", "bucket"], as_index=False)
                 .agg(amount=("amount", "sum"), delivers=("delivers", "sum"),
                      payments=("cf_date", "nunique"),
                      first_payment=("cf_date", "min"), last_payment=("cf_date", "max"))
                 .sort_values(["month", "kind"], ascending=[True, False])
                 .reset_index(drop=True))

    return {**base, "status": status, "first_move": first_move, "due_now": due_now,
            "switch_now": switch_now, "next_move": next_move, "events": events,
            "monthly": monthly}


def plan_purposes(rows, as_of, params=_DEFAULT_INSTRUMENT_PARAMS):
    """Plans for every ACTIVE goal row, plus family-level totals.

    Returns (plans, totals). ``totals`` sums the due-now lumps across goals
    and carries the earliest next movement.
    """
    plans = []
    for row in rows:
        r = {str(k).strip().lower(): v for k, v in dict(row).items()}
        if str(r.get("purpose_type") or "goal").lower() != "goal":
            continue
        if str(r.get("goal_status") or "active").lower() != "active":
            continue
        if _truthy(r.get("is_deleted")):
            continue
        plans.append(parking_plan(goal_from_purpose_row(r), as_of, params))

    totals = {"debt_now": sum(p["due_now"]["debt"] for p in plans),
              "hybrid_now": sum(p["due_now"]["hybrid"] for p in plans),
              "switch_now": sum(p.get("switch_now", 0.0) for p in plans),
              "next_month": None}
    upcoming = [p["next_move"]["month"] for p in plans if p.get("next_move")]
    if upcoming:
        totals["next_month"] = min(upcoming)
    return plans, totals
