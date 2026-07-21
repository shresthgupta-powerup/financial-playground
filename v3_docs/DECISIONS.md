# DECISIONS — append-only log of non-obvious modelling and structural choices

Read before changing existing methodology. New entries at the top. Each entry: ISO date • short title • rationale • trade-off / when to revisit.

This file is seeded from the commit history that's actually in the repo today (2026-05-21). Going forward, append a new entry whenever a structural or modelling change goes in — the binary nature of `Glide Paths.xlsx` and the implicit nature of "why we chose this default" mean git log alone won't explain things later.

---

## 2026-06-03 — Renamed "income streams" → "investment streams" and "windfalls" → "one-time investments" (everywhere, incl. config keys)

**What changed:** a terminology rename across the whole stack — UI labels, config keys, function names, dataframe columns, advisor export sheets, and docs. No behavioural change.

- Config keys: `income_streams` → **`investment_streams`**, `windfalls` → **`one_time_investments`**. No migration path (consistent with the schema's no-migration convention).
- Functions: `calculate_income_cashflows` → `calculate_investment_cashflows`; `net_income_against_payouts` → `net_investment_against_payouts`; Streamlit `render_income_streams`/`render_windfalls` → `render_investment_streams`/`render_one_time_investments`; advisor `_income_sheet`/`_windfalls_sheet` → `_investment_sheet`/`_one_time_investments_sheet`.
- Comprehensive-view columns: `Income` → `Investment`, `Income to Corpus` → `Investment to Corpus`, `Income Used for Payouts` → `Investment Used for Payouts`.
- Advisor export sheet names: `Income Streams` → `Investment Streams`, `Windfalls` → `One-time Investments`.
- UI: section headers `💰 Investment Streams` and `🎁 One-time Investments`; default stream name `Stream 1`.

**Why:** the user prefers "investment streams" / "one-time investments" as the user-facing and internal vocabulary — these inflows are framed as money put in, and the advisory team sees the renamed sheets.

**Deliberately NOT renamed:** any goal-context use of "income" — notably the **"Retirement Income"** goal name/description in the sample config and UI defaults, and the goal-side captions. Those refer to the *payout* a Replenishing goal produces, not the input streams. The earlier (same-day) netting entry below still references the pre-rename identifiers (`net_income_against_payouts`, `income_streams`); this entry is the old→new map.

---

## 2026-06-03 — Income now nets against Replenishing payouts before the pool; only surplus reaches Core Corpus

**What changed:** the routing rule from the 2026-06-02 unification ("all income flows into Core Corpus; the pool runs on gross payouts") is reversed. Income now funds Replenishing payouts **first**, per calendar month, in aggregate:

- New helper `net_income_against_payouts(income_df, payouts_df, current_date)` buckets both sides by `(year, month)` and returns `(net_payouts_df, surplus_income_df)`. For each month: `used = min(income, payout)`, `net_payout = max(0, payout − income)`, `surplus_income = max(0, income − payout)`.
- `run_simulation` drives `simulate_pool()` with **net** payouts (not gross) and builds the Core Corpus from **surplus** income (not gross income). If income covers every month's payouts, `net_payouts_df` is empty and **no pool is created at all**.
- The income used to cover a payout **bypasses the corpus entirely** — it is cash paying an expense, so it incurs **no** equity cap-gains tax. Only surplus income is invested in Core and taxed (cap gains) on later withdrawal. This is the deliberate tax treatment chosen with the user (the alternative — route all income through Core and only let netting decide the pool draw — was rejected as it keeps the tax drag and contradicts "income funds the payout directly").
- Granularity is **aggregate monthly**: total income vs total Replenishing payout for the month. There is no stream→goal matching — single corpus, single Debt pool, single Hybrid pool, one total-income figure per month. (User confirmed this is the intended architecture.)
- `generate_comprehensive_view` gains three columns: `Income to Corpus` (surplus), `Income Used for Payouts` (gross income − surplus), `Net Payouts (Pool)` (the balance the pool funds). Gross `Income` and `Replenishing Payouts` columns are retained.

**Why:** routing all income through Core and funding payouts via Core→pool refills double-taxed and over-conservatised plans — income earned equity returns then paid cap-gains tax on the way out to refill the pool, even when it could have paid the payout directly. The user wanted income to offset payouts directly, with the pool standing up only for the genuine shortfall.

**Trade-off / numerical impact:** retirement dates generally come **earlier** (less corpus pre-drain, less debt/hybrid tax drag, more stays in equity longer). Pools can be much smaller or empty whenever income covers payouts. Surplus income is **not** carried forward as cash to pre-fund future payout months directly — it goes to Core, and future shortfalls draw from Core via the pool refill (so surplus still helps later months, but through the taxed Core→pool path). Plans where income and payouts never overlap in a month (e.g. salary stops `At retirement` exactly as a `Replenishing` retirement-income goal begins) are **unchanged** — netting is a no-op there. When to revisit: if per-goal earmarking of specific income streams is ever needed, this aggregate-monthly rule is the thing to generalise.

---

## 2026-06-02 — Removed target-retirement-date mode; `find_retirement_date()` is solver-only

**What changed:** the `target_retirement_date` config key and the "test this one date" branch of `find_retirement_date()` are gone. The function now always binary-searches for the earliest feasible date. Its return dict shrank from `{mode, success, retirement_date, earliest_feasible, failure}` to `{success, retirement_date, failure}`. The Streamlit "📅 Target Retirement Date (optional)" section, the `target_retirement` arg to `build_config()`, and the `Mode` / `Earliest Feasible Date` rows on the advisor Simulation Result sheet were all removed accordingly.

**Why:** the user only wants the earliest-feasible answer; the target-date mode was extra surface area (a second code path, an extra UI section, and a `mode`-branching renderer) that wasn't being used.

**Trade-off / when to revisit:** there's no longer a way to ask "does *this specific* date work?" If that need returns, reintroduce it as a thin wrapper that calls `run_simulation()` once for the chosen date — but keep `find_retirement_date()` solver-only and don't resurrect the `mode` field on its return dict.

---

## 2026-06-02 — Income `amount` is now "as of start date", not today's rupees

**What changed:** an income stream's `amount` is interpreted as the monthly figure **on its `start_date`**, with step-ups accruing from the start date forward. Previously it was a PV in today's rupees, grown by step-ups from today to each date. Implemented by passing `stream_start` (not `current_date`) as the base reference into `amount_at_date_with_stepup()` in `calculate_income_cashflows()`.

**Why:** for a stream that begins years out (e.g. rent starting in 2035), "today's rupees" is unintuitive — the user wants to type what the income will actually be when it begins. Goals still follow PV-in/FV-out from today; income and windfalls are the two exceptions (see `CLAUDE.md` conventions).

**Trade-off / numerical impact:** for streams starting today nothing changes (base == today). Future-dated streams now start lower than before (no implicit growth from today → start), so plans relying on future income are more conservative. Past-dated streams use the real (un-clamped) start as the step-up base, so the amount has accrued step-ups by `current_date`.

---

## 2026-06-02 — Active + Passive income unified into one `income_streams` list; all income routes to Core Corpus

The two income concepts collapse into one. There is no Active/Passive distinction anymore — just income streams that differ only by when they stop.

**What changed:**

- Config keys `active_income_streams` and `passive_income_streams` are both gone, replaced by a single **`income_streams`** list. Stream shape: `{name, amount (PV), start_date, end_date_mode, end_date, step_up_percent, step_up_frequency, step_up_date}`. Passive's `growth_*` fields are renamed to `step_up_*` — one escalation concept. No migration path.
- `end_date_mode ∈ {Fixed, At retirement}` decides when a stream stops. **`At retirement`** → stops at the solver's retirement date (exclusive of the retirement month). **`Fixed`** → runs through `end_date` exactly (inclusive) and is **no longer truncated at retirement** — this removes the old "active income stops at retirement even if its end date is later" behaviour, which was the motivation for this change.
- `calculate_active_income_cashflows` + `calculate_passive_income_cashflows` → one `calculate_income_cashflows(config, retirement_date, simulation_end_date)` emitting a single `Income` column over the full sim horizon (not just to retirement, so Fixed streams can persist past retirement).
- **Routing unified to "all income into Core Corpus"** (the chosen rule). The pool no longer nets passive income against payouts: `simulate_pool` drops its `passive_income_df` parameter and sizes/withdraws on **gross** payouts; the surplus-passive-re-enters-Core path is deleted. `create_active_income_trans` → `create_core_corpus_trans`.
- Comprehensive view: `Active Income` + `Passive Income` columns → one `Income` column. Advisor export: `Active Income` + `Passive Income` sheets → one `Income Streams` sheet (adds End Mode / End Date columns).
- Solver short-circuit generalised: a single "retire now" feasibility check happens only when nothing is tied to retirement — i.e. no income stream is `At retirement` **and** no goal is `start_date_mode='At retirement'`. Otherwise binary-search up to `death_date`. Fixed income streams no longer bound the search (they're retirement-independent).

**Why:** Active vs Passive was a distinction in *principle* (work vs not) but the treatments diverged in ways the user didn't want — active was force-stopped at retirement, passive was netted in the pool. Unifying lets the user model any source identically and pick stop-behaviour per stream via `end_date_mode`. Routing-to-Core was chosen over pool-netting for simplicity (one code path) and because it also fixes a latent quirk: passive income used to be silently ignored when there were no Replenishing goals (the pool didn't run).

**Trade-off / numerical impact:** results change for any plan that previously used passive income. Income that used to directly offset payouts now earns Core/equity returns and is taxed as equity on pool refills, and the pool sizes on gross (not net) payouts → larger refills. More optimistic on returns, different on tax. Plans with a Fixed stream ending after retirement now keep that income post-retirement. These are intended consequences.

**When to revisit:** if a future requirement needs income that is *consumed as it arrives* (rent/pension that shouldn't earn equity returns), reintroduce a per-stream routing toggle rather than a separate Passive concept — the pool-netting machinery was removed but the git history (`simulate_pool` pre-2026-06-02) shows how it worked.

---

## 2026-05-26 — Inputs restructured: Active Income + Goals (advisor format); expenses folded into Replenishing goals

Major refactor. The simulator's input contract changed shape. The old config keys are no longer accepted; there's no migration path.

**What changed:**

- Removed `current_sip`, `yearly_sip_step_up_%`, `stepup_date_*`, `sip_adjustments`. Replaced with **`active_income_streams`** — a list of `{name, amount (PV), start_date, end_date, step_up_percent, step_up_frequency (Annual/Quarterly/Monthly), step_up_date}` records. Multiple overlapping streams allowed. Each stream is truncated at `min(end_date, retirement_date)`.
- Removed `expense_streams` entirely. Post-retirement living expenses are now modelled as a goal with `nature='Replenishing', structure='Recurring'` — same data shape as everything else. The retirement-income concept no longer exists in code.
- Removed `effects_on_cashflows`. Positive one-offs → **`windfalls`** (new, internal-only). Anything else (pre-existing loan EMIs, recurring obligations) → goals.
- Goals shape now matches the advisory team's `Advisory - Financial Planning Tracker.xlsx`: `{name, description, type, nature, structure, start_date_mode, start_date, amount (PV), frequency, occurrences, inflation_percent}`. `start_date_mode='At retirement'` links the goal's start to the solver's retirement variable.
- `find_retirement_date()` now returns a dict with `mode` (`'solver'` or `'target'`), `success`, `retirement_date`, `earliest_feasible`, `failure`. If `target_retirement_date` is set in config, the solver runs against that single date AND, on failure, also runs the binary search to suggest the earliest feasible date.
- `simulate_post_retirement()` is now `simulate_pool()`, driven by Replenishing payouts (summed across all Replenishing goals) net of passive income. The pool starts whenever the first Replenishing payout is due — no longer hardcoded to retirement.
- Passive income streams drop the pre/post-retirement growth split. One growth rate, one anchor, one frequency. Retirement-agnostic.
- A new `advisor_export.py` module produces a multi-sheet Excel that mirrors the advisor's Goals column layout exactly and adds Personal & Corpus, Active Income, Passive Income, Windfalls, Simulation Result, and Comprehensive Monthly sheets.

**Why:** the previous input model was structured around simulator internals (SIP vs expense vs effect), not around how a portfolio manager thinks about a client. The advisory team only tracks goals, and their existing Excel is the system of record. Aligning our inputs/outputs to that shape lets the tool be used as their front-end without translation.

**Trade-off:** any saved sessions from the old UI won't migrate. Anyone with notebooks / scripts calling the old API needs to rewrite. The advisor export is a one-way push for now — we don't yet ingest their Excel back as a config.

**When to revisit:** if the advisor file format changes columns or picklists, update `advisor_export.py` (column list at the top of the file) to match. If we ever want round-trip ingest from their Excel, add a reader on the same module.

---

## 2026-05-21 — UI and code defaults aligned for hybrid (12→10%) and debt (8→6%)

The Streamlit UI's "Configure Instrument Returns and Taxes" expander had been carrying different prefill values than the `find_retirement_date()` defaults in `main_v2.py:1300-1307`. Aligned in this direction: **UI is the source of truth** (the user interacts with it daily and just lowered the hybrid prefill to 10%), so `main_v2.py` code defaults were updated to match.

Aligned values (both UI prefill and code default):
- `hybrid` return: 10% (was 12% in code, 12% in UI before today's change)
- `debt` return: 6% (was 8% in code, 6% in UI — silent drift)
- `core_corpus` return: 12% (already matched)
- All STCG/LTCG: 20% / 12.5% (already matched)

`equity` (12%) and `cash` (4%) are still code-only; the UI does not surface them, so they're unchanged.

**Why:** silent drift between UI prefill and code default means a user running through Streamlit gets a different baseline than a developer running `python main_v2.py`. Worse, the `find_retirement_date()` defaults double as the smoke-test baseline — if they say 12% hybrid while the UI says 10%, the smoke test isn't checking what the user actually sees.

**Trade-off:** any future change to a UI prefill should be mirrored in `main_v2.py:1300-1307` (and vice versa). If the two should ever intentionally diverge, log the reason here.

**When to revisit:** if return assumptions change (e.g. revised debt-fund yields), update both sides together and log here.

---

## 2026-05-21 — Glide paths stay in tranche-and-chain format, not target-allocation

When updating from `Glide Paths v2.xlsx`, we re-authored the new glide path values into the existing tranche-and-chain row format rather than rewriting `calculate_goal_cashflows()` to consume a target-allocation table.

**Why:** the target-allocation format ("at year -N, hold X% in Debt, Y% in Hybrid, rest in Equity") is silent on (a) how many tranches to split the goal into, (b) when each tranche enters each bucket, (c) the funding-source chain. The tranche-chain format encodes all three explicitly. Translating one to the other requires modelling assumptions that should be made by the human, not the simulator.

**Trade-off:** authoring a glide path in the chain format is more verbose and requires the editor to think in tranches.

**When to revisit:** if the team starts to author glide paths primarily in the new format and the chain format becomes a translation layer, rewrite the simulator to consume target weights and a rebalancing schedule.

---

## 2026-04-01 — All `Date` columns standardised to `datetime64[ns]`

Pandas `merge_asof` raises a dtype-mismatch error when the left and right `Date` columns have different time resolutions (e.g. `[ns]` vs `[us]`). The default resolution can shift between pandas versions and even between input paths (Excel read, Timestamp construction, date_range).

**Why:** rather than fix this at every merge site, we normalise at construction. `_NS_DTYPE = "datetime64[ns]"` and `_ensure_date_ns(df)` in `main_v2.py:11-17` are the convention. Streamlit Cloud independently hit the same issue, so `requirements.txt` pins `pandas>=3.0.0` to keep behaviour consistent across local + cloud.

**Trade-off:** any new code path that creates a DataFrame with a `Date` column must remember to call `_ensure_date_ns()` (or construct via `_ts()`). Forgetting it surfaces as `MergeError` deep inside the simulator.

**When to revisit:** if pandas ever fully unifies datetime arithmetic across resolutions, this convention can be relaxed.

---

## 2026-03-18 — Default `target_lifetime` lowered from 100 to 90

Changed in commit `d34bb03`. The previous default of 100 made the simulator size post-retirement pools (and Core Corpus runway) for a much longer tail than most users actually plan for, inflating the required retirement corpus and pushing the discovered retirement date later.

**Why:** 90 is a more representative planning horizon.

**Trade-off:** users planning for longevity-tail scenarios must explicitly raise the input. The UI surfaces this as a configurable field.

**When to revisit:** if users systematically ask for longer horizons, raise the default.

---

## 2026-02-23 — STCG / LTCG replaces flat per-bucket tax rate

Commit `b121263`. Each instrument bucket now carries `stcg_tax` and `ltcg_tax` rather than a single rate. Tax is determined per tax-lot at redemption based on holding period (≤ 365 days → STCG, > 365 → LTCG).

**Why:** the previous flat-rate model materially overstated tax on long-held core corpus lots and understated tax on short-term debt-pool churn. STCG/LTCG split mirrors Indian capital-gains rules and produces accurate per-redemption tax.

**Trade-off:** FIFO tax-lot accounting is more code (the `TaxLot` / `InvestmentPool` classes and the lot-walking logic in `add_withdrawls_to_trans()`). Worth it for accuracy.

**When to revisit:** if Indian tax rules change (e.g. revised LTCG rate, removal of indexation), update the per-bucket `stcg_tax` / `ltcg_tax` defaults in `find_retirement_date()` (`main_v2.py:1300-1307`) and log it here.

---

## 2026-02-22 — Removed 5-year-beyond-death post-retirement pool buffer

Commit `f6d83c0` (revert of `3ab82d9`). The simulator previously pre-funded post-retirement pools to 5 years beyond the death date as a conservative buffer; this was removed alongside the switch to showing total wealth (rather than core corpus only) on the chart.

**Why:** pools are now sized exactly to the death date. The "buffer" was hiding the genuine question of "does the corpus actually last?" by reserving extra capital.

**Trade-off:** the model treats the death date as a hard endpoint with no margin. Users who want a margin should raise `target_lifetime`.

---

## Open / pending decisions

(None tracked here yet. Add a stub entry the moment a decision is "we'll think about this later" so it doesn't get lost.)
