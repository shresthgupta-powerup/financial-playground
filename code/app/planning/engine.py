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

# Engine version stamped on saved plans (reproducibility, D-P206-3/6).
# "1515f1e" = the v3 source the engine was ported from; the "+pool2x2" suffix
# marks the deliberate post-port divergence (Hybrid pool window 2+3 -> 2+2,
# operator decision 2026-06-09); "+lifetimefix" marks the Plan 222 pool
# death-date provisioning fix; "+monthgrid" marks the Plan 223 boundary
# coercion (all input dates snapped to day=1, step-up anchor -> current_date).
# Bump this whenever the engine's numeric behaviour changes, so two saved plans
# with the same stamp always reproduce.
ENGINE_SOURCE_SHA = "1515f1e+pool2x2+lifetimefix+monthgrid"

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
        return self.stcg_tax if holding_days <= 365 else self.ltcg_tax

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
    tranches = []
    for k in range(occurrences):
        occ_date = start + relativedelta(months=k * freq_months)
        if pd.Timestamp(occ_date) > _MAX_SAFE_DATE:
            break
        years_to = max(0.0, (occ_date - current_date).days / 365.25)
        tranches.append((occ_date, pv * ((1 + inflation) ** years_to)))
    return tranches


def compute_replenishing_payouts(goals, current_date):
    """Return a ``[Date, Amount]`` DataFrame summing every Replenishing goal's payouts."""
    rows = []
    for goal in goals:
        if str(goal.get('nature', '')).lower() != 'replenishing':
            continue
        for d, amt in expand_recurring_goal_to_tranches(goal, current_date):
            rows.append({'Date': pd.Timestamp(d), 'Amount': float(amt)})
    if not rows:
        empty = pd.DataFrame({'Date': pd.Series(dtype=_NS_DTYPE), 'Amount': pd.Series(dtype=float)})
        return empty
    df = pd.DataFrame(rows)
    df = df.groupby('Date', as_index=False)['Amount'].sum()
    df['Date'] = df['Date'].astype(_NS_DTYPE)
    return df.sort_values('Date').reset_index(drop=True)


def net_investment_against_payouts(investment_df, payouts_df, current_date):
    """Net monthly Investment streams against Replenishing payouts — aggregate, per calendar month.

    Each month: total investment funds that month's total Replenishing payouts *first*. Only the
    **balance** (``max(0, payout - investment)``) is left for the pool to fund, and only **surplus**
    investment (``max(0, investment - payout)``) is invested into the Core Corpus. The investment used
    to cover a payout bypasses the corpus entirely — it is cash paying an expense, so it incurs no
    equity cap-gains tax.

    Single corpus, single Debt/Hybrid pool, one total-investment figure per month — there is no
    stream->goal matching.

    Returns ``(net_payouts_df, surplus_investment_df)``:
      - ``net_payouts_df``  ``[Date, Amount]`` — the payout balance the pool must fund. One row per
        original payout date, reduced by that month's investment; only rows with ``Amount > 0`` survive.
      - ``surplus_investment_df`` ``[Date, Investment]`` — investment left after covering that month's
        payouts, the only investment routed into the Core Corpus.
    """
    investment_df = investment_df if investment_df is not None else pd.DataFrame({'Date': [], 'Investment': []})
    payouts_df = payouts_df if payouts_df is not None else pd.DataFrame({'Date': [], 'Amount': []})

    inc = investment_df.copy()
    if inc.empty:
        investment_by_month = {}
    else:
        inc['ym'] = inc['Date'].dt.to_period('M')
        investment_by_month = inc.groupby('ym')['Investment'].sum().to_dict()

    # Investment left in each month, decremented as it funds payouts in date order.
    remaining = dict(investment_by_month)

    net_rows = []
    if not payouts_df.empty:
        for _, r in payouts_df.sort_values('Date').iterrows():
            ym = pd.Timestamp(r['Date']).to_period('M')
            avail = remaining.get(ym, 0.0)
            gross = float(r['Amount'])
            used = min(avail, gross)
            remaining[ym] = avail - used
            net = gross - used
            if net > 1e-6:
                net_rows.append({'Date': pd.Timestamp(r['Date']), 'Amount': net})

    if net_rows:
        net_payouts_df = _ensure_date_ns(pd.DataFrame(net_rows))
        net_payouts_df = net_payouts_df.sort_values('Date').reset_index(drop=True)
    else:
        net_payouts_df = pd.DataFrame({'Date': pd.Series(dtype=_NS_DTYPE), 'Amount': pd.Series(dtype=float)})

    # Surplus investment per month = whatever investment is left after funding that month's payouts.
    if inc.empty:
        surplus_investment_df = _ensure_date_ns(pd.DataFrame({'Date': [pd.Timestamp(current_date)], 'Investment': [0.0]}))
    else:
        surplus_rows = [
            {'Date': r['Date'], 'Investment': max(0.0, remaining.get(r['Date'].to_period('M'), float(r['Investment'])))}
            for _, r in inc.iterrows()
        ]
        surplus_investment_df = _ensure_date_ns(pd.DataFrame(surplus_rows))

    return net_payouts_df, surplus_investment_df


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

