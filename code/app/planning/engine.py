"""Financial-planning simulation engine — faithful port of the v3 ``main_v2.py``.

Ported near-verbatim (D-P202-1 / D-P202-2) from
``C:\\Punit Patel\\Financial Planning v3\\main_v2.py`` at commit ``1515f1e``
(branch ``feature/income-model-rework``). Pure logic — solver, ``run_simulation``,
``InvestmentPool`` / ``TaxLot`` FIFO tax-lot accounting, ``simulate_pool``
Debt/Hybrid pool. UI-/framework-agnostic; consumes a plain dict config.

Phase 1 (this file at port time) is a faithful copy: ONLY the xlsx-reading
``get_default_glide_paths()`` is replaced by ``glide_paths.get_glide_paths()``
and the ``main()`` smoke harness is dropped. The 3 audited defects (crash class,
perf cliff, validation) are hardened in Phase 2 — see ``validation.py`` and the
``get_withdrawl_df`` / ``add_withdrawls_to_trans`` guards below.

See ``.context/modules/financial-planning.md`` and the v3
``.context/SIMULATION_MODEL.md`` + ``DECISIONS.md`` for the model rationale.
"""

import pandas as pd
import numpy as np
from dateutil.relativedelta import relativedelta

from .glide_paths import get_glide_paths
from .validation import validate_plan_config
from .goal_grid import normalise_negotiability
from .grid_engine import (
    _NEG_RANK,
    _PAY_TOLERANCE,
    chain_table_rows,
    grid_slice_plan,
    net_tranches_against_income,
    slice_principal,
)

# Engine version stamped on saved plans (reproducibility, D-P206-3/6).
# "1515f1e" = the v3 source the engine was ported from; the "+pool2x2" suffix
# marks the deliberate post-port divergence (Hybrid pool window 2+3 -> 2+2,
# operator decision 2026-06-09); "+lifetimefix" marks the Plan 222 pool
# death-date provisioning fix; "+monthgrid" marks the Plan 223 boundary
# coercion (all input dates snapped to day=1, step-up anchor -> current_date).
# Bump this whenever the engine's numeric behaviour changes, so two saved plans
# with the same stamp always reproduce.
# v2 (2026-08-24): the goal GRID replaces glide-path chains and shared pools.
# v1 lineage "1515f1e+pool2x2+lifetimefix+monthgrid+poolprefund+goaldedupe
# +goaltaxequity" is retired; git history holds it. DECISIONS.md 2026-08-24.
ENGINE_SOURCE_SHA = "v2grid+goaltaxequity+fixedstart"
# Bump alongside ENGINE_SOURCE_SHA - shown in the app header as "updated ...".
ENGINE_UPDATED = "2026-08-25"

# Taxation of goal money ("+goaltaxequity", 2026-08-24, per advisory desk):
# ALL goal buckets are equity-taxed - the "debt" bucket holds ARBITRAGE funds
# (debt-like ~6% return, equity taxation), and the hybrid funds offered are
# equity-taxed. So every redemption of goal money is 20% STCG / 12.5% LTCG.
#
# Boundary rule: a redemption within LTCG_GRACE_DAYS of completing one year is
# treated as LTCG - operationally the desk shifts the redemption by 1-2 days to
# cross the year, so a lot held >= 364 days taxes at 12.5%, never 20%. This
# also removes a latent inconsistency where a 1-year glide hop was STCG in a
# 365-day year but LTCG in a 366-day one.
LTCG_GRACE_DAYS = 2
_STCG_MAX_DAYS = 365 - LTCG_GRACE_DAYS          # held <= 363 days -> STCG
_STCG_MAX_YEARS = _STCG_MAX_DAYS / 365.25       # same rule for fractional-year legs


# ---------------------------------------------------------------------------
# All Date columns / Timestamps must use a single resolution to avoid
# pandas merge_asof dtype-mismatch errors across versions.  We standardise
# on nanosecond resolution (datetime64[ns]) everywhere.
# ---------------------------------------------------------------------------
_NS_DTYPE = "datetime64[ns]"
# pandas datetime64[ns] tops out at 2262-04-11. Keep a buffer so date math doesn't trip it.
_MAX_SAFE_DATE = pd.Timestamp("2260-01-01")


def _ensure_date_ns(df):
    """Cast the 'Date' column of *df* to datetime64[ns] **in-place** and return df."""
    if "Date" in df.columns:
        df["Date"] = df["Date"].astype(_NS_DTYPE)
    return df


def _ts(val):
    """Return a pd.Timestamp guaranteed to be nanosecond resolution."""
    return pd.Timestamp(val).as_unit("ns")


class TaxLot:
    def __init__(self, date, units, purchase_price_per_unit):
        self.date = pd.Timestamp(date)
        self.units = float(units)
        self.purchase_price = float(purchase_price_per_unit)
        self.purchase_val = self.units * self.purchase_price

    def current_value(self, current_nav):
        return self.units * current_nav


