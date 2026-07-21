# ENGINE contract — the simulator itself

Part 2 of the two-part architecture: `../code/app/planning/engine.py`
(~1,200 lines, byte-identical to the shipped app at the snapshot date).
Pure computation — pandas + numpy + python-dateutil only; no DB, no web
framework, no config. This file describes the callable surface and the moving
parts; the model **rationale** is canonical in `../v3_docs/SIMULATION_MODEL.md`
(how it works end-to-end) and `../v3_docs/DECISIONS.md` (why each non-obvious
choice was made). Read both before changing ANY behaviour — several things
that look like bugs are deliberate (see "Deliberate model choices" below).

## Version stamp

```python
ENGINE_SOURCE_SHA = "1515f1e+pool2x2+lifetimefix+monthgrid"
```
- `1515f1e` — the commit of the standalone v3 repo (`Financial Planning v3`,
  branch `feature/income-model-rework`) this engine was ported from, near-verbatim.
- `+pool2x2` — one deliberate numeric divergence from v3: the Hybrid pool window
  narrowed from months 25–60 to months 25–48 (operator decision, 2026-06-09).
- `+lifetimefix` — a pool death-date provisioning bug fix (see Pool mechanics).
- `+monthgrid` — all input dates coerced to day=1 at engine entry.

The app stamps every saved plan with this string; if you fork the engine and
change behaviour, change your own version stamp too — two results are only
comparable under the same stamp.

## Entrypoints

### `find_retirement_date(config, instrument_params=None, glide_paths=None) -> dict`
The solver. Normalises dates (day=1), runs `validate_plan_config` (raises
`PlanValidationError`), defaults `instrument_params` to
`_DEFAULT_INSTRUMENT_PARAMS` and `glide_paths` to `get_glide_paths()`, then
binary-searches months in `[current_date, death_date]` for the earliest date
where `run_simulation` succeeds.

Returns `{'success': bool, 'retirement_date': pd.Timestamp | None, 'failure': None}`
(`failure` is always None here — for a diagnostic, re-run `run_simulation` at
`death_date` and read its `failure_details`; that is exactly what the app's
service layer does on the infeasible path).

Short-circuit: if NO stream has `end_date_mode='At retirement'` AND no goal has
`start_date_mode='At retirement'`, nothing depends on the retirement date — the
solver collapses to a single feasibility check at `current_date`.

### `run_simulation(config, retirement_date, instrument_params, glide_paths=None) -> 6-tuple`
One deterministic simulation at one candidate retirement date.
**`instrument_params` is REQUIRED here** (only the solver defaults it), and
this function does NOT validate the config (the solver pre-validates).

Returns `(success, final_trans_df, failure_details, pool_movements_df, goal_dfs, comprehensive_df)`:
| element | content |
|---|---|
| `success` | bool — plan feasible at this date |
| `final_trans_df` | the full transaction ledger (investments, withdrawals, tax per row) |
| `failure_details` | dict describing the first failure event, or None |
| `pool_movements_df` | Debt/Hybrid pool inflows/outflows/refills |
| `goal_dfs` | dict `{goal label -> glide-path cashflow DataFrame}` — NON-replenishing goals only |
| `comprehensive_df` | month-by-month wide view; `Date` **column** (not index) + one `<bucket> Value` column per corpus/pool/tranche — total wealth per month = row-sum of all `*Value` columns. NOTE: `Date` values are month-END timestamps (`pd.date_range(freq='ME')`), NOT the day=1 grid the inputs use — join with `>=` or nearest-match, never equality against a day=1 date (see `run_example.py` and `service._build_snapshot` for both patterns) |

The 10-step internal pipeline (resolve linked start dates → non-replenishing
chains → NAV series → investment series → net-off against payouts → replenishing
payout schedule → pool simulation → Core Corpus inflows → withdrawals →
comprehensive view) is documented step-by-step in `SIMULATION_MODEL.md § The
big picture`.

### Supporting exports
- `validate_plan_config(config)` / `PlanValidationError` / `MAX_NONREPLENISHING_SPAN_MONTHS` (see `docs/INPUT_contract.md`)
- `get_glide_paths()` → `{'Non-Negotiable' | 'Semi-Negotiable' | 'Negotiable': DataFrame}`, `GLIDEPATH_VERSION = 1`
- `TaxLot`, `InvestmentPool` — the FIFO tax-lot machinery (importable for unit-level experimentation)
- `_DEFAULT_INSTRUMENT_PARAMS` — per-bucket `{'return','stcg_tax','ltcg_tax'}`:
  core 12% / equity 12% / debt 6% / hybrid 10% / cash 4%; STCG 20% and LTCG 12.5% on every bucket

