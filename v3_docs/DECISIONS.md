# DECISIONS — append-only log of non-obvious modelling and structural choices

Read before changing existing methodology. New entries at the top. Each entry: ISO date • short title • rationale • trade-off / when to revisit.

This file is seeded from the commit history that's actually in the repo today (2026-05-21). Going forward, append a new entry whenever a structural or modelling change goes in — the binary nature of `Glide Paths.xlsx` and the implicit nature of "why we chose this default" mean git log alone won't explain things later.

---

## 2026-08-31 - CRM goals contract v2, and the income flag it rescued

**What changed:** Punit's v2 spec, built and shipped the same day.

- `purpose_id` REMOVED. The upload is strictly ADD - the CM appends goals to
  the client's CRM list, which is the source of truth for edits and
  cancellations - so nothing in the file addresses an existing goal and an id
  has no job. Dropped from the export, the loader, the form state and the
  goal card.
- `lifetime` ADDED (boolean, null when `occurrences == 1`).
- `payments_fixed_at_start` ADDED (boolean, null when `occurrences == 1`).
  They store and return it verbatim; we re-derive from live policy on load, so
  a stale row cannot override the current rule.
- `occurrences` now always the TRUE simulated count. The `CRM_OPEN_ENDED_
  OCCURRENCES = 500` sentinel is retired.
- `every_other_year` gone from their enum too. Our loader still refuses any
  frequency it cannot represent - defensive, since the silent alternative is
  `normalise_goal`'s "Monthly" default over-funding a goal.

**Why the sentinel had to go - Punit's catch:** v1 inferred "unbounded" from
`occurrences == 500`. On a real plan (Aman Gupta) the income resolved to 397
occurrences, missed the sentinel, and would have been read back as
contract-fixed - silently stopping that client's retirement income from
tracking inflation. The magic number was deciding a forty-year funding
question.

**The bug his catch exposed on OUR side (worse, and live):** the same loss
existed in our own save format, independent of the CRM.
`build_inputs_json(retirement_date=...)` resolves Lifetime into a concrete
count and "At retirement" into a date - erasing BOTH signals
`payments_fixed_for` reads. Reloading a resolved export therefore turned a
retirement income into a contract-fixed series that stopped escalating.
Reproduced end to end before fixing. Reachable via the resolved download, not
via version history (which stores the as-entered shape). Our inputs JSON now
carries `lifetime` explicitly and the loader restores the end mode from it;
both round trips are pinned by tests.

**Punit's other decisions, accepted as his call:** no client envelope in the
file (wrong-file risk handled by CM process on their side); no
`source_simulation_id`.

**Operational note worth watching:** ADD-only plus no ids means a re-upload
after editing a plan APPENDS a second copy of every goal rather than updating
the first. Flagged to Punit; their UI is where edits are meant to happen.

**Tests:** `test_crm_contract.py` (19) pins the twelve keys in order, the
null rules, both booleans, the true-count guarantee, both round trips, and
the frequency refusal.

## 2026-08-30 - CRM goals contract: flat rows, strict tokens, `purpose_id`

**What changed:** new export `crm_goals_upload_json()` producing the CRM's
goals upload file to Punit's spec (email 2026-08-30): strictly
`{"goals": [...]}`, eleven keys on every goal, their vocabulary, dates
resolved. Goals gained two fields - `purpose_id` (CRM-minted identity, null
until first upload) and `goal_category` (their ten-value taxonomy, a picker
on the goal card). The loader now also accepts their FLAT goal rows, which is
how minted ids come back to us. Our own inputs JSON is unchanged - it is our
save format, not theirs.

**Their contract, in their words:** nothing is interpreted on their side - a
missing key, an unknown key, a null where one is not allowed, or a token in
the wrong case rejects that goal. So `test_crm_contract.py` pins the file key
by key and token by token; a drift is a silent rejection for a CM.

**Two judgement calls, both flagged back to Punit:**

1. `occurrences: 500` is their marker for "open-ended (lifetime /
   at-retirement)". We apply it ONLY to a series whose length is genuinely
   unstated (our Lifetime end mode). A series that merely starts at
   retirement but runs a stated 240 payments keeps 240 - writing 500 would
   misstate a real number, and its start date is resolved regardless.