class InvestmentPool:
    def __init__(self, name, stcg_tax, ltcg_tax):
        self.name = name
        self.stcg_tax = stcg_tax
        self.ltcg_tax = ltcg_tax
        self.lots = []  # List of TaxLot objects

    def _get_tax_rate(self, lot_date, redemption_date):
        holding_days = (pd.Timestamp(redemption_date) - pd.Timestamp(lot_date)).days
        # Boundary rule (+goaltaxequity): within LTCG_GRACE_DAYS of a full year
        # counts as LTCG - the desk shifts the redemption to complete the year.
        return self.stcg_tax if holding_days <= _STCG_MAX_DAYS else self.ltcg_tax

    def invest(self, date, amount, nav, description="Investment"):
        if amount <= 0:
            return None
        units = amount / nav
        new_lot = TaxLot(date, units, nav)
        self.lots.append(new_lot)
        return {
            'Date': date, 'Amount': amount, 'NAV': nav, 'units': units,
            'Description': description, 'tax': 0, 'fully_funded': True, 'shortfall': 0, 'source': 'Investment',
            'Pool': self.name
        }

    def get_market_value(self, nav):
        return sum(lot.units for lot in self.lots) * nav

    def get_unrealized_tax(self, nav, as_of_date=None):
        total_tax = 0
        for lot in self.lots:
            gain_per_unit = nav - lot.purchase_price
            if gain_per_unit > 0:
                rate = self._get_tax_rate(lot.date, as_of_date) if as_of_date is not None else self.ltcg_tax
                total_tax += gain_per_unit * lot.units * rate
        return total_tax

    def redeem_net_amount(self, date, target_net, nav, description="Withdrawal"):
        # We need to withdraw enough units such that (Value - Tax) = target_net
        # Since tax depends on which lots are sold (FIFO), this is iterative or requires handling lot by lot.

        needed_net = target_net
        total_gross_withdrawn = 0
        total_tax = 0
        total_units = 0

        lots_to_remove = []
        lots_updated = {}  # index -> new_units

        trans_details = []

        # Iterate through lots FIFO
        for i, lot in enumerate(self.lots):
            if needed_net <= 1e-4:
                break

            # Max we can get from this lot
            curr_val = lot.current_value(nav)
            gain_per_unit = nav - lot.purchase_price
            tax_per_unit = max(0, gain_per_unit * self._get_tax_rate(lot.date, date))
            net_per_unit = nav - tax_per_unit

            # Check if this lot covers the remainder
            max_net_from_lot = lot.units * net_per_unit

            if max_net_from_lot <= needed_net:
                # Consume entire lot
                units_to_sell = lot.units
                gross_amt = curr_val
                tax_amt = units_to_sell * tax_per_unit

                needed_net -= (gross_amt - tax_amt)
                total_gross_withdrawn += gross_amt
                total_tax += tax_amt
                total_units += units_to_sell
                lots_to_remove.append(i)

            else:
                # Partial lot
                units_to_sell = needed_net / net_per_unit
                gross_amt = units_to_sell * nav
                tax_amt = units_to_sell * tax_per_unit

                needed_net = 0
                total_gross_withdrawn += gross_amt
                total_tax += tax_amt
                total_units += units_to_sell

                # Update lot remaining units
                lots_updated[i] = lot.units - units_to_sell

        # Apply updates
        # Process updates first
        for i, new_units in lots_updated.items():
            self.lots[i].units = new_units

        # Process removals (reverse order to keep indices valid)
        for i in sorted(lots_to_remove, reverse=True):
            self.lots.pop(i)

        fully_funded = (needed_net <= 1.0)  # Floating point tolerance

        return {
            'Date': date, 'Amount': -total_gross_withdrawn, 'NAV': nav,
            'units': -total_units, 'Description': description,
            'tax': total_tax, 'fully_funded': fully_funded,
            'shortfall': needed_net,
            'net_received': total_gross_withdrawn - total_tax,
            'Pool': self.name
        }

    def redeem_gross_amount(self, date, target_gross, nav, description="Withdrawal Gross"):
        # Simpler: just sell units to meet target gross
        needed_gross = target_gross
        total_gross_withdrawn = 0
        total_tax = 0
        total_units = 0

        lots_to_remove = []
        lots_updated = {}

        for i, lot in enumerate(self.lots):
            if needed_gross <= 1e-4:
                break

            curr_val = lot.current_value(nav)

            if curr_val <= needed_gross:
                # Consume entire lot
                units_to_sell = lot.units
                gross_amt = curr_val
                gain = gross_amt - lot.purchase_val
                tax = max(0, gain * self._get_tax_rate(lot.date, date))

                needed_gross -= gross_amt
                total_gross_withdrawn += gross_amt
                total_tax += tax
                total_units += units_to_sell
                lots_to_remove.append(i)

            else:
                # Partial lot
                fraction = needed_gross / curr_val
                units_to_sell = lot.units * fraction
                gross_amt = needed_gross

                purchase_cost_for_part = lot.purchase_val * fraction
                gain = gross_amt - purchase_cost_for_part
                tax = max(0, gain * self._get_tax_rate(lot.date, date))

                needed_gross = 0
                total_gross_withdrawn += gross_amt
                total_tax += tax
                total_units += units_to_sell

                lots_updated[i] = lot.units - units_to_sell

        for i, new_units in lots_updated.items():
            self.lots[i].units = new_units
        for i in sorted(lots_to_remove, reverse=True):
            self.lots.pop(i)

        fully_funded = (needed_gross <= 1.0)

        return {
            'Date': date, 'Amount': -total_gross_withdrawn, 'NAV': nav,
            'units': -total_units, 'Description': description,
            'tax': total_tax, 'fully_funded': fully_funded,
            'shortfall': needed_gross,
            'net_received': total_gross_withdrawn - total_tax,
            'Pool': self.name
        }


def calculate_corpus_required_for_future_expense(expense_amount, years_to_expense, rate_of_return, tax_rate):
    # Formula: P = E / [ (1+r)^t(1-tax) + tax ]
    # Where E is expense, r is rate, t is time in years

    growth_factor = (1 + rate_of_return) ** years_to_expense
    denominator = growth_factor * (1 - tax_rate) + tax_rate
    required_corpus = expense_amount / denominator
    return required_corpus


# --- Helper Functions from main.py ---

def format_inr(amount):
    amount = round(float(amount), 2)
    integer, decimal = f"{amount:.2f}".split(".")

    if len(integer) > 3:
        last3 = integer[-3:]
        rest = integer[:-3]
        rest = ",".join([rest[max(i - 2, 0):i] for i in range(len(rest), 0, -2)][::-1])
        integer = rest + "," + last3

    return f"₹{integer}.{decimal}"


def future_value(present_value, inflation_rate, current_date, future_date):
    # Time difference in years (actual days / 365.25)
    years = (future_date - current_date).days / 365.25
    # Future value calculation
    fv = present_value * ((1 + inflation_rate) ** years)
    return round(fv, 2)


_FREQ_TO_MONTHS = {'Annual': 12, 'Quarterly': 3, 'Half-Yearly': 6, 'Monthly': 1}


