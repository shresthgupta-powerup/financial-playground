"""v2 goal provisioning: the glide-path GRID (Punit's "Goal Algo", 2026-08).

The v1 model routed each goal through a tranche-and-chain script and funded
Replenishing goals from shared Debt/Hybrid pools. v2 replaces both with ONE
idea: a goal is a series of cashflows, and every cashflow is looked up in a
single grid that says what share of it should be sitting in Debt and Hybrid
right now.

This module is the pure calculator for that idea. It is deliberately free of
simulation state so it can be validated on its own against the worked examples
in the source document, and it is layered:

  1. ``carve_shares``       - the grid lookup             (no money)
  2. ``carve_at_fv``        - the share applied to FV     (money, PRE-tax)
  3. ``invest_today``       - discounted back to today    (matches the doc)
  4. ``invest_today_taxed`` - grossed up for redemption tax

Layers 1-3 reproduce the document's worked tables exactly, which is what makes
them usable as golden tests. Layer 4 follows the engine's existing convention
(see ``engine.calculate_required_inflow``): today's engine taxes BOTH the sizing
and the actual movement, and v2 keeps that.

NOTE: this module is the calculator only. Wiring it into ``run_simulation`` (and
retiring the chains + pools) is a separate, larger change.
"""

import math

from dateutil.relativedelta import relativedelta
import pandas as pd

# Rows are floor(years to cashflow); values are (debt_share, hybrid_share).
GOAL_GRID = {
    0: {"non-negotiable": (1.00, 0.00), "semi-negotiable": (0.75, 0.25), "negotiable": (0.50, 0.50)},
    1: {"non-negotiable": (0.75, 0.25), "semi-negotiable": (0.50, 0.25), "negotiable": (0.30, 0.40)},
    2: {"non-negotiable": (0.50, 0.25), "semi-negotiable": (0.25, 0.25), "negotiable": (0.00, 0.30)},
    3: {"non-negotiable": (0.25, 0.25), "semi-negotiable": (0.00, 0.25), "negotiable": (0.00, 0.00)},
    4: {"non-negotiable": (0.00, 0.25), "semi-negotiable": (0.00, 0.00), "negotiable": (0.00, 0.00)},
}

# Each column's own reach in whole years. A cashflow at or beyond its column's
# reach carves nothing and marks the goal as replenishing.
GOAL_REACH_YEARS = {"non-negotiable": 5, "semi-negotiable": 4, "negotiable": 3}

# The engine's existing goal `type` values map onto the grid's columns.
NEGOTIABILITY_ALIASES = {
    "non-negotiable": "non-negotiable", "non negotiable": "non-negotiable",
    "semi-negotiable": "semi-negotiable", "semi negotiable": "semi-negotiable",
    "negotiable": "negotiable",
}

FREQUENCY_MONTHS = {"monthly": 1, "quarterly": 3, "half-yearly": 6, "annual": 12}

# Firm growth assumptions for the goal sleeves (tunable settings).
DEFAULT_DEBT_GROWTH = 0.06
DEFAULT_HYBRID_GROWTH = 0.09
DEFAULT_GOAL_INFLATION = 0.06

DAYS_PER_YEAR = 365.25  # load-bearing: decides which grid row a cashflow lands in

# Taxation (mirrors engine._DEFAULT_INSTRUMENT_PARAMS, "+goaltaxequity"):
# ALL goal money is EQUITY-taxed - the debt sleeve holds ARBITRAGE funds
# (debt-like return, equity taxation) and the hybrid funds offered are
# equity-taxed. Every redemption: 20% STCG (< 1 year) / 12.5% LTCG.
# Boundary rule: a redemption within LTCG_GRACE_DAYS of completing one year
# counts as LTCG - the desk shifts it 1-2 days to cross the year. This covers
# the negotiable-goal wrinkle where money enters hybrid ~1 year before the
# goal: held ~365 days -> 12.5%, never 20%.
EQUITY_STCG_TAX = 0.20
EQUITY_LTCG_TAX = 0.125
LTCG_GRACE_DAYS = 2
STCG_MAX_YEARS = (365 - LTCG_GRACE_DAYS) / DAYS_PER_YEAR
DEFAULT_DEBT_TAX = (EQUITY_STCG_TAX, EQUITY_LTCG_TAX)
DEFAULT_HYBRID_TAX = (EQUITY_STCG_TAX, EQUITY_LTCG_TAX)