2. That 500 is load-bearing in the OTHER direction. Their contract drops
   `end_mode` and `start_date_mode` - exactly what `payments_fixed_for()`
   reads - so after a CRM round trip the only surviving signal that a goal is
   income is `occurrences == 500`. The loader maps it back to Lifetime, which
   restores the policy; without it a retirement income would silently stop
   escalating. Safe in practice (500 monthly = 41 years, past any real EMI)
   but it means a genuine 500-payment fixed series would be misread. Noted to
   Punit; the clean fix if it ever bites is a flag they store.

**Not sent:** `payments_fixed_at_start` (stays derived our side, per their
note), and `nature` / `structure` / `end_mode` / `start_date_mode` (no longer
accepted).

**Operational note:** the upload file requires a solved retirement date, so
it is offered only on a successful run - "at retirement" has no
representation in their contract.

## 2026-08-27 - Fixed-vs-inflating payments is POLICY, not a CM choice

**What changed:** the `payments_fixed_at_start` checkbox (shipped 2026-08-25,
live for two days) is REMOVED. The app now derives the treatment from the
goal's shape - `payments_fixed_for()` in streamlit_app.py, the single source:

> Every recurring goal is contract-fixed (escalate only to the first payment)
> EXCEPT income-like series, which keep escalating throughout. "Income" is
> structural, never a name: the goal starts At retirement, or its payments
> run for Lifetime.

The goal card states which rule applies (a lock / trend caption on every
recurring goal), so the CM always sees the treatment - they just cannot
change it. The engine is untouched: it still honours the explicit flag;
`build_config` now sets it from policy. The CRM export still carries the
resolved value per goal.

**Why (operator, 2026-08-27):** the boss's call - this is a modelling truth,
not a preference. Education installments do not rise mid-course any more than
an EMI does; giving CMs a checkbox meant every plan's correctness depended on
someone remembering to tick it, and old plans loaded with it silently off.
Deriving it applies the correct treatment to every plan - new, loaded, or
replayed - with zero CM action.

**Consequence for old plans - measured (311-plan replay, before/after):**
- 14 plans flip infeasible -> success (Anmol Agrawal x4, Prasad N x2,
  Mahesh Gunturu x2, Jaywant x2, R Jagannath, Playground, +2 more).
  Prasad N lands back at "retire immediately" - his six education/marriage
  goals were the over-provisioned kind.
- Of 245 plans feasible in both: 109 unchanged, 136 EARLIER, 0 later.
  Median -1 month, mean -8.6; the education/EMI-heavy tail is enormous -
  Balla Pavan -160 months (13+ years), Ajit Gaginer -109, several -60s.
  Compounding an 8-10% education rate across a whole payment series was
  that much phantom cost.
- Target-date plans: SIP-needed falls Rs ~48k/month median where it moves.
- Direction is one-sided by construction: fixing payments can only reduce a
  goal's cost. Income goals (Lifetime / At-retirement) are untouched.

**Escape hatch, deliberate:** a genuinely escalating non-income series (rent
with a contractual escalation clause) currently has no way to opt back in.
Accepted: no client plan has needed one; if one does, that is the two-rate
extension flagged in the +fixedstart entry, not a return of the checkbox.

## 2026-08-25 - Contract-fixed payments: escalate to the FIRST payment only (`+fixedstart`)

**What changed:** recurring goals gained `payments_fixed_at_start` (bool,
default off). On: the amount escalates at the goal's growth % from today to
the FIRST payment, then every payment equals the first. Off (and absent):
every payment escalates to its own date, exactly as before. One change point -
`expand_recurring_goal_to_tranches` - everything downstream (grid carving,
taxes, netting, settlement) just consumes dated amounts. Engine stamp bumped
to `v2grid+goaltaxequity+fixedstart`.