def count_stepup_events(start_date, end_date, anchor_date, frequency):
    """Count step-up events strictly after start_date and at or before end_date.

    Events occur on calendar anniversaries of anchor_date at the given frequency.
    """
    step_months = _FREQ_TO_MONTHS.get(frequency)
    if step_months is None or anchor_date is None:
        return 0
    start_date = pd.Timestamp(start_date)
    end_date = pd.Timestamp(end_date)
    if end_date <= start_date:
        return 0

    cur = pd.Timestamp(anchor_date)
    # Roll forward in big chunks until we are past start_date.
    while cur + relativedelta(months=step_months) <= start_date:
        cur = cur + relativedelta(months=step_months)
    # Now cur <= start_date < cur + step. Advance once and start counting.
    cur = cur + relativedelta(months=step_months)
    count = 0
    while cur <= end_date:
        if cur > start_date:
            count += 1
        cur = cur + relativedelta(months=step_months)
    return count


def amount_at_date_with_stepup(pv_amount, growth_percent, growth_frequency, growth_anchor, current_date, target_date):
    """Inflated amount on *target_date* given PV at *current_date* and discrete step-up events."""
    target_date = pd.Timestamp(target_date)
    current_date = pd.Timestamp(current_date)
    if target_date <= current_date:
        return float(pv_amount)
    n = count_stepup_events(current_date, target_date, growth_anchor, growth_frequency)
    return float(pv_amount) * ((1 + float(growth_percent) / 100.0) ** n)


def _resolve_recurring_occurrences(goal, death_date):
    """Convert a Recurring goal's ``end_mode`` (Occurrences/Fixed date/Lifetime) into a concrete count."""
    if goal.get('structure') != 'Recurring':
        return int(goal.get('occurrences', 1) or 0)
    end_mode = goal.get('end_mode') or 'Occurrences'
    if end_mode == 'Occurrences':
        return int(goal.get('occurrences', 1) or 0)
    freq_months = _FREQ_TO_MONTHS.get(goal.get('frequency'))
    if freq_months is None:
        return int(goal.get('occurrences', 1) or 0)
    start = pd.Timestamp(goal['start_date'])
    if end_mode == 'Lifetime':
        end = pd.Timestamp(death_date) if death_date is not None else start
    elif end_mode == 'Fixed date':
        end = pd.Timestamp(goal.get('end_date') or start)
    else:
        return int(goal.get('occurrences', 1) or 0)
    if end < start:
        return 0
    months_span = (end.year - start.year) * 12 + (end.month - start.month)
    return months_span // freq_months + 1


def expand_recurring_goal_to_tranches(goal, current_date):
    """Convert a goal definition into a list of ``(date, fv_amount)`` tranches.

    For ``Lumpsum`` goals this is a single tranche. For ``Recurring`` goals there
    is one tranche per occurrence, each grown to its occurrence date by
    ``inflation_percent`` (continuous compounding from ``current_date``).
    """
    structure = goal.get('structure', 'Lumpsum')
    pv = float(goal['amount'])
    inflation = float(goal.get('inflation_percent', 0.0)) / 100.0
    start = pd.Timestamp(goal['start_date'])
    current_date = pd.Timestamp(current_date)

    if structure == 'Lumpsum':
        years_to = max(0.0, (start - current_date).days / 365.25)
        return [(start, pv * ((1 + inflation) ** years_to))]

    freq_months = _FREQ_TO_MONTHS.get(goal.get('frequency', 'Monthly'))
    if freq_months is None:
        return [(start, pv)]
    occurrences = int(goal.get('occurrences', 1) or 0)

    # "+fixedstart" (DECISIONS.md 2026-08-25): contract-fixed payments. The
    # amount escalates only until the FIRST payment - an EMI is signed, college
    # fees lock at admission - so every occurrence equals the first. Without
    # the flag each occurrence escalates to its own date (Punit doc SS4.2),
    # which is right for cost-of-living series like retirement income but
    # nearly doubles a 20-year EMI. Deliberate divergence from SS4.2, per
    # operator decision; absent flag = old behaviour, so saved plans replay
    # identically.
    fixed = bool(goal.get('payments_fixed_at_start'))
    if fixed:
        years_to_start = max(0.0, (start - current_date).days / 365.25)
        fixed_amount = pv * ((1 + inflation) ** years_to_start)

    tranches = []
    for k in range(occurrences):
        occ_date = start + relativedelta(months=k * freq_months)
        if pd.Timestamp(occ_date) > _MAX_SAFE_DATE:
            break
        if fixed:
            tranches.append((occ_date, fixed_amount))
        else:
            years_to = max(0.0, (occ_date - current_date).days / 365.25)
            tranches.append((occ_date, pv * ((1 + inflation) ** years_to)))
    return tranches






def generate_pseudo_nav(start_date, end_date, rate_of_return):
    date_range = pd.date_range(start=start_date, end=end_date, freq='D')
    annual_rate = rate_of_return
    daily_rate = (1 + annual_rate) ** (1 / 365) - 1
    days_elapsed = np.arange(len(date_range))
    nav_values = 100 * (1 + daily_rate) ** days_elapsed

    pseudo_nav_df = pd.DataFrame({
        'Date': date_range,
        'nav': nav_values
    })
    return _ensure_date_ns(pseudo_nav_df)


# --- Core Calculation Functions ---