def calculate_goal_cashflows(input_df, end_date, goal_value_post_tax, instrument_params, input_variables):
    current_date = input_variables['current_date']
    df = input_df.copy()
    end_date = pd.Timestamp(end_date)
    df['place'] = df['place'].str.lower()

    df['inflow_date'] = df['years from inflow till end'].apply(
        lambda years: end_date - relativedelta(years=int(years))
    )

    df['outflow_date'] = df['years from outflow till end'].apply(
        lambda x: end_date - relativedelta(years=int(x)) if pd.notna(x) else pd.NaT
    )

    df['inflow_date'] = df['inflow_date'].astype(_NS_DTYPE)
    df['outflow_date'] = pd.to_datetime(df['outflow_date']).astype(_NS_DTYPE)

    df[['inflow_date', 'outflow_date']] = df[['inflow_date', 'outflow_date']].mask(
        df[['inflow_date', 'outflow_date']] < current_date,
        current_date
    )

    df['goal_value_post_tax'] = goal_value_post_tax
    df['inflow_amount'] = 0.0
    id_to_idx = {row['id']: idx for idx, row in df.iterrows()}

    def calculate_required_inflow(target_post_tax, annual_return, tax_rate, years):
        if years == 0:
            return target_post_tax
        growth_factor = (1 + annual_return) ** years
        multiplier = growth_factor * (1 - tax_rate) + tax_rate
        return target_post_tax / multiplier

    def process_chain(goal_row_id):
        current_id = goal_row_id
        current_idx = id_to_idx[current_id]
        current_row = df.loc[current_idx]

        target_amount = current_row['goal_value_post_tax'] * (current_row['% of goal value'] / 100)
        df.at[current_idx, 'inflow_amount'] = target_amount

        while True:
            current_idx = id_to_idx[current_id]
            current_row = df.loc[current_idx]
            inflow_from = current_row['inflow_from']

            if inflow_from == 'core corpus':
                break

            source_idx = id_to_idx[inflow_from]
            source_row = df.loc[source_idx]

            inflow_date = source_row['inflow_date']
            outflow_date = current_row['inflow_date']
            years = (outflow_date - inflow_date).days / 365.25

            place = source_row['place'].lower()
            params = instrument_params.get(place, {'return': 0.0, 'stcg_tax': 0.0, 'ltcg_tax': 0.0})
            tax_rate = params['stcg_tax'] if years <= 1 else params['ltcg_tax']

            target_for_source = df.at[current_idx, 'inflow_amount']
            required_inflow = calculate_required_inflow(
                target_for_source, params['return'], tax_rate, years
            )

            df.at[source_idx, 'inflow_amount'] = required_inflow
            current_id = inflow_from

    goal_rows = df[df['place'] == 'goal']
    for _, goal_row in goal_rows.iterrows():
        process_chain(goal_row['id'])

    df['inflow_amount'] = df['inflow_amount'].round(2)
    df['total_outflow_amount'] = 0.0
    df['tax_out_of_outflow'] = 0.0

    for idx, row in df.iterrows():
        if row['place'] == 'goal':
            df.at[idx, 'total_outflow_amount'] = pd.NA
            df.at[idx, 'tax_out_of_outflow'] = pd.NA
            continue

        place = row['place'].lower()
        params = instrument_params.get(place, {'return': 0.0, 'stcg_tax': 0.0, 'ltcg_tax': 0.0})

        if pd.notna(row['outflow_date']):
            years = (row['outflow_date'] - row['inflow_date']).days / 365.25
            principal = row['inflow_amount']
            total_outflow = principal * ((1 + params['return']) ** years)
            gains = total_outflow - principal
            tax_rate = params['stcg_tax'] if years <= 1 else params['ltcg_tax']
            tax = gains * tax_rate

            df.at[idx, 'total_outflow_amount'] = round(total_outflow, 2)
            df.at[idx, 'tax_out_of_outflow'] = round(tax, 2)
        else:
            df.at[idx, 'total_outflow_amount'] = pd.NA
            df.at[idx, 'tax_out_of_outflow'] = pd.NA

    output_columns = [
        'id', 'place', 'inflow_date', 'outflow_date', 'inflow_from',
        'outflow_to', '% of goal value', 'goal_value_post_tax', 'inflow_amount',
        'total_outflow_amount', 'tax_out_of_outflow'
    ]
    return df[output_columns]


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


def get_withdrawl_df(goal_dfs):
    """Return a ``[Date, Amount, Description]`` frame of Core-Corpus chain departures.

    Crash-class fix (D-P202-5): when there are no Core-Corpus withdrawals (no goals,
    0-occurrence goals, etc.) this returns a **typed** empty frame with the three
    expected columns rather than a column-less ``pd.DataFrame([])``. The original v3
    code returned a column-less frame, which made the downstream
    ``add_withdrawls_to_trans`` ``sort_values('Date')`` raise ``KeyError: 'Date'``.
    """
    results = []
    for name, df in goal_dfs.items():
        # Filter for 'core corpus' withdrawals
        for _, row in df[df['inflow_from'] == 'core corpus'].copy(deep=True).sort_values(by='inflow_date').iterrows():
            results.append({
                'Date': row['inflow_date'],
                'Amount': row['inflow_amount'],
                'Description': f'Moving to {row["place"]} for {name} goal.'
            })
    if not results:
        return pd.DataFrame({
            'Date': pd.Series(dtype=_NS_DTYPE),
            'Amount': pd.Series(dtype=float),
            'Description': pd.Series(dtype=object),
        })
    return pd.DataFrame(results)


