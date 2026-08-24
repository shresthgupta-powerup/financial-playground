"""v2 goal provisioning + settlement: the grid replaces chains AND pools.

This module is the simulation half of the v2 goal structure (Punit's
"4. Goal Planning" / "Goal Algo" docs, 2026-08; decisions logged in
DECISIONS.md 2026-08-24). ``goal_grid.py`` is the pure doc-reference
calculator; THIS module is what ``run_simulation`` actually executes.

The model, in one paragraph: a goal is a series of cashflows. Each cashflow
is provisioned by the grid — rows are whole years until the cashflow
(t = days/365.25, load-bearing), columns are negotiability, each cell a
Debt/Hybrid share of the cashflow's future value. As the cashflow nears, the
grid's shares change; each share-change is a concrete, dated funding EVENT:
new money carved from Core into a sleeve, or (non-negotiable goals, final
year) the hybrid sleeve converting into debt. Sizing is PATH-CONSISTENT: each
slice of money is back-solved leg by leg along the route it will actually
travel, taxed at every hop, so the goal receives its amount NET of every tax
on the way. This deliberately diverges from the doc's each-sleeve-discounted-
in-place formula (operator decision, 2026-08-24 — the doc formula understates
by ~9.9% with tax on the worked example; ``goal_grid.invest_today`` keeps the
doc-literal behaviour as the golden reference against Punit's tables).

Settlement is a MONTHLY loop (all dates on the day=1 month grid):
  - income nets against that month's due cashflows first (untaxed, in goal
    priority order); only the balance is provisioned; surplus income → Core;
  - funding events draw from Core via net redemption (Core tax grossed up);
    a month where Core cannot fund an event is NOT a failure — the slice
    stays pending and retries every later month, re-sized from that month;
  - a due cashflow is paid from its own sleeves, then Core, then — in
    reverse priority order — every other goal's sleeves. FAILURE means a due
    cashflow is still short after ALL of that: every pool depleted
    (operator decision #3, 2026-08-24). There is no other failure mode.

Taxation ("+goaltaxequity"): every bucket is equity-taxed (the debt sleeve
holds arbitrage funds) — 20% STCG under a year, 12.5% LTCG beyond, with the
2-day year-boundary grace at every decision site.
"""

import math

import pandas as pd
from dateutil.relativedelta import relativedelta

from .goal_grid import (
    DAYS_PER_YEAR,
    GOAL_GRID,
    GOAL_REACH_YEARS,
    normalise_negotiability,
)

# Priority order for sequential funding (Goal Algo step 4).
_NEG_RANK = {"non-negotiable": 0, "semi-negotiable": 1, "negotiable": 2}

_PAY_TOLERANCE = 1.0  # rupees; float/rounding slack on payments


def _month_start(ts):
    ts = pd.Timestamp(ts)
    return ts.replace(day=1)


def _shares(negotiability, row):
    return GOAL_GRID[row][negotiability]


def row_of(cf_date, month):
    """Grid row for a cashflow seen from *month* (negative once it is due)."""
    t = (pd.Timestamp(cf_date) - pd.Timestamp(month)).days / DAYS_PER_YEAR
    return math.floor(t)


def row_entry_month(cf_date, row, floor_month):
    """First day-1 month at or after *floor_month* whose grid row == *row*.

    Analytic: the row starts when (cf_date - m).days < (row+1) * 365.25, i.e.
    the first month-start strictly later than cf_date - (row+1) years-of-days.
    Month gaps (28-31d) are far smaller than the 365/366-day row width, so the
    first qualifying month-start always lands inside the row — no scan needed.
    """
    cf_date = pd.Timestamp(cf_date)
    boundary = cf_date - pd.Timedelta(days=math.floor((row + 1) * DAYS_PER_YEAR))
    cand = _month_start(boundary)
    while (cf_date - cand).days >= (row + 1) * DAYS_PER_YEAR:
        cand = cand + relativedelta(months=1)
    return max(cand, _month_start(floor_month))