def calculate_investment_cashflows(config, retirement_date, simulation_end_date=None):
    """Build a monthly ``[Date, Investment]`` DataFrame summing every stream in ``config['investment_streams']``.

    There is a single investment-stream concept (formerly split into Active and Passive). Each stream
    contributes from ``max(stream.start_date, current_date)`` to its end, where the end depends on
    ``stream.end_date_mode``:

    - ``'At retirement'`` -> the stream stops *at* the retirement date (exclusive of the retirement
      month), so it tracks the solver's retirement-date variable.
    - ``'Fixed'`` -> the stream runs through ``stream.end_date`` (inclusive), honored exactly even if
      that date is after retirement. It is *not* truncated at retirement.

    Investment is netted against Replenishing payouts each month (see ``net_investment_against_payouts``):
    investment funds that month's payouts first, and only the *surplus* flows into the Core Corpus. This
    function returns the gross monthly investment; the netting happens downstream in ``run_simulation``.
    ``stream.amount`` is the monthly figure **as of the stream's start date** (not today); discrete
    step-ups accrue from the start date on ``stream.step_up_date`` anniversaries at
    ``stream.step_up_frequency``.
    """
    current_date = pd.Timestamp(config['current_date'])
    retirement_date = pd.Timestamp(retirement_date)
    streams = config.get('investment_streams', []) or []

    if simulation_end_date is None:
        simulation_end_date = current_date + pd.DateOffset(years=100)
    simulation_end_date = pd.Timestamp(simulation_end_date)

    if simulation_end_date <= current_date or not streams:
        return _ensure_date_ns(pd.DataFrame({'Date': [current_date], 'Investment': [0.0]}))

    date_range = pd.date_range(start=current_date, end=simulation_end_date, freq='MS')
    df = _ensure_date_ns(pd.DataFrame({'Date': date_range, 'Investment': 0.0}))

    for stream in streams:
        stream_start = pd.Timestamp(stream['start_date'])
        s_start = max(stream_start, current_date)  # series start, clamped to today
        end_mode = stream.get('end_date_mode', 'Fixed')
        if end_mode == 'At retirement':
            # Stops at retirement — exclusive of the retirement month.
            s_end = retirement_date
            mask = (df['Date'] >= s_start) & (df['Date'] < s_end)
        else:
            # Fixed end date, honored exactly (inclusive), never capped at retirement.
            s_end = min(pd.Timestamp(stream['end_date']), simulation_end_date)
            mask = (df['Date'] >= s_start) & (df['Date'] <= s_end)
        if s_end < s_start:
            continue
        # ``amount`` is the monthly figure as of the stream's start date; step-ups accrue from there.
        amount_base = float(stream['amount'])
        step_pct = float(stream.get('step_up_percent', 0.0))
        step_freq = stream.get('step_up_frequency', 'Annual')
        # Default step-up anchor → current_date (D-P223-4, Plan 223).
        # The existing strict ``cur > start_date`` guard in count_stepup_events
        # (line ~295) + the ``target_date <= current_date → base`` short-circuit
        # (line ~305) already ensure no step-up fires on the current/start month
        # and that the first step-up lands exactly one full frequency later.
        step_anchor = pd.Timestamp(stream.get('step_up_date') or current_date)

        for idx in df.index[mask]:
            d = df.at[idx, 'Date']
            df.at[idx, 'Investment'] += amount_at_date_with_stepup(
                amount_base, step_pct, step_freq, step_anchor, stream_start, d
            )

    return df










# --- Main Simulation Logic ---

def _resolve_goals(goals, retirement_date, death_date=None):
    """Return a copy of *goals* with start_date and Recurring occurrences resolved.

    - ``start_date_mode='At retirement'`` -> overridden to *retirement_date*.
    - For Recurring goals, ``end_mode`` (Occurrences/Fixed date/Lifetime) is collapsed to a
      concrete ``occurrences`` count using *death_date* when needed.
    """
    resolved = []
    for goal in goals:
        g = dict(goal)
        if str(g.get('start_date_mode', 'Fixed')).lower() == 'at retirement':
            g['start_date'] = pd.Timestamp(retirement_date)
        else:
            g['start_date'] = pd.Timestamp(g['start_date'])
        if g.get('structure') == 'Recurring':
            g['occurrences'] = _resolve_recurring_occurrences(g, death_date)
        resolved.append(g)
    return resolved


# ============================================================================
# v2 GRID PROVISIONING + SETTLEMENT (DECISIONS.md 2026-08-24)
#
# The glide-path chains and the shared Debt/Hybrid pools are both RETIRED.
# Every goal - however long it runs - expands into cashflows, and each
# cashflow is provisioned by the goal grid (grid_engine.py / goal_grid.py).
# "Replenishing" is DERIVED (cashflows beyond the grid's reach), never typed;
# the goal `nature` input is accepted and ignored for compatibility.
#
# Failure semantics (operator decision #3, 2026-08-24): a month where Core
# cannot fund a provisioning event is NOT a failure - the slice retries every
# later month. Failure means a DUE cashflow is still short after draining its
# own sleeves, Core, and every other goal's sleeves: all pools depleted.
# ============================================================================


def _nav_value(rate, t0, date):
    """The engine's pseudo-NAV closed form (daily compounding from t0)."""
    days = (pd.Timestamp(date) - pd.Timestamp(t0)).days
    return 100.0 * (1.0 + rate) ** (days / 365.0)


def _build_goal_specs(goals, current_date):
    """Tranches + priority + display labels for every goal (no nature split).

    Display-name dedupe (+goaldedupe) now applies to ALL goals: the first goal
    of a name keeps it, later ones become "<name> #2", "<name> #3", ...
    Priority is the Goal Algo step-4 order: negotiability rank, earliest
    cashflow, larger total first - funding is sequential in this order.
    """
    specs = []
    name_counts = {}
    for goal in goals:
        base = goal['name']
        seen = name_counts.get(base, 0)
        name_counts[base] = seen + 1
        label = base if seen == 0 else f"{base} #{seen + 1}"
        neg = normalise_negotiability(goal.get('type'))
        tranches = expand_recurring_goal_to_tranches(goal, current_date)
        tranches = [(pd.Timestamp(d).replace(day=1), float(fv))
                    for d, fv in tranches if float(fv) > 0]
        if not tranches:
            continue
        first = min(d for d, _ in tranches)
        total = sum(fv for _, fv in tranches)
        specs.append({'label': label, 'neg': neg, 'tranches': tranches,
                      'sort': (_NEG_RANK[neg], first, -total)})
    specs.sort(key=lambda s: s['sort'])
    for pri, s in enumerate(specs):
        s['priority'] = pri
    return specs


class _GridSlice:
    """One slice of one cashflow's funding: a route, a target, and a pool."""
    __slots__ = ('goal', 'share', 'target_net', 'unfunded', 'hops', 'entry',
                 'conv_month', 'place', 'pool', 'priority', 'cf_date')

    def __init__(self, goal, priority, cf_date, share, target_net, hops,
                 instrument_params):
        self.goal = goal
        self.priority = priority
        self.cf_date = cf_date
        self.share = share
        self.target_net = target_net
        self.unfunded = target_net
        self.hops = hops
        self.entry = pd.Timestamp(hops[0][1])
        self.conv_month = pd.Timestamp(hops[1][1]) if len(hops) > 1 else None
        self.place = hops[0][0]
        params = instrument_params[self.place]
        self.pool = InvestmentPool(self.place.capitalize(),
                                   params['stcg_tax'], params['ltcg_tax'])