**Why:** an EMI is signed and college fees lock at admission - the payments
are contractually fixed, but the engine escalated every one to its own date.
A Rs 50k/month EMI starting in 3 years at 6% was funded as if the 240th
payment were Rs 1.90L - an 88.9% overstatement of the whole goal. The only
workaround was entering 0% growth with a hand-computed future amount (seen in
the wild: Pankaj Bhatia's 0%-growth education goals).

**Scope (operator decisions, 2026-08-25):**
- EMIs / loan repayments, term-insurance premiums, any contractual fixed
  outflow -> fixed. **Education too**: fees inflate until admission, but the
  installments of one course stay fixed (the boss's explicit call, overriding
  my earlier lean).
- Retirement income and other cost-of-living series -> keep escalating.
- Property of the GOAL, not the category -> it is asked, not derived: a
  checkbox on recurring goals. Templates encode the policy: Child Education
  arrives ticked, Retirement Income unticked.
- An already-running EMI needs no special case: zero pre-start window means
  flat at today's amount.

**Default off is deliberate** - it is today's behaviour, so every saved plan
replays identically (the parity golden masters double as the back-compat
proof; no re-baseline). A CM forgetting to tick overfunds (conservative); the
opposite default would let a forgotten untick underfund a retirement income
silently (optimistic - the dangerous direction, per the goaldedupe lesson).

**Divergence from the goal doc:** SS4.2 says every occurrence escalates to its
own date. `goal_grid.py` stays doc-literal so Punit's worked tables keep
reproducing; the flag lives in the engine only. Punit informed (the CRM goals
contract gains the field; his ingestion should carry it alongside the goal
IDs he mints).

**Tests:** TestPaymentsFixedAtStart - flag-on flatness + exact first-payment
amount, flag-off monotone growth, absent==off equality, end-to-end funding
drop (>40% on the 20-year EMI), already-running EMI flat at today's amount.

**Revisit when:** a client needs different growth rates before vs during the
series (rent: CPI till start, contractual escalation after). That is a second
rate field, not a flag - deliberately not built until asked for.

## 2026-08-25 - Wealth figures double-counted every rupee held for a goal

**The bug:** the comprehensive view reports debt and hybrid TWICE by design -
once as a total (`Debt Pool Value`, `Hybrid Pool Value`) and once broken down
per goal (`<goal> Debt Value`, `<goal> Hybrid Value`). Four consumers summed
every column ending in "Value", so all goal money was counted twice:

- `build_snapshot` -> the "Wealth at retirement" metric, the Excel summary's
  wealth split, and `snapshot_at_retirement` in the logged output JSON
- `wealth_frame` -> the wealth chart AND the "Wealth at lifetime end" metric
- `csv_with_summary` -> the CSV's "Total Wealth (Rs)" column

**Why it appeared now:** in v1 those two column families were genuinely
different money - the shared Debt/Hybrid pools funded Replenishing goals, and
the per-goal columns held Non-replenishing glide-path chains. Nothing
overlapped, so summing everything was correct. v2 made `Debt Pool Value` the
total across all goals and kept the per-goal columns as its breakdown, which
silently turned a correct sum into a double count. The engine's own maths was
never wrong - only the reporting on top of it.

**Caught by** the operator, from a live run (SIM-20260825-124919-119153): a
client with a Rs 6.70 Cr corpus retiring in the CURRENT month was shown
"Wealth at retirement Rs 7.20 Cr". With no income streams and no time to
compound, wealth cannot rise Rs 50L in the retirement month - the gap was
exactly the goal sleeves counted a second time.

**Fix:** totals come from the three aggregate columns only
(`_POOL_VALUE_COLS`). The per-goal sums stay in the snapshot dict and the
Excel sheet, relabelled as "of which, by goal" so they read as the breakdown
they are. Corrected figure for that client: Rs 6.76 Cr (corpus plus one month
of growth).

**Regression test:** `TestComprehensiveViewIsNotDoubleCounted` pins both
halves - the aggregate column must equal the sum of the per-goal columns, and
month-one total wealth must sit within 2% of the opening corpus. It also
asserts that the naive "sum every ...Value column" figure is materially
larger, so the test fails if anyone reintroduces that sum.

**Revisit when:** a bucket ever holds money that is NOT attributable to a
goal. Then the aggregate stops equalling the per-goal sum, and the difference
becomes a real number worth reporting on its own.

## 2026-08-24 - v2: the GOAL GRID replaces glide-path chains and shared pools

**What changed:** the goal model itself, per Punit's "4. Goal Planning" (§4.1-4.5)
and "Goal Algo" docs. Engine stamp is now `v2grid+goaltaxequity` - the v1
lineage string is retired (git history holds it).

- **One grid, read per cashflow.** A goal is a series of cashflows; each is
  looked up by whole years-to-cashflow (`t = days / 365.25`, load-bearing) and
  negotiability, giving a Debt/Hybrid share of that cashflow's future value.
  Glide-path chain scripts (`calculate_goal_cashflows`) and the shared
  Debt/Hybrid pools (`simulate_pool`, `calculate_debt_injection_need`,
  `compute_replenishing_payouts`) are DELETED, along with `POOL_PREFUND_MONTHS`
  (+poolprefund, 2026-08-11) which existed only to soften the pools' cliff.
- **Per-column reach**: non-negotiable pre-funds 5 years out, semi-negotiable
  4, negotiable 3. Beyond its reach a cashflow carves nothing - it is Core's
  job until a later month brings it into range.
- **Purpose is DERIVED, never typed.** "Does this goal have cashflows outside
  the carve window?" -> `GOAL_REPLENISH` / `GOAL_NON_REPLENISH`. The `nature`
  input is gone from the form; saved files that carry one still load and their
  stored value is ignored. The 48-month non-replenishing span cap
  (D-P208-1) is retired with the chains - it was a chain-count performance
  guard, and long recurring goals are exactly what the window handles.
- **Sizing is PATH-CONSISTENT, not doc-literal** (operator decision, this
  date). The doc discounts each sleeve at its own growth for the whole
  distance; but the grid MOVES money hybrid->debt as the goal nears, and that
  switch is taxed. So each slice is back-solved leg by leg along the route it
  actually travels, net of tax at every hop. On the worked example the
  doc-literal formula understates by ~9.9% with tax. `goal_grid.invest_today`
  keeps the doc-literal behaviour as the golden reference against Punit's
  worked tables (36 tests); `grid_engine.slice_principal` is what the
  simulator uses.
- **Dynamics** (the four operator answers, this date): Core is re-read
  **monthly**, not annually; tax applies to both sizing and movement; failure
  means **all pools depleted**; "Goal Algo" §6 ignored.
- **Failure semantics.** A month where Core cannot fund a provisioning event
  is NOT a failure - the slice stays pending and re-sizes every later month.
  A due cashflow drains, in order: its own debt sleeves, its own hybrid
  sleeves, Core, then every other goal's sleeves in REVERSE priority (so a
  negotiable goal's money is taken before a non-negotiable goal goes short).
  Only after all of that is the plan infeasible.
- **Sequential funding order** (Goal Algo step 4): negotiability, then
  earliest cashflow, then larger first. Income nets against due cashflows in
  that same order, untaxed, before anything is provisioned.

**Output contract unchanged.** `run_simulation` still returns
`(success, final_trans_df, failure_details, pool_movements_df, goal_dfs,
comprehensive_df)`; `goal_dfs` still uses the chain-table column shape (every
slice IS a chain: Core -> bucket [-> bucket] -> goal), and the comprehensive
view keeps its column names. So the advisor workbook, the CSV, the Excel
summary, the CRM export, and the version log all read v2 unchanged.

**Impact (replay of all 311 unique logged plans, v1 -> v2):**

- **The headline: v1 declared 28 plans dead in their FIRST MONTH. v2 fails
  none of them there.** 2026 was v1's single largest failure bucket - larger
  than any other year - and it was an artifact: a Non-replenishing goal's
  chain inflow dates are clamped to `current_date`, so a goal two or three
  years out demanded its ENTIRE provisioning on day one. (This is the same
  cliff `+poolprefund` fixed for Replenishing goals in 2026-08-11; the chains
  were never fixed. The grid removes it structurally for both.) Of those 28:
  10 are now fully feasible, and all 18 that still fail now fail at a REAL
  date, 2 to 13 years out - which is what a CM can actually act on.
- 19 verdict changes overall: 14 infeasible -> success, 3 formerly `invalid`
  (the retired span cap) now solve or fail honestly, and 2 success ->
  infeasible (Prasad N v1/v9 - a no-income plan funding six education and
  marriage goals off Rs 1.55 Cr; it runs dry at the last goal in Jan 2041.
  Note its ORIGINAL logged verdict was also infeasible, so v2 restores what
  the CM first saw; the 2026-08-24 tax change had briefly flipped it).
- 227 plans feasible in both: 64 unchanged, 139 later (median +3 months, max
  +20), 24 earlier. Later is the expected direction and the intended cost:
  the grid parks goal money in debt/hybrid (6%/10%) up to five years ahead of
  each cashflow instead of leaving it compounding in Core (12%), and it taxes
  every hop that money actually takes.
- 3 target-date plans: SIP-needed rises in all 3 (median +Rs 26,000/month),
  same mechanism.

**CMs must re-run their clients.** Every logged plan predates this change.

**Golden masters re-baselined** with full delta attribution (parity config 1
2032-09 -> 2033-01, config 2 2028-02 -> 2028-05; transaction counts rise from
tens to thousands because per-cashflow carves replace annual pool refills).

**Revisit when:** Punit's grid percentages or per-column reach change (both are
data - `goal_grid.GOAL_GRID` / `GOAL_REACH_YEARS`), or if the desk decides the
doc-literal sizing should be restored (swap `slice_principal` for
`goal_grid.invest_today_taxed`; Punit's worked tables would then reproduce
exactly, at ~9.9% lighter funding).