def create_core_corpus_trans(nav_df, investment_df, config):
    """Open the Core Corpus with current_corpus, then layer in monthly Investment inflows."""
    trans = []
    current_corpus = float(config['current_corpus'])
    current_date = pd.Timestamp(config['current_date'])

    nav_rows = nav_df[nav_df['Date'] == current_date]
    if nav_rows.empty:
        nav_rows = nav_df[nav_df['Date'] <= current_date]
    nav = nav_rows['nav'].iloc[-1] if not nav_rows.empty else nav_df['nav'].iloc[0]
    trans.append({
        'Date': current_date, 'Amount': current_corpus, 'NAV': nav,
        'units': current_corpus / nav, 'Description': 'Current Corpus'
    })

    for _, row in investment_df.iterrows():
        amount = float(row['Investment'])
        if amount <= 0:
            continue
        date = row['Date']
        matches = nav_df[nav_df['Date'] <= date]
        nav = matches['nav'].iloc[-1] if not matches.empty else nav_df['nav'].iloc[0]
        trans.append({
            'Date': date, 'Amount': amount, 'NAV': nav,
            'units': amount / nav, 'Description': 'Investment'
        })

    return pd.DataFrame(trans)


def add_withdrawls_to_trans(sip_trans_df, withdrawls_df, nav_df, instrument_params):
    updated_trans_df = sip_trans_df.copy(deep=True)
    # Ensure numeric columns are float to avoid dtype upcast errors on partial updates
    for col in ['Amount', 'units', 'NAV']:
        if col in updated_trans_df.columns:
            updated_trans_df[col] = updated_trans_df[col].astype(float)
    withdrawal_transactions = []

    # Combine withdrawals from goals and post-retirement expenses
    # Assume withdrawls_df has both

    # Crash-class fix (D-P202-5): an empty / no-Date withdrawals frame means there are
    # no Core-Corpus departures to settle. Skip the loop entirely and return the
    # SIP-trans frame with the tax/fully_funded/shortfall columns + success=True,
    # instead of letting ``sort_values('Date')`` raise ``KeyError: 'Date'``.
    if withdrawls_df is None or 'Date' not in withdrawls_df.columns or withdrawls_df.empty:
        sip_trans_final = sip_trans_df.copy()
        sip_trans_final['tax'] = 0
        sip_trans_final['fully_funded'] = True
        sip_trans_final['shortfall'] = 0
        sip_trans_final = sip_trans_final.sort_values('Date').reset_index(drop=True)
        return sip_trans_final, True, None

    # Sort withdrawals by date
    withdrawls_df = withdrawls_df.sort_values('Date').reset_index(drop=True)

    for _, row in withdrawls_df.iterrows():
        amount = row['Amount']
        date = row['Date']
        description = row['Description']

        # Get NAV
        matches = nav_df[nav_df['Date'] <= date]
        if not matches.empty:
            current_nav = matches['nav'].iloc[-1]
        else:
            current_nav = nav_df['nav'].iloc[0]  # Should not happen if nav covers range

        # Get available units up to this date
        available_trans_df = updated_trans_df[updated_trans_df['Date'] <= date].copy()

        if available_trans_df.empty:
            withdrawal_transactions.append({
                'Date': date, 'Amount': -amount, 'NAV': current_nav, 'units': -amount / current_nav,
                'Description': description, 'tax': 0, 'fully_funded': False, 'shortfall': amount
            })
            continue

        # Calculate Taxes and Liquidation
        cc_stcg = instrument_params['core_corpus']['stcg_tax']
        cc_ltcg = instrument_params['core_corpus']['ltcg_tax']
        available_trans_df['current_value'] = available_trans_df['units'] * current_nav
        available_trans_df['gains'] = available_trans_df['current_value'] - available_trans_df['Amount']
        available_trans_df['holding_days'] = (date - available_trans_df['Date']).dt.days
        available_trans_df['applicable_tax_rate'] = available_trans_df['holding_days'].apply(
            lambda d: cc_stcg if d <= 365 else cc_ltcg
        )
        available_trans_df['tax'] = available_trans_df['gains'] * available_trans_df['applicable_tax_rate']
        available_trans_df['post_tax_current_value'] = available_trans_df['current_value'] - available_trans_df['tax']

        remaining_amount = amount
        trans_ids_to_remove = []
        trans_ids_to_update = {}
        total_units_withdrawn = 0
        total_pretax_amount = 0
        total_tax_paid = 0

        for id_, row_ in available_trans_df.iterrows():
            if remaining_amount <= 0:
                break

            available_val = row_['post_tax_current_value']

            if remaining_amount >= available_val:
                remaining_amount -= available_val
                trans_ids_to_remove.append(id_)
                total_units_withdrawn += row_['units']
                total_pretax_amount += row_['current_value']
                total_tax_paid += row_['tax']
            else:
                fraction = remaining_amount / available_val
                units_wd = row_['units'] * fraction
                pretax_wd = row_['current_value'] * fraction
                tax_wd = row_['tax'] * fraction

                total_units_withdrawn += units_wd
                total_pretax_amount += pretax_wd
                total_tax_paid += tax_wd

                trans_ids_to_update[id_] = {
                    'units': row_['units'] - units_wd,
                    'Amount': row_['Amount'] * (1 - fraction)
                }
                remaining_amount = 0

        fully_funded = (remaining_amount <= 1e-6)

        # Apply updates
        updated_trans_df = updated_trans_df.drop(trans_ids_to_remove)
        for id_, updates in trans_ids_to_update.items():
            updated_trans_df.loc[id_, 'units'] = updates['units']
            updated_trans_df.loc[id_, 'Amount'] = updates['Amount']

        updated_trans_df = updated_trans_df.reset_index(drop=True)

        if fully_funded:
            withdrawal_transactions.append({
                'Date': date, 'Amount': -total_pretax_amount, 'NAV': current_nav,
                'units': -total_units_withdrawn, 'Description': description,
                'tax': total_tax_paid, 'fully_funded': True, 'shortfall': 0
            })
        else:
            withdrawal_transactions.append({
                'Date': date, 'Amount': -amount, 'NAV': current_nav,
                'units': -amount / current_nav, 'Description': description,
                'tax': 0, 'fully_funded': False, 'shortfall': remaining_amount
            })

    # Combine
    sip_trans_final = sip_trans_df.copy()
    sip_trans_final['tax'] = 0
    sip_trans_final['fully_funded'] = True
    sip_trans_final['shortfall'] = 0

    trans_df = pd.concat([sip_trans_final, pd.DataFrame(withdrawal_transactions)], ignore_index=True)
    trans_df = trans_df.sort_values('Date').reset_index(drop=True)

    failed = trans_df[trans_df['fully_funded'] == False]  # noqa: E712 (pandas mask)
    success = len(failed) == 0

    failure_details = None
    if not success:
        first_fail = failed.iloc[0]
        failure_details = {
            'date': first_fail['Date'],
            'amount': abs(first_fail['shortfall']),  # Shortfall amount
            'description': first_fail['Description']
        }

    return trans_df, success, failure_details