def _settle_grid_plan(month0, final_month, core_inflows, tranche_list,
                      instrument_params, nav_rates):
    """Monthly settlement of the grid plan against live FIFO pools.

    ``core_inflows``: {month: [(amount, description), ...]} - corpus seed,
    surplus income, one-time investments. ``tranche_list``: dicts with
    'goal', 'priority', 'cf_date', 'fv_net', 'slices' ([_GridSlice]).

    Returns (success, failure_details, final_trans_df, pool_trans_df,
    pool_movements_df, monthly_units) where monthly_units is
    [(month, core_units, {'debt': u, 'hybrid': u}, {goal: {'debt': u, ...}})].
    The simulation records the FIRST failure and keeps going, so diagnostics
    (workbook, CSV, comprehensive view) exist even for infeasible plans.
    """
    month0 = pd.Timestamp(month0)
    final_month = pd.Timestamp(final_month)

    cc = instrument_params['core_corpus']
    core = InvestmentPool('Core Corpus', cc['stcg_tax'], cc['ltcg_tax'])

    months = list(pd.date_range(month0, final_month, freq='MS'))
    navs = {place: {m: _nav_value(nav_rates[place], month0, m) for m in months}
            for place in ('core', 'debt', 'hybrid')}

    final_trans, pool_trans, movements, monthly_units = [], [], [], []
    core_units = 0.0
    bucket_units = {'debt': 0.0, 'hybrid': 0.0}
    goal_units = {}

    def core_row(row):
        nonlocal core_units
        if row is None:
            return
        core_units += row['units']
        final_trans.append(row)

    def pool_row(row, sl, flows):
        if row is None:
            return
        bucket = sl.place
        bucket_units[bucket] += row['units']
        gu = goal_units.setdefault(sl.goal, {'debt': 0.0, 'hybrid': 0.0})
        gu[bucket] += row['units']
        row = dict(row)
        row['Pool'] = bucket.capitalize()
        pool_trans.append(row)
        key = f"{bucket}_{'in' if row['Amount'] >= 0 else 'out'}"
        flows[key] += abs(row['Amount'])

    entries, conversions, payments = {}, {}, {}
    all_slices = []
    for tr in tranche_list:
        payments.setdefault(pd.Timestamp(tr['cf_date']), []).append(tr)
        for sl in tr['slices']:
            entries.setdefault(sl.entry, []).append(sl)
            if sl.conv_month is not None:
                conversions.setdefault(sl.conv_month, []).append(sl)
            all_slices.append(sl)

    pending = set()
    paid = set()          # slices whose cashflow has been settled
    success, failure = True, None

    for m in months:
        flows = {'debt_in': 0.0, 'debt_out': 0.0,
                 'hybrid_in': 0.0, 'hybrid_out': 0.0}
        core_nav = navs['core'][m]

        # 1. Core inflows (corpus seed, surplus income, one-time investments).
        for amount, desc in core_inflows.get(m, []):
            core_row(core.invest(m, amount, core_nav, description=desc))

        # 2. Conversions: hybrid -> debt at the final-row boundary. Convert
        #    whatever the slice actually holds; taxes on the realized gain.
        for sl in conversions.get(m, []):
            if id(sl) in paid:
                continue
            h_nav = navs['hybrid'][m]
            value = sl.pool.get_market_value(h_nav)
            if value > 0.01:
                wd = sl.pool.redeem_gross_amount(
                    m, value, h_nav, description=f"Convert to Debt: {sl.goal}")
                pool_row(wd, sl, flows)
                net = wd['net_received']
            else:
                net = 0.0
            params = instrument_params['debt']
            sl.place = 'debt'
            sl.pool = InvestmentPool('Debt', params['stcg_tax'], params['ltcg_tax'])
            if net > 0.01:
                pool_row(sl.pool.invest(m, net, navs['debt'][m],
                                        description=f"Converted from Hybrid: {sl.goal}"),
                         sl, flows)

        # 3. Provisioning entries + retries, in priority order. A short month
        #    is NOT a failure - the slice stays pending and re-sizes later.
        for sl in entries.get(m, []):
            if id(sl) not in paid:
                pending.add(sl)
        for sl in sorted(pending, key=lambda s: (s.priority, s.cf_date)):
            if sl.unfunded <= 0.01:
                pending.discard(sl)
                continue
            principal = slice_principal(sl.unfunded, sl.hops, m, navs,
                                        instrument_params, _STCG_MAX_YEARS)
            if principal <= 0.01:
                pending.discard(sl)
                continue
            wd = core.redeem_net_amount(
                m, principal, core_nav,
                description=f"Moving to {sl.place} for {sl.goal} goal.")
            got = wd['net_received']
            if abs(wd['Amount']) > 1e-9:
                core_row(wd)
            if got > 0.01:
                pool_row(sl.pool.invest(m, got, navs[sl.place][m],
                                        description=f"Provision: {sl.goal}"),
                         sl, flows)
                sl.unfunded = max(0.0, sl.unfunded * (1.0 - got / principal))
            if sl.unfunded <= 0.01:
                pending.discard(sl)

        # 4. Payments due this month, in priority order. Waterfall: own debt
        #    sleeves, own hybrid sleeves, Core, then EVERY other sleeve in
        #    reverse priority. Only exhausting all of that is failure.
        for tr in sorted(payments.get(m, []), key=lambda t: t['priority']):
            remaining = tr['fv_net']
            own = sorted(tr['slices'], key=lambda s: 0 if s.place == 'debt' else 1)
            for sl in own:
                if remaining <= _PAY_TOLERANCE:
                    break
                if sl.pool.get_market_value(navs[sl.place][m]) <= 0.01:
                    continue
                wd = sl.pool.redeem_net_amount(
                    m, remaining, navs[sl.place][m],
                    description=f"Goal Payout: {tr['goal']}")
                pool_row(wd, sl, flows)
                remaining = wd['shortfall']
            if remaining > _PAY_TOLERANCE:
                wd = core.redeem_net_amount(
                    m, remaining, core_nav,
                    description=f"Goal Payout (Core): {tr['goal']}")
                if abs(wd['Amount']) > 1e-9:
                    core_row(wd)
                remaining = wd['shortfall']
            if remaining > _PAY_TOLERANCE:
                raiders = sorted(
                    (s for s in all_slices
                     if id(s) not in paid and s not in tr['slices']),
                    key=lambda s: (-s.priority, s.cf_date))
                for s2 in raiders:
                    if remaining <= _PAY_TOLERANCE:
                        break
                    if s2.pool.get_market_value(navs[s2.place][m]) <= 0.01:
                        continue
                    wd = s2.pool.redeem_net_amount(
                        m, remaining, navs[s2.place][m],
                        description=f"Raided for {tr['goal']}")
                    pool_row(wd, s2, flows)
                    remaining = wd['shortfall']
            if remaining > _PAY_TOLERANCE and failure is None:
                success = False
                failure = {
                    'date': m, 'amount': remaining,
                    'description': (f"All pools depleted — {tr['goal']} payout "
                                    f"short by {format_inr(remaining)}"),
                }
            # Sweep any leftover back to Core. Sizing targets the cashflow
            # exactly, so this is normally rounding dust - but a slice that
            # over-delivered (e.g. funded late, then NAVs moved) would
            # otherwise be stranded in a sleeve nobody reads again.
            for sl in tr['slices']:
                leftover = sl.pool.get_market_value(navs[sl.place][m])
                if leftover > 1.0:
                    wd = sl.pool.redeem_gross_amount(
                        m, leftover, navs[sl.place][m],
                        description=f"Sweep to Core: {tr['goal']}")
                    pool_row(wd, sl, flows)
                    core_row(core.invest(m, wd['net_received'], core_nav,
                                         description=f"Swept from {sl.place}: {tr['goal']}"))
                paid.add(id(sl))
                pending.discard(sl)

        # 5. Month snapshot for movements + comprehensive view.
        movements.append({
            'Date': m,
            'Debt Pool Value': bucket_units['debt'] * navs['debt'][m],
            'Inflow to Debt': flows['debt_in'],
            'Outflow from Debt': flows['debt_out'],
            'Hybrid Pool Value': bucket_units['hybrid'] * navs['hybrid'][m],
            'Inflow to Hybrid': flows['hybrid_in'],
            'Outflow from Hybrid': flows['hybrid_out'],
        })
        monthly_units.append((m, core_units, dict(bucket_units),
                              {g: dict(u) for g, u in goal_units.items()}))

    final_trans_df = pd.DataFrame(final_trans)
    if not final_trans_df.empty:
        final_trans_df = final_trans_df.sort_values('Date').reset_index(drop=True)
    pool_trans_df = pd.DataFrame(pool_trans)
    pool_movements_df = pd.DataFrame(movements)
    return (success, failure, final_trans_df, pool_trans_df,
            pool_movements_df, monthly_units)