GOAL_REPLENISH = "GOAL_REPLENISH"
GOAL_NON_REPLENISH = "GOAL_NON_REPLENISH"


def normalise_negotiability(value):
    key = str(value or "").strip().lower()
    try:
        return NEGOTIABILITY_ALIASES[key]
    except KeyError:
        raise ValueError("unknown negotiability: %r" % (value,))


def years_until(cashflow_date, plan_date):
    """Years from plan_date to cashflow_date on the 365.25-day convention."""
    delta_days = (pd.Timestamp(cashflow_date) - pd.Timestamp(plan_date)).days
    return delta_days / DAYS_PER_YEAR


def is_beyond_window(t_years, negotiability):
    """True when the cashflow sits at or past its column's reach (not carved)."""
    neg = normalise_negotiability(negotiability)
    return math.floor(t_years) >= GOAL_REACH_YEARS[neg]


def carve_shares(t_years, negotiability):
    """Layer 1 - the grid lookup. Returns (debt_share, hybrid_share)."""
    neg = normalise_negotiability(negotiability)
    if t_years < 0 or is_beyond_window(t_years, neg):
        return (0.0, 0.0)
    return GOAL_GRID[math.floor(t_years)][neg]


def future_value(amount, inflation, t_years):
    """The cashflow escalated to its OWN date at the goal's own inflation."""
    return amount * (1 + inflation) ** t_years


def carve_at_fv(amount, t_years, negotiability, inflation):
    """Layer 2 - the share of the escalated cashflow, PRE-tax, at its date."""
    if t_years < 0:
        return (0.0, 0.0)
    debt_share, hybrid_share = carve_shares(t_years, negotiability)
    fv = future_value(amount, inflation, t_years)
    return (fv * debt_share, fv * hybrid_share)


def invest_today(amount, t_years, negotiability, inflation,
                 debt_growth=DEFAULT_DEBT_GROWTH,
                 hybrid_growth=DEFAULT_HYBRID_GROWTH):
    """Layer 3 - discounted back to today. Reproduces the document's tables."""
    if t_years < 0:
        return (0.0, 0.0)
    carve_debt, carve_hybrid = carve_at_fv(amount, t_years, negotiability, inflation)
    return (carve_debt / (1 + debt_growth) ** t_years,
            carve_hybrid / (1 + hybrid_growth) ** t_years)


def gross_up_for_tax(target_post_tax, annual_return, t_years, stcg_tax, ltcg_tax):
    """Layer 4 helper - principal needed so the sleeve DELIVERS target net of tax.

    Identical convention to ``engine.calculate_required_inflow``: gains are taxed
    at STCG under a year, LTCG beyond - with the boundary grace (see
    ``STCG_MAX_YEARS``): within a couple of days of a full year counts as LTCG.
    """
    if t_years <= 0:
        return target_post_tax
    tax_rate = stcg_tax if t_years <= STCG_MAX_YEARS else ltcg_tax
    growth_factor = (1 + annual_return) ** t_years
    return target_post_tax / (growth_factor * (1 - tax_rate) + tax_rate)