## 2026-08-24 - Goal money uniformly EQUITY-taxed + LTCG year-boundary grace (`+goaltaxequity`)

**What changed:** Two coupled taxation decisions from the advisory desk
(relayed by Shresth, 2026-08-24). Engine stamp bumped to
`1515f1e+pool2x2+lifetimefix+monthgrid+poolprefund+goaldedupe+goaltaxequity`.

1. **All goal buckets are equity-taxed.** The "debt" sleeve for goal money is
   implemented with ARBITRAGE funds - debt-like return (6% assumption kept),
   equity taxation - and the hybrid funds offered are equity-taxed. So every
   redemption of goal money taxes at 20% STCG / 12.5% LTCG. The engine's debt
   bucket is only ever goal money (chains + pools), so this is simply the
   uniform rule. (An intermediate proposal to slab-tax debt at 30% was
   considered the same week and dropped once the arbitrage-fund implementation
   was confirmed - it never shipped.)

2. **Year-boundary grace (`LTCG_GRACE_DAYS = 2`).** A redemption within 2 days
   of completing one year is taxed as LTCG: operationally the desk shifts the
   redemption 1-2 days to cross the year, so a lot held >= 364 days pays 12.5%,
   never 20%. Applied at all three tax-decision sites: FIFO lot redemptions
   (`InvestmentPool._get_tax_rate`), chain-leg sizing
   (`calculate_goal_cashflows`), and pool injection sizing
   (`calculate_debt_injection_need`).