def generate_comprehensive_view(config, monthly_units, nav_rates, month0,
                                investment_df, payouts_df,
                                surplus_investment_df, net_payouts_df,
                                death_date):
    """Month-end view of every balance the plan holds (v2: from settlement).

    v1 rebuilt these values from transaction frames and planned chain
    schedules; v2's settlement already tracks exact unit balances per month,
    per bucket, per goal - this just prices them at month-end NAVs and merges
    the monthly cashflow attributions (same column names as v1, so the UI,
    CSV, and advisor workbook read it unchanged).
    """
    month0 = pd.Timestamp(month0)
    if not monthly_units:
        return pd.DataFrame()
    last_month = monthly_units[-1][0]
    end_date = max(last_month, pd.Timestamp(death_date))
    master_df = _ensure_date_ns(pd.DataFrame(
        {'Date': pd.date_range(start=month0, end=end_date, freq='ME')}))

    by_period = {m.to_period('M'): (cu, bu, gu)
                 for m, cu, bu, gu in monthly_units}
    goal_names = sorted({g for _m, _cu, _bu, gu in monthly_units for g in gu})

    rows_core, rows_debt, rows_hybrid = [], [], []
    rows_goal = {g: {'debt': [], 'hybrid': []} for g in goal_names}
    last = None
    for d in master_df['Date']:
        snap = by_period.get(d.to_period('M'), last)
        last = snap if snap is not None else last
        if snap is None:
            cu, bu, gu = 0.0, {'debt': 0.0, 'hybrid': 0.0}, {}
        else:
            cu, bu, gu = snap
        rows_core.append(cu * _nav_value(nav_rates['core'], month0, d))
        rows_debt.append(bu['debt'] * _nav_value(nav_rates['debt'], month0, d))
        rows_hybrid.append(bu['hybrid'] * _nav_value(nav_rates['hybrid'], month0, d))
        for g in goal_names:
            u = gu.get(g, {'debt': 0.0, 'hybrid': 0.0})
            rows_goal[g]['debt'].append(
                u['debt'] * _nav_value(nav_rates['debt'], month0, d))
            rows_goal[g]['hybrid'].append(
                u['hybrid'] * _nav_value(nav_rates['hybrid'], month0, d))

    master_df['Core Corpus Value'] = rows_core
    master_df['Debt Pool Value'] = rows_debt
    master_df['Hybrid Pool Value'] = rows_hybrid
    for g in goal_names:
        master_df[f'{g} Debt Value'] = rows_goal[g]['debt']
        master_df[f'{g} Hybrid Value'] = rows_goal[g]['hybrid']

    # Monthly cashflow attributions - identical column names to v1.
    master_df['YearMonth'] = master_df['Date'].dt.to_period('M')

    def _merge_monthly(df, value_col, out_col):
        nonlocal master_df
        if df is not None and not df.empty:
            tmp = df.copy()
            tmp['YearMonth'] = tmp['Date'].dt.to_period('M')
            agg = (tmp.groupby('YearMonth')[value_col].sum().reset_index()
                   .rename(columns={value_col: out_col}))
            master_df = master_df.merge(agg, on='YearMonth', how='left')
            master_df[out_col] = master_df[out_col].fillna(0)
        else:
            master_df[out_col] = 0.0

    _merge_monthly(investment_df, 'Investment', 'Investment')
    _merge_monthly(payouts_df, 'Amount', 'Replenishing Payouts')
    if surplus_investment_df is not None and not surplus_investment_df.empty:
        _merge_monthly(surplus_investment_df, 'Investment', 'Investment to Corpus')
    else:
        master_df['Investment to Corpus'] = master_df['Investment']
    master_df['Investment Used for Payouts'] = (
        master_df['Investment'] - master_df['Investment to Corpus']).clip(lower=0)
    _merge_monthly(net_payouts_df, 'Amount', 'Net Payouts (Pool)')

    return master_df.drop(columns=['YearMonth'])