def calculate_daily_value(final_trans_df, nav_df):
    trans_df = final_trans_df.copy(deep=True)
    trans_df['Date'] = trans_df['Date'].astype(_NS_DTYPE)
    trans_df = trans_df.sort_values('Date').reset_index(drop=True)

    trans_df = trans_df.groupby('Date', as_index=False)['units'].sum()

    trans_df['cumulative_units'] = trans_df['units'].cumsum()
    units_df = trans_df[['Date', 'cumulative_units']]

    units_df['Date'] = units_df['Date'].astype(_NS_DTYPE)
    units_df = units_df.sort_values('Date')

    if units_df.empty:
        return pd.DataFrame(columns=['Date', 'cumulative_units', 'nav', 'current_value'])

    full_dates = pd.date_range(
        start=units_df['Date'].min(),
        end=units_df['Date'].max(),
        freq='D'
    )

    units_df = (
        units_df
        .set_index('Date')
        .reindex(full_dates)
    )

    units_df['cumulative_units'] = units_df['cumulative_units'].ffill()
    units_df = units_df.reset_index().rename(columns={'index': 'Date'})

    units_df = units_df.merge(nav_df, on='Date', how='left')

    units_df['nav'] = units_df['nav'].ffill()
    units_df['current_value'] = units_df['cumulative_units'] * units_df['nav']

    return units_df


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