**Why it matters more than "edge case":** glide-path chains hop annually and
pools refill annually, and an annual hop in a non-leap year spans exactly 365
days - which the old `<= 365` rule taxed at STCG 20%. The grace flips every
such hop to 12.5%. It also removes a latent asymmetry where the same 1-year hop
was STCG in a non-leap year but LTCG across a leap day. The motivating case:
negotiable goals move money into hybrid ~1 year before the goal; held ~365
days, it now correctly pays 12.5%.

**Impact (replay of all 307 unique logged plans, before/after):**
- 1 verdict flip, favorable: Prasad N v1 infeasible ("fails Jan 2040") ->
  success (retire Aug 2026). A marginal plan whose late-life depletion the
  tax saving compounds away.
- 227 plans feasible in both: 212 retirement dates unchanged, 15 EARLIER by
  1 month, 0 later. Median shift 0, mean -0.1 months.
- 3 target-date plans: SIP-needed LOWER in all 3 (median -Rs 500/month).
- 73 infeasible, 3 invalid, 3 target-infeasible: unchanged.
- No plan got worse - the change is mathematically one-directional (tax rates
  only ever fall or stay).

**Golden masters re-baselined** (test_planning_engine.py, delta attribution in
the docstrings): both parity retirement dates unchanged; funding cheaper by
0.036% / 0.018%; config 1 loses its final Sep-2085 debt-pool refill event
(pools slightly richer -> last top-up rounds to zero). The
`test_tax_rate_by_holding_period` unit test now pins the 363/364-day boundary.

**Where it surfaces in the UI:** the sidebar "Model assumptions - returns &
taxation" expander states both rules (reads live from
`_DEFAULT_INSTRUMENT_PARAMS`, so it cannot drift from the engine).

**Revisit when:** the desk stops using arbitrage funds for the debt sleeve
(taxation would need to be re-split per bucket), or equity STCG/LTCG rates
change in a Finance Act.

## 2026-08-20 — Duplicate goal names no longer silently drop a goal (`+goaldedupe`)

**What changed:** `run_simulation` disambiguates non-replenishing goal names
before they become `goal_dfs` keys. The FIRST goal of a given name keeps that
name exactly; a second goal with the same name becomes `<name> #2`, a third
`<name> #3`. Engine stamp bumped to
`1515f1e+pool2x2+lifetimefix+monthgrid+poolprefund+goaldedupe`.

**The bug:** `goal_dfs` is a plain dict keyed by the goal's (user-supplied) name
— `engine.py`, "1. Non-replenishing goals -> chain math". Two goals with the
same name, and for Recurring goals the same occurrence count, produced identical
keys, so the later goal **silently overwrote** the earlier one. The overwritten
goal was then never provisioned, never withdrawn for, and absent from every
output (comprehensive view columns, per-goal Excel sheets, action plan) — while
the plan still reported SUCCESS at an impossibly early retirement date. Nothing
warned.

