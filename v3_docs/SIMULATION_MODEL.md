# SIMULATION_MODEL — How the simulator actually works

Read this end-to-end at least once. After that, the routing in `CLAUDE.md` will tell you which section is relevant for a given question.

## The big picture

`find_retirement_date()` is solver-only: a binary search over months in `[current_date, death_date]` for the earliest date that makes `run_simulation()` succeed (all routed through `run_simulation()`).

Returns a dict: `{success, retirement_date, failure}`. (`failure` is reserved/always `None` here — the UI re-runs the latest date for diagnostics.)

`run_simulation(config, retirement_date, instrument_params, glide_paths)` for a candidate retirement date executes this pipeline:

1. **Resolve linked goal start_dates.** Any goal with `start_date_mode='At retirement'` gets `start_date = retirement_date`. Others keep their fixed date.
2. **Build Non-replenishing goal cashflows.** For each Non-replenishing goal (`expand_recurring_goal_to_tranches()`), one tranche per occurrence. Each tranche becomes its own chain through `calculate_goal_cashflows()` — same chain math as before.
3. **Build NAV series** for Core Corpus, Debt, Hybrid via `generate_pseudo_nav()`.
4. **Build Investment series** (`calculate_investment_cashflows()`) — one monthly series summed across all `investment_streams`, over the full sim horizon. Each stream runs from its start to its end, where the end is `retirement_date` (for `end_date_mode='At retirement'`, exclusive of the retirement month) or `stream.end_date` (for `Fixed`, inclusive, never capped at retirement), inflated by its step-up calendar.
5. **Net investment against payouts** (`net_investment_against_payouts()`) — bucket investment and Replenishing payouts by calendar month and net them: investment funds each month's payouts first. Produces `net_payouts_df` (the balance the pool must fund = `max(0, payout − investment)`) and `surplus_investment_df` (investment left over = `max(0, investment − payout)`). Aggregate monthly; no stream→goal matching.
6. **Build Replenishing payouts schedule** (`compute_replenishing_payouts()`) — for every Replenishing goal, expand into per-occurrence FV amounts; sum across goals month-by-month. (This is the *gross* schedule that step 5 nets investment against.)
7. **Run the pool simulator** (`simulate_pool()`) — driven by the **net** payouts from step 5 (investment already funded its share directly). Runs only when some month's payout exceeds its investment; if investment covers everything, there is no pool. Annual refills from Core Corpus. If the Debt pool ever fails to fund a net payout, the simulation fails at that month.
8. **Build Core Corpus transactions:** current corpus + **surplus** Investment (monthly, post-netting) + One-time Investments (one-off). Investment that directly funded a payout bypasses the corpus entirely (untaxed cash paying an expense).
9. **Apply Core Corpus withdrawals:** Non-replenishing goal chain departures (`get_withdrawl_df`) + Pool refills (from step 7). Settled via `add_withdrawls_to_trans()` with FIFO tax-lot accounting. If Core Corpus depletes, the simulation fails.
10. **Generate the month-by-month comprehensive dataframe** (`generate_comprehensive_view()`) for the UI.

Returns: `(success, final_trans_df, failure_details, pool_movements_df, goal_dfs, comprehensive_df)`.

## Goals (the only outflow concept)

Each goal is `{name, description, type, nature, structure, start_date_mode, start_date, amount (PV), frequency, occurrences, inflation_percent}`.

- **`type`** ∈ `Non-Negotiable | Semi-Negotiable | Negotiable` → which glide-path sheet to use (only matters for Non-replenishing).
- **`nature`** ∈ `Non-replenishing | Replenishing` → which mechanism funds it.
  - **Non-replenishing**: save up via a glide-path chain, spend, done. One chain per occurrence.
  - **Replenishing**: corpus pays out periodically. Goes through the shared pool.
- **`structure`** ∈ `Lumpsum | Recurring`.
  - **Lumpsum**: one date, one amount. `amount` is PV (today); the simulator grows it to `start_date` at `inflation_percent`.
  - **Recurring**: N occurrences at `frequency` (`Monthly | Quarterly | Half-Yearly | Annual`). `amount` is per-occurrence PV; each occurrence is grown to its own date at `inflation_percent`.
- **`start_date_mode`** ∈ `Fixed | At retirement`. `At retirement` links the goal's start to the solver's retirement-date variable — useful for "monthly income starting whenever I retire" style goals.
- **`inflation_percent`** does double duty: PV → FV from today to start_date *and* per-occurrence escalation thereafter. (Continuous compounding, applied per occurrence based on years from today.)

## Investment (one unified concept)