def run_simulation(config, retirement_date, instrument_params, glide_paths=None):
    """Run a single simulation for the given retirement_date and return its outcome.

    Month-grid invariant (D-P223-2): input dates are normalised to day=1 here as
    a defensive guard for callers that bypass ``find_retirement_date`` (e.g. the
    service's infeasible-diagnostics path, direct test calls). The solver already
    goes through ``find_retirement_date`` which normalises first; this second call
    is idempotent.

    Returns ``(success, final_trans_df, failure_details, pool_movements_df, goal_dfs, comprehensive_df)``.
    """
    if glide_paths is None:
        glide_paths = get_glide_paths()

    # D-P223-2: defensive day=1 normalisation for direct callers.
    config = _normalise_config_dates(config)

    current_date = pd.Timestamp(config['current_date'])
    retirement_date = pd.Timestamp(retirement_date)
    target_lifetime = config.get('target_lifetime', 90)
    current_age = config.get('current_age', 30)
    death_date = pd.Timestamp(current_date + pd.DateOffset(years=int(target_lifetime - current_age)))

    # 0. Resolve goals: linked start_dates and Recurring end_mode -> concrete occurrences.
    goals = _resolve_goals(config.get('goals', []), retirement_date, death_date)

    # 1. Non-replenishing goals -> chain math, one tranche per occurrence.
    goal_dfs = {}
    last_goal_date = current_date
    for goal in goals:
        if str(goal.get('nature', '')).lower() == 'replenishing':
            continue
        tranches = expand_recurring_goal_to_tranches(goal, current_date)
        for i, (tranche_date, tranche_fv) in enumerate(tranches):
            if tranche_date > last_goal_date:
                last_goal_date = tranche_date
            label = goal['name'] if len(tranches) == 1 else f"{goal['name']} ({i+1}/{len(tranches)})"
            goal_dfs[label] = calculate_goal_cashflows(
                input_df=glide_paths[goal['type']],
                end_date=tranche_date,
                goal_value_post_tax=tranche_fv,
                instrument_params=instrument_params,
                input_variables=config,
            )

    final_date = min(max(last_goal_date, death_date), _MAX_SAFE_DATE)

    # 2. NAV series.
    nav_df = generate_pseudo_nav(current_date, final_date, instrument_params['core_corpus']['return'])
    debt_nav_df = generate_pseudo_nav(current_date, final_date, instrument_params['debt']['return'])
    hybrid_nav_df = generate_pseudo_nav(current_date, final_date, instrument_params['hybrid']['return'])

    # 3. Cashflow series — a single unified monthly Investment series and gross Replenishing payouts.
    investment_df = calculate_investment_cashflows(config, retirement_date, final_date)
    payouts_df = compute_replenishing_payouts(goals, current_date)

    # 3b. Net investment against payouts (aggregate, per calendar month). Investment funds payouts
    #     first; only the *balance* needs the pool, and only *surplus* investment flows into the Core Corpus.
    net_payouts_df, surplus_investment_df = net_investment_against_payouts(investment_df, payouts_df, current_date)

    # 4. Pool simulation — driven by the NET payout balance (after investment). Runs only when
    #    investment fails to fully cover the Replenishing payouts in some month; if investment covers
    #    everything, there is no pool at all.
    if net_payouts_df.empty:
        pool_trans_df = pd.DataFrame()
        core_replenishments_df = pd.DataFrame()
        pool_movements_df = pd.DataFrame()
    else:
        pool_start = min(pd.Timestamp(net_payouts_df['Date'].min()), retirement_date)
        (pool_trans_df, core_replenishments_df,
         failure_date, failure_reason, pool_movements_df) = simulate_pool(
            net_payouts_df, debt_nav_df, hybrid_nav_df,
            instrument_params['debt'], instrument_params['hybrid'], pool_start, final_date,
        )
        if failure_date:
            return (False, pool_trans_df,
                    {'date': failure_date, 'amount': 0, 'description': failure_reason},
                    pool_movements_df, goal_dfs, pd.DataFrame())

    # 5. Build Core Corpus transactions: current corpus + SURPLUS Investment (post-netting) + One-time Investments.
    core_trans = create_core_corpus_trans(nav_df, surplus_investment_df, config)

    # One-time investments — one-off inflows at face value on their date.
    for w in config.get('one_time_investments', []) or []:
        wdate = pd.Timestamp(w['date'])
        wamount = float(w.get('amount', 0))
        if wamount == 0 or wdate < current_date or wdate > final_date:
            continue
        matches = nav_df[nav_df['Date'] <= wdate]
        wnav = matches['nav'].iloc[-1] if not matches.empty else nav_df['nav'].iloc[0]
        core_trans = pd.concat([core_trans, pd.DataFrame([{
            'Date': wdate, 'Amount': wamount, 'NAV': wnav,
            'units': wamount / wnav, 'Description': f"One-time Investment: {w.get('name', '')}".strip(),
        }])], ignore_index=True)

    core_trans = core_trans.sort_values('Date').reset_index(drop=True)

    # 6. Core Corpus withdrawals: Non-replenishing chain departures + pool refills.
    withdrawals_from_goals = get_withdrawl_df(goal_dfs)
    if core_replenishments_df is None or core_replenishments_df.empty:
        all_withdrawals = withdrawals_from_goals
    else:
        all_withdrawals = pd.concat([withdrawals_from_goals, core_replenishments_df], ignore_index=True)

    final_trans_df, success, failure_details = add_withdrawls_to_trans(
        core_trans, all_withdrawals, nav_df, instrument_params,
    )

    comprehensive_df = generate_comprehensive_view(
        config, final_trans_df, pool_trans_df, goal_dfs,
        nav_df, debt_nav_df, hybrid_nav_df,
        investment_df, payouts_df,
        surplus_investment_df=surplus_investment_df, net_payouts_df=net_payouts_df,
    )

    return success, final_trans_df, failure_details, pool_movements_df, goal_dfs, comprehensive_df