def run_simulation(config, retirement_date, instrument_params, glide_paths=None):
    """Run one simulation for the given retirement_date and return its outcome.

    v2 (DECISIONS.md 2026-08-24): goal provisioning is the GRID - no chains,
    no shared pools, no typed nature. ``glide_paths`` is accepted and ignored
    for caller compatibility. Month-grid invariant (D-P223-2) unchanged.

    Returns ``(success, final_trans_df, failure_details, pool_movements_df,
    goal_dfs, comprehensive_df)`` - the same contract as v1.
    """
    config = _normalise_config_dates(config)

    current_date = pd.Timestamp(config['current_date'])
    retirement_date = pd.Timestamp(retirement_date)
    target_lifetime = config.get('target_lifetime', 90)
    current_age = config.get('current_age', 30)
    death_date = pd.Timestamp(current_date + pd.DateOffset(years=int(target_lifetime - current_age)))

    # 0. Resolve goals (start-at-retirement links, Recurring end modes).
    goals = _resolve_goals(config.get('goals', []), retirement_date, death_date)

    # 1. Goal specs: tranches, negotiability, priority, display labels.
    specs = _build_goal_specs(goals, current_date)

    last_cf = max((d for s in specs for d, _fv in s['tranches']),
                  default=current_date)
    final_date = min(max(last_cf, death_date), _MAX_SAFE_DATE)
    final_month = final_date.replace(day=1)

    # 2. Income streams; gross payouts; income nets against payouts FIRST
    #    (in priority order) - only the balance is provisioned, only surplus
    #    income reaches the Core Corpus.
    investment_df = calculate_investment_cashflows(config, retirement_date, final_date)

    flat = [{'spec': s, 'date': d, 'fv': fv, 'priority': s['priority']}
            for s in specs for d, fv in s['tranches']]
    payouts_df = (_ensure_date_ns(pd.DataFrame(
        [{'Date': t['date'], 'Amount': t['fv']} for t in flat]))
        if flat else pd.DataFrame({'Date': pd.Series(dtype=_NS_DTYPE),
                                   'Amount': pd.Series(dtype=float)}))

    net_amounts, surplus_investment_df = net_tranches_against_income(flat, investment_df)
    net_rows = [{'Date': t['date'], 'Amount': net}
                for t, net in zip(flat, net_amounts) if net > 1e-6]
    net_payouts_df = (_ensure_date_ns(pd.DataFrame(net_rows)) if net_rows
                      else pd.DataFrame({'Date': pd.Series(dtype=_NS_DTYPE),
                                         'Amount': pd.Series(dtype=float)}))

    nav_rates = {'core': instrument_params['core_corpus']['return'],
                 'debt': instrument_params['debt']['return'],
                 'hybrid': instrument_params['hybrid']['return']}
    months = list(pd.date_range(current_date, final_month, freq='MS'))
    navs = {place: {m: _nav_value(nav_rates[place], current_date, m)
                    for m in months}
            for place in ('core', 'debt', 'hybrid')}

    # 3. Grid slice plans per (net-funded) tranche + chain-shaped goal tables.
    tranche_list = []
    goal_rows = {s['label']: [] for s in specs}
    for t, net in zip(flat, net_amounts):
        if net <= 1e-6:
            continue
        s = t['spec']
        plan = grid_slice_plan(s['neg'], t['date'], current_date)
        slices = [_GridSlice(s['label'], s['priority'], t['date'],
                             p['share'], p['share'] * net, p['hops'],
                             instrument_params)
                  for p in plan]
        tranche_list.append({'goal': s['label'], 'priority': s['priority'],
                             'cf_date': t['date'], 'fv_net': net,
                             'slices': slices})
        tid = f"t{len(tranche_list)}"

        def _principals(sl):
            amts = [slice_principal(sl['share'] * net, sl['hops'],
                                    sl['hops'][0][1], navs, instrument_params,
                                    _STCG_MAX_YEARS)]
            for hi, (place, h_in, h_out) in enumerate(sl['hops'][:-1]):
                nxt = slice_principal(sl['share'] * net, sl['hops'],
                                      sl['hops'][hi + 1][1], navs,
                                      instrument_params, _STCG_MAX_YEARS)
                amts.append(nxt)
            return amts

        goal_rows[s['label']].extend(
            chain_table_rows(tid, s['neg'], t['date'], net, plan,
                             _principals, navs))

    goal_dfs = {}
    for s in specs:
        rows = goal_rows[s['label']]
        if rows:
            df = pd.DataFrame(rows)
            df['inflow_date'] = df['inflow_date'].astype(_NS_DTYPE)
            df['outflow_date'] = pd.to_datetime(df['outflow_date']).astype(_NS_DTYPE)
            goal_dfs[s['label']] = df

    # 4. Core inflows: corpus seed, surplus income, one-time investments.
    core_inflows = {}

    def _add_inflow(date, amount, desc):
        if amount <= 0:
            return
        core_inflows.setdefault(pd.Timestamp(date).replace(day=1), []).append(
            (float(amount), desc))

    _add_inflow(current_date, float(config['current_corpus']), 'Current Corpus')
    if surplus_investment_df is not None and not surplus_investment_df.empty:
        agg = (surplus_investment_df.assign(
            _m=surplus_investment_df['Date'].dt.to_period('M'))
            .groupby('_m')['Investment'].sum())
        for period, amount in agg.items():
            _add_inflow(period.to_timestamp(), amount, 'Investment')
    for w in config.get('one_time_investments', []) or []:
        wdate = pd.Timestamp(w['date'])
        wamount = float(w.get('amount', 0))
        if wamount > 0 and current_date <= wdate <= final_date:
            _add_inflow(wdate, wamount,
                        f"One-time Investment: {w.get('name', '')}".strip())

    # 5. Monthly settlement.
    (success, failure_details, final_trans_df, _pool_trans_df,
     pool_movements_df, monthly_units) = _settle_grid_plan(
        current_date, final_month, core_inflows, tranche_list,
        instrument_params, nav_rates)

    comprehensive_df = generate_comprehensive_view(
        config, monthly_units, nav_rates, current_date,
        investment_df, payouts_df, surplus_investment_df, net_payouts_df,
        death_date)

    return (success, final_trans_df, failure_details, pool_movements_df,
            goal_dfs, comprehensive_df)