## Glide paths (tranche-and-chain — NOT a target-allocation table)

`glide_paths.py` carries 3 sheets as literals. Each row is one **cashflow
event** with `inflow_from` / `outflow_to` chain links and a `% of goal value`;
`calculate_goal_cashflows` walks each chain backwards from the goal date and
back-solves the Core-Corpus principal net of holding-period tax. Used ONLY for
Non-replenishing goals. Goal-row percentages sum to 100 per sheet. Read
`SIMULATION_MODEL.md § Glide paths (critical — read carefully)` before touching
this — it is the most misread part of the model. A playground that wants
user-editable glide paths should keep this row format and version its data
(the app stamps `glidepath_version` on every saved plan for the same reason).

## Pool mechanics (Replenishing goals)

`simulate_pool` runs two `InvestmentPool`s driven by the NET replenishing
payout schedule (monthly investment funds its share first — the net-off):
- **Debt pool** — provisions the next 24 months of payouts; the ONLY pool that
  pays payouts.
- **Hybrid pool** — buffer for months 25–48 (the `+pool2x2` divergence; v3 used
  25–60). Transfers surplus into Debt; NEVER pays a payout directly. If Debt
  cannot cover a net payout the run FAILS — it does not dip into Hybrid.
- Annual refills from Core Corpus; Hybrid→Debt transfer is attempted before a
  Core draw.
- Expected behaviour, not a bug: the Debt pool reads ₹0 while ongoing
  investment fully covers payouts (net payout = 0).
- `+lifetimefix`: the window provisions from the START of the sim month, and a
  same-month payout dated before the run date counts as due-now — without both,
  ~1 payout/month went unprovisioned and any pre-retirement `Lifetime` expense
  eventually produced a spurious "Debt Pool Depleted" failure.

## Tax-lot accounting

`InvestmentPool` and the Core-Corpus withdrawal path use FIFO tax lots:
per-lot tax = `gain × (STCG if holding_days ≤ 365 else LTCG)`. Two redemption
modes: `redeem_net_amount` (back-solve gross so the investor receives a target
post-tax amount) and `redeem_gross_amount`. See `SIMULATION_MODEL.md § Tax-lot
accounting`.

## Deliberate model choices — do NOT "fix" these

All documented with rationale in `../v3_docs/DECISIONS.md`:
- Monthly investment **nets against Replenishing payouts before the pools**;
  income covering an expense bypasses Core Corpus entirely, untaxed.
- Goals are **PV-in / FV-out** (amount in today's rupees, inflated to the goal
  date); streams are **amount-as-of-start-date** (no PV). Asymmetric on purpose.
- Glide paths stay tranche-and-chain, not target-allocation.
- STCG/LTCG per holding period (replaced a flat tax rate).
- `generate_comprehensive_view`'s per-tranche column fan-out emits pandas
  PerformanceWarnings — inherited from v3, known, not a defect.
- The month grid: everything happens on the 1st. See INPUT_contract.md.

## Failure modes (what "infeasible" means)

Two diagnostic classes surface via `failure_details`:
- **Core Corpus depletion** — the ledger keeps going with shortfall rows, so a
  full monthly view exists for diagnostics.
- **Debt pool depletion** — the run early-returns with an EMPTY comprehensive
  view (the app's CSV endpoint turns this into HTTP 409).

## Performance envelope

Synchronous, single-path. The app's worst allowed input (49 monthly
non-replenishing occurrences — the span cap's ceiling) stays under a 3s budget;
a typical solve (binary search ≈ 10 `run_simulation` calls) lands well under
that. If a playground removes the span cap, the non-replenishing chain fan-out
is the cliff to watch (each occurrence spawns its own glide-path chain).

## The app layers above the engine (reference-only here)

- `service.py` — `simulate_plan(config) -> dict`: orchestrates
  solve → simulate → build snapshot / per-goal results / monthly wealth rows;
  on infeasible configs re-runs at `death_date` for the diagnostic. Reads glide
  paths from the app DB (`load_glide_paths`), so it does not run standalone —
  but its `_build_snapshot` / `_build_goal_results` / `_build_wealth_monthly`
  helpers are pure and show exactly how the app shapes engine output for a UI.
- `advisor_export.py` — `build_advisor_workbook(...) -> xlsx bytes`: the
  advisor-facing Excel (Action Plan sheet with Invest/Switch/Pay-out rows,
  per-goal sheets, Income & Pools, Comprehensive Monthly). Imports only the
  engine — runs standalone.
- `routes.py`, `plans_repo.py`, `glide_paths_repo.py` — HTTP + persistence
  (versioned immutable saved plans). App-coupled; included for reference.