def generate_comprehensive_view(config, final_trans_df, pool_trans_df, goal_dfs, nav_df,
                                debt_nav_df, hybrid_nav_df, investment_df, payouts_df,
                                surplus_investment_df=None, net_payouts_df=None):
    current_date = pd.Timestamp(config['current_date'])
    target_lifetime = config.get('target_lifetime', 90)
    current_age = config.get('current_age', 30)
    death_date = pd.Timestamp(current_date + pd.DateOffset(years=int(target_lifetime - current_age)))

    end_date = final_trans_df['Date'].max()
    if pool_trans_df is not None and not pool_trans_df.empty:
        end_date = max(end_date, pool_trans_df['Date'].max())
    # Always extend at least to death_date so the chart reaches the full target lifetime
    end_date = max(end_date, death_date)

    # Generate Month-End dates up to end_date
    full_date_range = pd.date_range(start=current_date, end=end_date, freq='ME')

    # Create the master DF
    master_df = _ensure_date_ns(pd.DataFrame({'Date': full_date_range}))

    # 1. Core Corpus Value
    core_trans = final_trans_df.copy()
    core_trans['Date'] = core_trans['Date'].astype(_NS_DTYPE)
    core_trans = core_trans.sort_values('Date')
    core_daily_cats = _ensure_date_ns(pd.DataFrame({'Date': pd.date_range(start=current_date, end=end_date, freq='D')}))

    # Agg transactions by day
    agg_trans = core_trans.groupby('Date')['units'].sum().reset_index()
    core_vals = core_daily_cats.merge(agg_trans, on='Date', how='left').fillna(0)
    core_vals['cum_units'] = core_vals['units'].cumsum()

    # Get NAVs
    core_vals = core_vals.merge(nav_df[['Date', 'nav']], on='Date', how='left').ffill()
    core_vals['Core Corpus Value'] = core_vals['cum_units'] * core_vals['nav']

    # Merge into Master
    master_df = pd.merge_asof(master_df, core_vals[['Date', 'Core Corpus Value']], on='Date')

    # 2. Expense Debt & Hybrid Pools
    if pool_trans_df is not None and not pool_trans_df.empty:
        # Separate by Pool
        pool_trans_df['Date'] = pool_trans_df['Date'].astype(_NS_DTYPE)

        for pool_name, nav_source in [('Debt', debt_nav_df), ('Hybrid', hybrid_nav_df)]:
            p_trans = pool_trans_df[pool_trans_df['Pool'] == pool_name].copy()
            if p_trans.empty:
                master_df[f'{pool_name} Pool Value'] = 0.0
                continue

            agg_p = p_trans.groupby('Date')['units'].sum().reset_index()
            daily_p = _ensure_date_ns(pd.DataFrame({'Date': pd.date_range(start=current_date, end=end_date, freq='D')}))
            daily_p = daily_p.merge(agg_p, on='Date', how='left').fillna(0)
            daily_p['cum_units'] = daily_p['units'].cumsum()

            daily_p = daily_p.merge(nav_source[['Date', 'nav']], on='Date', how='left').ffill()
            daily_p['val'] = daily_p['cum_units'] * daily_p['nav']

            master_df = pd.merge_asof(master_df, daily_p[['Date', 'val']], on='Date')
            master_df = master_df.rename(columns={'val': f'{pool_name} Pool Value'})
    else:
        master_df['Debt Pool Value'] = 0.0
        master_df['Hybrid Pool Value'] = 0.0

    # 3. Goal Specific Pools
    for goal_name, df in goal_dfs.items():
        # Initialize columns
        master_df[f'{goal_name} Debt Value'] = 0.0
        master_df[f'{goal_name} Hybrid Value'] = 0.0

        for idx, row in df.iterrows():
            place = row['place'].lower()
            if place not in ['debt', 'hybrid']:
                continue

            start_d = row['inflow_date']
            end_d = row['outflow_date'] if pd.notna(row['outflow_date']) else end_date
            amount = row['inflow_amount']

            if start_d >= end_d:
                continue

            # Select NAV DF
            curr_nav_df = debt_nav_df if place == 'debt' else hybrid_nav_df

            # Get Start NAV
            s_nav_rows = curr_nav_df[curr_nav_df['Date'] <= start_d]
            if s_nav_rows.empty:
                s_nav = curr_nav_df['nav'].iloc[0]
            else:
                s_nav = s_nav_rows['nav'].iloc[-1]

            units = amount / s_nav

            # Calculate value for the range
            mask = (master_df['Date'] >= start_d) & (master_df['Date'] <= end_d)
            subset_dates = master_df.loc[mask, 'Date']

            # Get NAVs for these dates
            temp_df = _ensure_date_ns(pd.DataFrame({'Date': subset_dates}))
            temp_df = pd.merge_asof(temp_df, curr_nav_df, on='Date')

            # Add to master
            values = temp_df['nav'] * units

            col_name = f'{goal_name} {place.capitalize()} Value'
            master_df.loc[mask, col_name] += values.values

    # 4. Monthly cashflow attributions (Investment, Replenishing Payouts).
    master_df['YearMonth'] = master_df['Date'].dt.to_period('M')

    if investment_df is not None and not investment_df.empty:
        inc = investment_df.copy()
        inc['YearMonth'] = inc['Date'].dt.to_period('M')
        inc_agg = inc.groupby('YearMonth')['Investment'].sum().reset_index()
        master_df = master_df.merge(inc_agg, on='YearMonth', how='left').fillna({'Investment': 0})
    else:
        master_df['Investment'] = 0.0

    if payouts_df is not None and not payouts_df.empty:
        pay = payouts_df.copy()
        pay['YearMonth'] = pay['Date'].dt.to_period('M')
        pay_agg = pay.groupby('YearMonth')['Amount'].sum().reset_index().rename(columns={'Amount': 'Replenishing Payouts'})
        master_df = master_df.merge(pay_agg, on='YearMonth', how='left').fillna({'Replenishing Payouts': 0})
    else:
        master_df['Replenishing Payouts'] = 0.0

    # Investment<->payout netting attributions: how the month's investment and payouts were split.
    if surplus_investment_df is not None and not surplus_investment_df.empty:
        si = surplus_investment_df.copy()
        si['YearMonth'] = si['Date'].dt.to_period('M')
        si_agg = si.groupby('YearMonth')['Investment'].sum().reset_index().rename(columns={'Investment': 'Investment to Corpus'})
        master_df = master_df.merge(si_agg, on='YearMonth', how='left').fillna({'Investment to Corpus': 0})
    else:
        master_df['Investment to Corpus'] = master_df['Investment']
    master_df['Investment Used for Payouts'] = (master_df['Investment'] - master_df['Investment to Corpus']).clip(lower=0)

    if net_payouts_df is not None and not net_payouts_df.empty:
        npay = net_payouts_df.copy()
        npay['YearMonth'] = npay['Date'].dt.to_period('M')
        npay_agg = npay.groupby('YearMonth')['Amount'].sum().reset_index().rename(columns={'Amount': 'Net Payouts (Pool)'})
        master_df = master_df.merge(npay_agg, on='YearMonth', how='left').fillna({'Net Payouts (Pool)': 0})
    else:
        master_df['Net Payouts (Pool)'] = 0.0

    master_df = master_df.drop(columns=['YearMonth'])

    return master_df