`investment_streams` is a single list — salary, business, rent, pension, dividends all live here. There is **no** Active/Passive distinction; streams differ only in when they stop. Each stream:

- **`amount`** — the monthly figure **as of the stream's `start_date`** (not today's rupees). Step-ups accrue from the start date onward.
- **`start_date`**
- **`end_date_mode`** ∈ `Fixed | At retirement`:
  - **`At retirement`** → the stream stops *at* the retirement date the solver finds (exclusive of the retirement month). Use for salary / work income.
  - **`Fixed`** → the stream runs through **`end_date`** exactly (inclusive) and is **never truncated at retirement**. Use for a stream with its own calendar (a lease, a fixed-term annuity).
- **`step_up_percent`**, **`step_up_frequency`** (`Annual | Quarterly | Monthly`), **`step_up_date`** (calendar anchor; defaults to `current_date - 1 day`)

Step-ups are *discrete events* on `step_up_date` anniversaries. The amount on any date = `amount × (1 + step_up_percent)^N`, where `N` is the number of step-up events between the stream's `start_date` and that date.

Multiple overlapping streams allowed — each contributes its own series, all summed monthly. **Investment nets against Replenishing payouts first** (`net_investment_against_payouts`, aggregate per calendar month): investment funds that month's payouts directly (bypassing the corpus, untaxed), only the *surplus* flows into Core Corpus, and only the payout *balance* (`max(0, payout − investment)`) is left for the pool. So post-retirement investment that covers a payout neither earns the Core/equity return nor pays equity tax — it's cash paying an expense. Surplus investment that does reach Core earns the Core return and is taxed as equity on later withdrawal.

## One-time investments

`one_time_investments = [{name, date, amount}]`. Amount is the **value on the date** (no PV adjustment — these are specific known events). Each one is a one-off positive inflow to Core Corpus on its date.

One-time investments are *not* pushed to the advisor file — they live in our internal config only.

## Glide paths (critical — read carefully)

The format in `Glide Paths.xlsx` is **not** a target-allocation table. It is a **tranche-and-chain cashflow script**.

Each sheet (`Non-Negotiable` / `Semi-Negotiable` / `Negotiable`) has one row per cashflow event. Columns:

| Column | Meaning |
|---|---|
| `id` | Row identifier, unique within the sheet. |
| `place` | Where the money sits: `hybrid`, `debt`, or `goal` (terminal). |
| `years from inflow till end` | Years before goal-end that this row's money *arrives* in its place. |
| `years from outflow till end` | Years before goal-end that this row's money *leaves* its place (NaN for `goal` rows). |
| `inflow_from` | Either `core corpus` (source is the Core Corpus) or another row's `id` (chain link). |
| `outflow_to` | The `id` this row's money flows into (NaN for `goal` rows). |
| `% of goal value` | Fraction of the total goal target that this chain delivers. The `goal` rows' percentages must sum to 100. |

**Reading a chain**: each goal row (`place='goal'`) is the endpoint of a chain. Walk backwards via `inflow_from`. Example:
- `id=3, place=goal, inflow_from=2, 25%` ← receives at year 0 (goal end)
- `id=2, place=debt, inflow_from=1, inflow=2y, outflow=0y, 25%` ← held in debt from year -2 to year 0
- `id=1, place=hybrid, inflow_from='core corpus', inflow=5y, outflow=2y, 25%` ← held in hybrid from year -5 to year -2, sourced from Core Corpus

`calculate_goal_cashflows()` walks each chain backwards and back-solves the principal that Core Corpus has to provide so the tranche reaches the goal target net of holding-period taxes (STCG ≤ 1y, LTCG > 1y). The per-link math is in `calculate_required_inflow()` inside that function.

**Glide paths are used only for Non-replenishing goals.** Replenishing goals use the shared pool mechanism (next section) — no glide path needed.

**Editing**: changing a glide path means rewriting the chain rows, not just changing percentages. See DECISIONS.md and `GLIDE_PATHS_CHANGELOG.md` for the history.

## Pool mechanics

Implemented in `simulate_pool()` (`main_v2.py`).

Two `InvestmentPool` instances: `Debt` (covers next 24 months of payouts) and `Hybrid` (covers months 25–60). Driven by the **net** Replenishing payouts schedule — gross payouts (summed across all Replenishing goals) *after* investment has funded its share each month (`net_investment_against_payouts`). Investment is netted *before* the pool; the pool only ever sees the balance, and refills that balance from Core Corpus. If investment fully covers payouts, the pool is never created.

The pool runs from `sim_start = min(first_net_payout, retirement_date)` to `final_date`. Pre-retirement Replenishing goals are supported (e.g., a SWP starting before retirement) — Core Corpus funds the pool as needed.