def grid_slice_plan(negotiability, cf_date, current_date):
    """The funding events for ONE cashflow, as slices with concrete hop routes.

    Returns a list of dicts, one per slice of money:
        {'share', 'hops': [(place, in_date, out_date), ...]}
    ``share`` is the fraction of the cashflow's (net) FV this slice delivers at
    ``cf_date``. Hops are the route the money actually travels: every hybrid
    slice of a non-negotiable goal converts to debt when the final-year row
    arrives; every other slice rides its bucket straight to the cashflow.

    A plan that starts mid-grid (the cashflow is already closer than the
    column's reach) gets a CATCH-UP first event: the cumulative shares of the
    current row all enter at the plan start.
    """
    neg = normalise_negotiability(negotiability)
    reach = GOAL_REACH_YEARS[neg]
    cf_date = pd.Timestamp(cf_date)
    t0 = _month_start(current_date)

    r_now = row_of(cf_date, t0)
    if r_now < 0:
        r_now = 0  # due immediately: treat as the final row, entered now
    start_row = min(reach - 1, r_now)

    # Conversion row: the highest row where the hybrid share DROPS entering it
    # (grid-wide this is only non-negotiable row 0, but detect generally).
    conv_row = None
    for r in range(start_row - 1, -1, -1):
        if _shares(neg, r)[1] < _shares(neg, r + 1)[1] - 1e-12:
            conv_row = r
            break
    conv_month = (row_entry_month(cf_date, conv_row, t0)
                  if conv_row is not None else None)

    slices = []
    prev_d, prev_h = 0.0, 0.0
    for r in range(start_row, -1, -1):
        d_r, h_r = _shares(neg, r)
        entry = row_entry_month(cf_date, r, t0)
        delta_d, delta_h = d_r - prev_d, h_r - prev_h

        if delta_h > 1e-12:
            if conv_row is not None and r > conv_row:
                hops = [("hybrid", entry, conv_month), ("debt", conv_month, cf_date)]
            else:
                hops = [("hybrid", entry, cf_date)]
            slices.append({"share": delta_h, "hops": hops})

        # New debt money: a conversion month's debt rise is fed by the
        # converting hybrid slices, not by fresh Core money.
        new_debt = delta_d + min(0.0, delta_h)
        if new_debt > 1e-12:
            slices.append({"share": new_debt, "hops": [("debt", entry, cf_date)]})

        prev_d, prev_h = d_r, h_r

    return slices


def slice_principal(target_net, hops, from_month, navs, instrument_params,
                    stcg_max_years):
    """Back-solve the principal needed at *from_month* to deliver *target_net*.

    Walks the remaining hops in reverse; per leg the growth is the actual NAV
    ratio (so settlement delivers exactly what sizing promised) and the gains
    are taxed STCG/LTCG by the leg's length with the year-boundary grace.
    """
    from_month = pd.Timestamp(from_month)
    need = float(target_net)
    for place, h_in, h_out in reversed(hops):
        h_in = max(pd.Timestamp(h_in), from_month)
        h_out = pd.Timestamp(h_out)
        if h_out <= h_in:
            continue
        params = instrument_params[place]
        g = navs[place][h_out] / navs[place][h_in]
        years = (h_out - h_in).days / DAYS_PER_YEAR
        rate = params["stcg_tax"] if years <= stcg_max_years else params["ltcg_tax"]
        need = need / (g * (1.0 - rate) + rate)
    return need