def calculate_debt_injection_need(expenses_list, injection_date, pool_params):
    # expenses_list: list of (date, amount)
    # Returns PV needed at injection_date to meet these expenses

    total_pv = 0
    rate = pool_params['return']
    stcg_tax = pool_params['stcg_tax']
    ltcg_tax = pool_params['ltcg_tax']

    for date, amount in expenses_list:
        # A payout dated before injection_date is one the caller has bucketed into
        # the current provisioning month (the withdrawal loop pays the whole
        # calendar month regardless of day). Provision it as due *now* (years=0)
        # rather than skipping it -- skipping under-provisions ~1 payout every
        # month that has a tranche earlier in the month than the pool's run date,
        # which accumulates and surfaces as a spurious depletion at the final
        # month. (Pairs with the month-aligned ``window_start`` filter in simulate_pool.)
        years_to_expense = max(0.0, (date - injection_date).days / 365.25)

        tax_rate = stcg_tax if years_to_expense <= 1 else ltcg_tax
        needed = calculate_corpus_required_for_future_expense(amount, years_to_expense, rate, tax_rate)
        total_pv += needed

    return total_pv


def simulate_pool(payouts_df, debt_nav_df, hybrid_nav_df,
                  debt_params, hybrid_params, sim_start, final_date):
    """Run the Debt+Hybrid pool simulation, driven by the NET Replenishing payout schedule.

    ``payouts_df`` is the *balance* after monthly investment has funded payouts first (see
    ``net_investment_against_payouts``) — investment covers payouts directly, and only the shortfall the
    pool must fund reaches this point. The pool is refilled from the Core Corpus on that net
    schedule.

    Returns: ``(pool_trans_df, core_replenishments_df, failure_date, failure_reason,
    pool_movements_df)``.
    """
    debt_pool = InvestmentPool('Debt', debt_params['stcg_tax'], debt_params['ltcg_tax'])
    hybrid_pool = InvestmentPool('Hybrid', hybrid_params['stcg_tax'], hybrid_params['ltcg_tax'])

    pool_transactions = []
    core_replenishments = []
    pool_movements = []

    if payouts_df is None or payouts_df.empty:
        return (pd.DataFrame(pool_transactions), pd.DataFrame(core_replenishments),
                None, None, pd.DataFrame(pool_movements))

    debt_nav_dict = dict(zip(debt_nav_df['Date'], debt_nav_df['nav']))
    hybrid_nav_dict = dict(zip(hybrid_nav_df['Date'], hybrid_nav_df['nav']))

    def get_nav(date, nav_dict, default_df):
        if date in nav_dict:
            return nav_dict[date]
        matches = default_df[default_df['Date'] <= date]
        if not matches.empty:
            return matches['nav'].iloc[-1]
        return default_df['nav'].iloc[-1]

    def log_movement(date, debt_in=0, debt_out=0, hybrid_in=0, hybrid_out=0):
        d_nav = get_nav(date, debt_nav_dict, debt_nav_df)
        h_nav = get_nav(date, hybrid_nav_dict, hybrid_nav_df)
        pool_movements.append({
            'Date': date,
            'Debt Pool Value': debt_pool.get_market_value(d_nav),
            'Inflow to Debt': debt_in, 'Outflow from Debt': debt_out,
            'Hybrid Pool Value': hybrid_pool.get_market_value(h_nav),
            'Inflow to Hybrid': hybrid_in, 'Outflow from Hybrid': hybrid_out,
        })

    payout_data = list(zip(payouts_df['Date'], payouts_df['Amount']))
    payout_last_date = payouts_df['Date'].max()

    sim_date = pd.Timestamp(sim_start)
    final_date = pd.Timestamp(final_date)
    while sim_date <= final_date and sim_date <= payout_last_date:
        debt_nav = get_nav(sim_date, debt_nav_dict, debt_nav_df)
        hybrid_nav = get_nav(sim_date, hybrid_nav_dict, hybrid_nav_df)

        # A. Determine needs over the Debt (24m) and Hybrid (25-48m) windows.
        # Pool durations 2+2 (Debt = next 2 years; Hybrid = the following 2 years).
        # This is a DELIBERATE post-port change from v3's 2+3 (months=60) per
        # operator decision (2026-06-09); the engine no longer matches v3's pool
        # window here -- the engine_version stamp reflects the divergence.
        debt_deadline = sim_date + pd.DateOffset(months=24)
        hybrid_end = sim_date + pd.DateOffset(months=48)

        # Provision from the START of sim_date's month, not sim_date itself. The
        # monthly withdrawal loop below buckets payouts by (year, month), so it
        # withdraws *every* payout in sim_date's month -- including ones dated
        # earlier in the month than sim_date's day (e.g. retirement-income
        # tranches on the 1st when sim_date lands on the 15th). Using
        # ``sim_date <= d`` would exclude those same-month-but-earlier payouts
        # from provisioning while still withdrawing them, so the pool runs short
        # -- the failure is largest for a payout that lands exactly on the
        # death/final date (e.g. a Lifetime expense), which previously surfaced
        # as a spurious "Debt Pool Depleted" at the final month. Aligning the
        # window lower bound to the month start makes provisioning cover exactly
        # what the withdrawal loop will take.
        window_start = sim_date.replace(day=1)
        debt_due = [(d, a) for d, a in payout_data if window_start <= d < debt_deadline]
        hybrid_due = [(d, a) for d, a in payout_data if debt_deadline <= d < hybrid_end]

        target_debt_val = calculate_debt_injection_need(debt_due, sim_date, debt_params)
        target_hybrid_val = calculate_debt_injection_need(hybrid_due, sim_date, hybrid_params)

        # B. Execute transfers.
        current_hybrid_val = hybrid_pool.get_market_value(hybrid_nav)
        hybrid_latent_tax = hybrid_pool.get_unrealized_tax(hybrid_nav, sim_date)
        hybrid_surplus = max(0.0, current_hybrid_val - (target_hybrid_val + hybrid_latent_tax))

        current_debt_val = debt_pool.get_market_value(debt_nav)
        debt_latent_tax = debt_pool.get_unrealized_tax(debt_nav, sim_date)
        debt_shortfall = max(0.0, (target_debt_val + debt_latent_tax) - current_debt_val)

        if debt_shortfall > 0 and hybrid_surplus > 0:
            transfer_gross = min(hybrid_surplus, debt_shortfall)
            wd_res = hybrid_pool.redeem_gross_amount(sim_date, transfer_gross, hybrid_nav,
                                                     description="Transfer to Debt (Surplus)")
            pool_transactions.append(wd_res)
            net_proceeds = wd_res['net_received']
            inv_res = debt_pool.invest(sim_date, net_proceeds, debt_nav, description="Transfer from Hybrid")
            if inv_res:
                pool_transactions.append(inv_res)
            log_movement(sim_date, debt_in=net_proceeds, hybrid_out=transfer_gross)

            current_debt_val = debt_pool.get_market_value(debt_nav)
            debt_latent_tax = debt_pool.get_unrealized_tax(debt_nav, sim_date)
            debt_shortfall = max(0.0, (target_debt_val + debt_latent_tax) - current_debt_val)

        if debt_shortfall > 0.01:
            core_replenishments.append({'Date': sim_date, 'Amount': debt_shortfall,
                                        'Description': 'Replenishment: Debt Pool'})
            inv_res = debt_pool.invest(sim_date, debt_shortfall, debt_nav, description="Replenishment from Core")
            if inv_res:
                pool_transactions.append(inv_res)
            log_movement(sim_date, debt_in=debt_shortfall)

        current_hybrid_val = hybrid_pool.get_market_value(hybrid_nav)
        hybrid_latent_tax = hybrid_pool.get_unrealized_tax(hybrid_nav, sim_date)
        hybrid_shortfall = max(0.0, (target_hybrid_val + hybrid_latent_tax) - current_hybrid_val)
        if hybrid_shortfall > 0.01:
            core_replenishments.append({'Date': sim_date, 'Amount': hybrid_shortfall,
                                        'Description': 'Replenishment: Hybrid Pool'})
            inv_res = hybrid_pool.invest(sim_date, hybrid_shortfall, hybrid_nav,
                                         description="Replenishment from Core")
            if inv_res:
                pool_transactions.append(inv_res)
            log_movement(sim_date, hybrid_in=hybrid_shortfall)

        # C. Monthly withdrawals for the next 12 months.
        next_year = sim_date + pd.DateOffset(months=12)
        m_date = sim_date
        while m_date < next_year and m_date <= final_date:
            month_payouts = [a for d, a in payout_data if d.year == m_date.year and d.month == m_date.month]
            if not month_payouts:
                # Even idle months get logged so the comprehensive view has continuous pool values.
                log_movement(m_date)
                m_date += relativedelta(months=1)
                continue

            net_withdrawal = sum(month_payouts)

            if net_withdrawal > 0:
                curr_nav = get_nav(m_date, debt_nav_dict, debt_nav_df)
                wd_res = debt_pool.redeem_net_amount(m_date, net_withdrawal, curr_nav,
                                                     description="Goal Payout")
                pool_transactions.append(wd_res)
                log_movement(m_date, debt_out=net_withdrawal)
                if not wd_res['fully_funded']:
                    return (pd.DataFrame(pool_transactions), pd.DataFrame(core_replenishments),
                            m_date, "Debt Pool Depleted",
                            pd.DataFrame(pool_movements))
            else:
                log_movement(m_date)

            m_date += relativedelta(months=1)

        sim_date = next_year

    return (pd.DataFrame(pool_transactions), pd.DataFrame(core_replenishments),
            None, None, pd.DataFrame(pool_movements))


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