This is not exotic: two children's education goals are routinely both called
"Child Education", and the app's own goal templates always insert the same
default name, so the collision is the *expected* outcome of normal CM behaviour.

**Discovered** from a real run (Gajender Patel v4, 2026-08-20): two "Child
Education" goals, one starting 2032 and one 2040. The CM asked why the 2032
goal's money never appeared. Reproduced: the 2032 goal was absent — 4 tranche
chains instead of 8, Rs 89.5L of chain funding instead of Rs 1.20 Cr, and the
plan reported **Oct 2040** when the true answer with both goals funded is
**Aug 2045** — five years optimistic.

**Blast radius (logged corpus, 322 rows):** 34 runs across 5 clients had at
least one silently dropped goal — Anmol Agrawal, Laxmidhar Souche, Gajender
Patel, Ruchi Takkar (+ one Playground row). Those plans must be re-run; their
results were materially wrong, always in the optimistic direction.

**Trade-off:** plans with duplicate goal names now provision every goal, so
their retirement dates move LATER (they were previously funding fewer goals
than the CM entered). Plans with unique goal names are byte-identical to before
— the first goal of a name is untouched, so no existing correct output changes.
Outputs for duplicated names now show `<name> #2`, which is visible in the
Excel per-goal sheets and CSV columns.

**When to revisit:** if goals ever gain a stable id, key `goal_dfs` by that id
and use the name for display only; the name would then never need suffixing.

---

## 2026-08-11 — Replenishing pools now PRE-FUND before the first payout (`+poolprefund`)

**What changed:** `run_simulation` starts the Debt/Hybrid pool simulation up to
`POOL_PREFUND_MONTHS` (48) before the first *net* payout instead of on the payout
date itself:

```
old: pool_start = min(first_net_payout, retirement_date)
new: pool_start = max(current_date,
                      min(first_net_payout - 48 months, retirement_date))
```

Nothing else moved — no new provisioning maths. The existing annual-cycle
lookahead windows (Debt = next 24 months, Hybrid = months 25–48) turn the earlier
start into a graduated ramp on their own. Engine stamp bumped to
`1515f1e+pool2x2+lifetimefix+monthgrid+poolprefund`.

**Why:** the old rule produced a **provisioning cliff**. For the common case
(payouts starting at/before retirement — i.e. every retirement-income goal) the
pool did nothing at all beforehand and then demanded the pools' entire 48-month
horizon from the Core Corpus in the single month the payouts began. Measured on a
₹1L/month payout stream: ₹20.6L into Debt + ₹16.8L into Hybrid in one month, with
zero movements before it.

Two things made this a defect rather than a policy:

1. **It contradicted the model's own philosophy.** Non-replenishing goals de-risk
   gradually via glide paths (Non-Negotiable starts 5 years out in four annual
   slices). Replenishing goals — the same underlying need, money that must be safe
   when spent — did it all in one day. In advice terms the old behaviour meant
   "stay fully in equity until the day income starts, then liquidate four years of
   spending at once", concentrating sequence-of-returns risk on a single date,
   which is exactly what the Debt/Hybrid buckets exist to prevent.
2. **The engine already pre-funded — inconsistently.** When a goal starts *after*
   retirement, `min()` returned the retirement date and the pool ramped smoothly
   (measured: first inflow 3 years ahead, then annual top-ups). The cliff was an
   artifact of that `min()`, not a deliberate rule.

Confirmed as unintended with Punit (model owner), who also confirmed the
playground is now the **only** financial-plan build — the CRM no longer runs one —
so there is no divergence to manage.

**Trade-off / numerical impact:** money leaves the Core Corpus earlier, so it
compounds less: strictly-feasible plans can see slightly later retirement dates
and lower terminal wealth. Against that, the single-month cliff disappears, so
plans that failed only because the corpus was momentarily short at the first
payout can now pass. The direction is plan-specific, not uniformly favourable, and
every plan containing a Replenishing goal moves at least slightly. A
baseline-vs-patched replay over the full logged-run corpus accompanies this
change.

**Not fixed by this:** a goal whose payouts begin so soon after the plan start
that there is no runway to ramp into (e.g. first payout 8 months in) still
provisions in one lump — `pool_start` is floored at `current_date`. That is
genuine underfunding, not a modelling artifact.

**When to revisit:** if the ramp should be *proportional* (e.g. an explicit
25%-per-year schedule like the glide-path sheets) rather than "whatever the 24/48
month lookahead windows imply", that is a larger change to `simulate_pool`'s
targeting logic and should be specced separately.

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