def net_tranches_against_income(tranches, investment_df):
    """Income funds due cashflows first — per tranche, in priority order.

    ``tranches``: list of dicts with 'date', 'fv', 'priority' (lower funds
    first). Mutates nothing; returns (net_amounts, surplus_df) where
    ``net_amounts[i]`` is tranche i's balance after income, and ``surplus_df``
    is the ``[Date, Investment]`` frame of income left for the Core Corpus.
    A cashflow covered by income is cash paying an expense — no tax, no pool.
    """
    if investment_df is None or investment_df.empty:
        by_month = {}
        inv_rows = []
    else:
        inv = investment_df.copy()
        inv["ym"] = inv["Date"].dt.to_period("M")
        by_month = inv.groupby("ym")["Investment"].sum().to_dict()
        inv_rows = list(inv[["Date", "ym", "Investment"]].itertuples(index=False))

    remaining = dict(by_month)
    net_amounts = [0.0] * len(tranches)
    order = sorted(range(len(tranches)),
                   key=lambda i: (tranches[i]["priority"], tranches[i]["date"]))
    for i in order:
        ym = pd.Timestamp(tranches[i]["date"]).to_period("M")
        avail = remaining.get(ym, 0.0)
        gross = float(tranches[i]["fv"])
        used = min(avail, gross)
        remaining[ym] = avail - used
        net_amounts[i] = gross - used

    if inv_rows:
        # A month's surplus is shared by its rows: cap each row at what is left
        # of that month's surplus and burn it down in row order.
        burn = dict(remaining)
        rows = []
        for r in inv_rows:
            left = max(0.0, burn.get(r.ym, 0.0))
            take = min(float(r.Investment), left)
            burn[r.ym] = left - take
            rows.append({"Date": r.Date, "Investment": take})
        surplus_df = pd.DataFrame(rows)
    else:
        surplus_df = pd.DataFrame({"Date": pd.Series(dtype="datetime64[ns]"),
                                   "Investment": pd.Series(dtype=float)})
    return net_amounts, surplus_df


def chain_table_rows(tranche_id, negotiability, cf_date, fv_net, slices,
                     principal_of, navs):
    """Render one tranche's slices in the v1 chain-table shape.

    The advisor workbook's action plan and the comprehensive view both read
    this shape (id / place / inflow_date / outflow_date / inflow_from /
    outflow_to / % of goal value / goal_value_post_tax / inflow_amount /
    total_outflow_amount / tax_out_of_outflow), so v2 keeps it: every slice
    IS a chain — Core → bucket [→ bucket] → goal.
    """
    rows = []
    for si, sl in enumerate(slices):
        prev_id = "core corpus"
        amounts = principal_of(sl)  # list of principals, one per hop
        for hi, (place, h_in, h_out) in enumerate(sl["hops"]):
            rid = f"{tranche_id}:s{si}h{hi}"
            principal = amounts[hi]
            g = navs[place][pd.Timestamp(h_out)] / navs[place][pd.Timestamp(h_in)]
            outflow = principal * g
            next_amt = amounts[hi + 1] if hi + 1 < len(amounts) else sl["share"] * fv_net
            rows.append({
                "id": rid, "place": place,
                "inflow_date": pd.Timestamp(h_in), "outflow_date": pd.Timestamp(h_out),
                "inflow_from": prev_id,
                "outflow_to": (f"{tranche_id}:s{si}h{hi + 1}"
                               if hi + 1 < len(sl["hops"]) else "goal"),
                "% of goal value": sl["share"] * 100.0,
                "goal_value_post_tax": fv_net,
                "inflow_amount": round(principal, 2),
                "total_outflow_amount": round(outflow, 2),
                "tax_out_of_outflow": round(max(0.0, outflow - next_amt), 2),
            })
            prev_id = rid
        rows.append({
            "id": f"{tranche_id}:s{si}goal", "place": "goal",
            "inflow_date": pd.Timestamp(cf_date), "outflow_date": pd.NaT,
            "inflow_from": prev_id, "outflow_to": None,
            "% of goal value": sl["share"] * 100.0,
            "goal_value_post_tax": fv_net,
            "inflow_amount": round(sl["share"] * fv_net, 2),
            "total_outflow_amount": pd.NA, "tax_out_of_outflow": pd.NA,
        })
    return rows