At the start of each annual cycle (`sim_date`):

1. Compute `target_debt_val` = PV at `sim_date` of net payouts for the next 24 months at the Debt return + STCG/LTCG. Same for `target_hybrid_val` over months 25–60.
2. Compare to current market value of each pool **plus latent unrealised tax** (the pool needs to be "big enough" so that after taxes it covers the target).
3. If Hybrid has surplus and Debt has shortfall: transfer Hybrid → Debt first (avoids unnecessary Core withdrawals).
4. Refill any remaining Debt shortfall from Core Corpus (`core_replenishments`).
5. Refill Hybrid shortfall from Core Corpus.
6. Loop the next 12 months: withdraw the full month `payout` from Debt. If Debt redemption returns `fully_funded=False`, the simulation fails at that month.

## Tax-lot accounting

`InvestmentPool` and the `add_withdrawls_to_trans()` Core-Corpus path both use **FIFO tax lots**:
- Each investment creates a `TaxLot(date, units, purchase_price_per_unit)`.
- Redemptions consume lots in FIFO order. Per-lot tax = `gain × (STCG if holding_days ≤ 365 else LTCG)`.
- Two redemption modes: `redeem_net_amount(target_net)` (back-solve units to land on a target post-tax amount) and `redeem_gross_amount(target_gross)` (just sell `target_gross` worth, tax falls out as side-effect).

## Retirement-date solver (binary search)

`_solver_search()` searches months in `[current_date, death_date]` (Fixed investment streams are retirement-independent, so they no longer bound the search):

```
low = current month
high = capped death/end month
while low <= high:
    mid = (low + high) // 2
    if run_simulation(retirement_date=mid).success:
        record mid; try earlier (high = mid - 1)
    else:
        need more time (low = mid + 1)
```

Result is the earliest first-of-month that succeeds, or `None` if no month in range works.

If nothing is tied to the retirement date — no investment stream with `end_date_mode='At retirement'` **and** no goal with `start_date_mode='At retirement'` — the solver short-circuits to a single feasibility check at `current_date` ("already retired" mode).

## Advisor export

`advisor_export.build_advisor_workbook(config, result, comprehensive_df=, snapshot=)` produces an Excel byte stream with these sheets:

- **Personal & Corpus** — key-value pairs for date / age / lifetime / corpus.
- **Investment Streams**, **One-time Investments** — our own input shapes (one Investment Streams sheet with an End Mode / End Date column).
- **Goals** — matches the advisor file's column layout exactly. Amounts are converted PV → FV at goal start_date. `Lumpsum` uses `Goal_amt_total`; `Recurring` uses `Goal_amt_per_occurrence`.
- **Picklists** — mirrors the advisor file's picklist sheet for downstream validation.
- **Simulation Result** — mode, success, retirement date, earliest feasible, plus the retirement snapshot.
- **Comprehensive Monthly** — the full month-by-month wealth view.

This export is one-way (us → advisor). We don't (yet) ingest the advisor's Excel back as a config.

## Date discipline

There is a single nanosecond dtype constant `_NS_DTYPE = "datetime64[ns]"` at the top of `main_v2.py` and two helpers:
- `_ensure_date_ns(df)` — cast `df['Date']` to `datetime64[ns]` in place.
- `_ts(val)` — return a `pd.Timestamp` at `[ns]` resolution.

Every DataFrame with a `Date` column must use this resolution before any `merge_asof`. Newer pandas (>=3.0) is stricter about cross-resolution merges; the pin in `requirements.txt` exists because Streamlit Cloud previously resolved an older version that broke this.

## Things that look weird but are intentional

- **`core_corpus`, `equity`, and `hybrid` all default to similar returns (12%/12%/10%).** They're conceptually different but the model uses the same number unless overridden. See DECISIONS.md 2026-05-21 entry.
- **`generate_pseudo_nav()` produces a smooth compounding curve, not real NAVs.** Deliberate: deterministic single-path simulation.
- **Goal `% of goal value` sums to 100 across `goal` rows, not across all rows.** Inflow/debt/hybrid rows carry the same percentage as their downstream goal row — that's how the back-solve walks the chain.
- **Recurring goals fan out into N internal tranches** (one per occurrence). A `Recurring` Non-replenishing goal with 4 occurrences runs the chain math 4 times, each ending at a different occurrence date. The UI shows them under one user-facing goal name.
- **The retirement-income concept does not exist as code** — it's just a `Replenishing Recurring` goal with `start_date_mode='At retirement'`. If you find yourself adding a "retirement income" special case, stop and reconsider.