_DEFAULT_INSTRUMENT_PARAMS = {
    'core_corpus': {'return': 0.12, 'stcg_tax': 0.20, 'ltcg_tax': 0.125},
    'equity':      {'return': 0.12, 'stcg_tax': 0.20, 'ltcg_tax': 0.125},
    'debt':        {'return': 0.06, 'stcg_tax': 0.20, 'ltcg_tax': 0.125},
    'hybrid':      {'return': 0.10, 'stcg_tax': 0.20, 'ltcg_tax': 0.125},
    'cash':        {'return': 0.04, 'stcg_tax': 0.20, 'ltcg_tax': 0.125},
}


def _solver_search(config, instrument_params, glide_paths):
    """Binary search for the earliest retirement_date that makes ``run_simulation`` succeed."""
    current_date = pd.Timestamp(config['current_date'])
    target_lifetime = config.get('target_lifetime', 90)
    current_age = config.get('current_age', 30)
    death_date = pd.Timestamp(current_date + pd.DateOffset(years=int(target_lifetime - current_age)))

    # The retirement date only matters if something is tied to it: an investment stream that stops
    # 'At retirement', or a goal whose start_date links to retirement. Fixed-end investment streams
    # are honored regardless of retirement and so never bound the search.
    streams = config.get('investment_streams', []) or []
    goals = config.get('goals', []) or []
    investment_tied = any(s.get('end_date_mode') == 'At retirement' for s in streams)
    goal_tied = any(g.get('start_date_mode') == 'At retirement' for g in goals)
    if not investment_tied and not goal_tied:
        # Nothing depends on the retirement date — feasibility is a single check at current_date.
        ok, *_ = run_simulation(config, current_date, instrument_params, glide_paths)
        return current_date if ok else None

    hi_cap = min(death_date, _MAX_SAFE_DATE)

    low = current_date.year * 12 + current_date.month
    high = hi_cap.year * 12 + hi_cap.month
    result = None
    while low <= high:
        mid = (low + high) // 2
        year = mid // 12
        month = mid % 12
        if month == 0:
            month = 12
            year -= 1
        cand = pd.Timestamp(year=year, month=month, day=1)
        ok, *_ = run_simulation(config, cand, instrument_params, glide_paths)
        if ok:
            result = cand
            high = mid - 1
        else:
            low = mid + 1
    return result


def _normalise_config_dates(config: dict) -> dict:
    """Return a copy of *config* with all input dates normalised to day=1.

    Defensive belt-and-suspenders normalisation (D-P223-2, Plan 223). The
    Pydantic schema boundary coerces dates first; this guard catches direct
    engine callers (tests, service, advisor-export) that build plain-dict
    configs without going through the schema layer.

    The copy is shallow for the top-level dict and for each stream/goal/
    one-time entry dict; it does not deep-copy DataFrames or other objects.
    """
    def _day1(v):
        if v is None:
            return None
        ts = pd.Timestamp(v)
        return ts.replace(day=1)

    cfg = dict(config)

    # current_date (D-P223-3)
    if cfg.get('current_date') is not None:
        cfg['current_date'] = _day1(cfg['current_date'])

    # Investment streams
    streams = cfg.get('investment_streams') or []
    new_streams = []
    for s in streams:
        s2 = dict(s)
        s2['start_date'] = _day1(s2.get('start_date'))
        if s2.get('end_date') is not None:
            s2['end_date'] = _day1(s2['end_date'])
        if s2.get('step_up_date') is not None:
            s2['step_up_date'] = _day1(s2['step_up_date'])
        new_streams.append(s2)
    cfg['investment_streams'] = new_streams

    # Goals
    goals = cfg.get('goals') or []
    new_goals = []
    for g in goals:
        g2 = dict(g)
        if g2.get('start_date') is not None:
            g2['start_date'] = _day1(g2['start_date'])
        if g2.get('end_date') is not None:
            g2['end_date'] = _day1(g2['end_date'])
        new_goals.append(g2)
    cfg['goals'] = new_goals

    # One-time investments
    one_time = cfg.get('one_time_investments') or []
    new_one_time = []
    for w in one_time:
        w2 = dict(w)
        if w2.get('date') is not None:
            w2['date'] = _day1(w2['date'])
        new_one_time.append(w2)
    cfg['one_time_investments'] = new_one_time

    return cfg


def find_retirement_date(config, instrument_params=None, glide_paths=None):
    """Solve for the earliest feasible retirement date via binary search.

    Server-side validation (D-P202-6) runs first: a malformed config raises
    ``PlanValidationError`` *before* any simulation, so every caller is guarded.

    Month-grid invariant (D-P223-2/3): all input dates are normalised to
    ``day=1`` before validation and simulation. This is the primary engine
    entry so the normalisation lives here.

    Returns a dict with keys:
        - ``success``: ``True`` if a feasible retirement date exists within the target lifetime.
        - ``retirement_date``: the earliest feasible date, or ``None`` if the plan is infeasible.
        - ``failure``: reserved for failure details; always ``None`` here (the solver doesn't
          surface a specific failure event — the UI re-runs the latest date for diagnostics).
    """
    # D-P223-2: defensive normalisation — coerce all input dates to day=1 before
    # any validation or simulation runs. The Pydantic layer already does this for
    # HTTP callers; this guard covers direct engine callers (tests, service, etc.).
    config = _normalise_config_dates(config)

    # D-P202-6: validate at the engine entrypoint so the HTTP layer (C2) and any
    # direct caller both get a single, authoritative input-validation gate.
    validate_plan_config(config)

    if instrument_params is None:
        instrument_params = _DEFAULT_INSTRUMENT_PARAMS
    if glide_paths is None:
        glide_paths = get_glide_paths()

    earliest = _solver_search(config, instrument_params, glide_paths)
    return {'success': earliest is not None, 'retirement_date': earliest, 'failure': None}