def invest_today_taxed(amount, t_years, negotiability, inflation,
                       debt_growth=DEFAULT_DEBT_GROWTH,
                       hybrid_growth=DEFAULT_HYBRID_GROWTH,
                       debt_tax=DEFAULT_DEBT_TAX, hybrid_tax=DEFAULT_HYBRID_TAX):
    """Layer 4 - what must be carved TODAY so each sleeve delivers its share net."""
    if t_years < 0:
        return (0.0, 0.0)
    carve_debt, carve_hybrid = carve_at_fv(amount, t_years, negotiability, inflation)
    return (gross_up_for_tax(carve_debt, debt_growth, t_years, *debt_tax),
            gross_up_for_tax(carve_hybrid, hybrid_growth, t_years, *hybrid_tax))


def expand_goal_to_cashflows(goal, plan_date=None):
    """Step 1 - a goal becomes its series of (date, amount) cashflows.

    one-time -> a single cashflow; recurring -> ``occurrences`` cashflows stepped
    by the frequency's month step. ``relativedelta`` clamps month-ends (a goal on
    the 31st steps to the 30th, the 28th, and back).
    """
    structure = str(goal.get("structure", "")).strip().lower()
    amount = float(goal.get("amount", 0) or 0)
    start = pd.Timestamp(goal.get("start_date"))
    if structure in ("one-time", "onetime", "lumpsum"):
        return [(start, amount)]
    step = FREQUENCY_MONTHS.get(str(goal.get("frequency", "")).strip().lower())
    if step is None:
        raise ValueError("recurring goal needs a valid frequency: %r"
                         % (goal.get("frequency"),))
    occurrences = int(goal.get("occurrences") or 1)
    return [(start + relativedelta(months=step * k), amount) for k in range(occurrences)]


def goal_sleeves(goal, plan_date, debt_growth=DEFAULT_DEBT_GROWTH,
                 hybrid_growth=DEFAULT_HYBRID_GROWTH, apply_tax=True,
                 debt_tax=DEFAULT_DEBT_TAX, hybrid_tax=DEFAULT_HYBRID_TAX):
    """Steps 1-3 for one goal: sleeves today, plus the derived purpose.

    Returns ``{debt, hybrid, purpose, inside, beyond, cashflows}``. ``purpose``
    is DERIVED, never typed: any cashflow past the column's reach means the goal
    outlives this plan.
    """
    neg = normalise_negotiability(goal.get("negotiability", goal.get("type")))
    inflation = goal.get("inflation")
    if inflation is None:
        pct = goal.get("inflation_percent")
        inflation = DEFAULT_GOAL_INFLATION if pct is None else float(pct) / 100.0
    plan_date = pd.Timestamp(plan_date)

    debt_total = 0.0
    hybrid_total = 0.0
    inside = 0
    beyond = 0
    for cf_date, amount in expand_goal_to_cashflows(goal, plan_date):
        t = years_until(cf_date, plan_date)
        if t < 0:
            continue                      # already happened; not ours to fund
        if is_beyond_window(t, neg):
            beyond += 1
            continue
        inside += 1
        if apply_tax:
            d, h = invest_today_taxed(amount, t, neg, inflation, debt_growth,
                                      hybrid_growth, debt_tax, hybrid_tax)
        else:
            d, h = invest_today(amount, t, neg, inflation, debt_growth, hybrid_growth)
        debt_total += d
        hybrid_total += h

    return {
        "debt": debt_total,
        "hybrid": hybrid_total,
        "purpose": GOAL_REPLENISH if beyond else GOAL_NON_REPLENISH,
        "inside": inside,
        "beyond": beyond,
        "cashflows": inside + beyond,
    }


def order_goals(goals, plan_date):
    """Step 4 - funding order: negotiability, then earliest cashflow, then larger."""
    rank = {"non-negotiable": 0, "semi-negotiable": 1, "negotiable": 2}
    plan_date = pd.Timestamp(plan_date)

    def key(goal):
        neg = normalise_negotiability(goal.get("negotiability", goal.get("type")))
        cashflows = expand_goal_to_cashflows(goal, plan_date)
        first = min((d for d, _a in cashflows), default=plan_date)
        total = sum(a for _d, a in cashflows)
        return (rank[neg], pd.Timestamp(first), -total)

    return sorted(goals, key=key)
