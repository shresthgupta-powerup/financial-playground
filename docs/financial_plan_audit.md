# Financial-Planning Launch Accuracy Audit (Plan 207)

Pre-launch accuracy audit of the financial-planning tool (LP-015, live in prod).
Inputs: the advisory team's real-client tracker
`Advisory - Financial Planning Tracker (3).xlsx` (2026-06-11 export; 31 clients,
66 real goal rows). Audit plan: `.context/TODO.md` Plan 207 (P-712). Engine
version under audit: `1515f1e+pool2x2`, glide-path version 1. (Engine later bumped to
`1515f1e+pool2x2+lifetimefix` by Plan 222 — pool death-date provisioning fix; audit
findings unchanged.)

**Status: COMPLETE** — all 4 legs audited; verdict SHIP (no calculation defect found).

---

## Leg 1 — Tracker→engine mapping + corpus (Ph1)

**Verdict: PASS with data-quality items for the advisory team (below).**

The audit corpus is generated mechanically from the tracker by
`scripts/gen_advisory_corpus.py` → `backend/tests/advisory_corpus.py`
(25 clients / 58 goals after exclusions; 3 synthesized corpus/stream profiles
per client = 75 configs). All 75 configs pass `validate_plan_config`. Mapping
rules + exclusion policy are documented in the generator header (D-P207-2/3/4).

### Data-quality catalog — rows excluded from the audit corpus

These rows cannot be represented in (or were flagged invalid by) the tracker
itself. **For the advisory team to fix at source.**

| Goal ID | Goal | Reason excluded |
|---|---|---|
| CUST001_G01 | Retirement income | Template client (also carries a `#REF!` end date) |
| 108_M3_G01 | Son's Wedding | Goal_status = Cancelled |
| 108_M3_G03 | Retirement Income | Goal_status = Cancelled |
| 125_M3_G02 | Financial Freedom | Tracker ERROR: Pick a Goal_structure (also no amount) |
| 139_M3_G05 | Financial Freedom | Tracker ERROR: Recurring needs per-occurrence, frequency, occurrences (also no amount) |
| 135_M3_G02 | Financial Freedom | Tracker ERROR: Pick a Goal_structure (also no amount) |
| 140_M3_G01 | Child UG | Tracker ERROR: Lumpsum should not have recurring fields (client 140_M3 drops out of corpus entirely) |
| 140_M3_G02 | Child PG | Tracker ERROR: Lumpsum should not have recurring fields |

Note: the three "Financial Freedom" rows (125/139/135) look like the same
half-filled pattern — a goal type the tracker template doesn't cleanly support.
Worth a picklist/template fix.

### End-date vs occurrences span mismatches (D-P207-3 — occurrences win)

The engine's `end_mode` is exclusive; where the tracker sets both
`Goal_occurrences` and `Goal_end_date`, occurrences were used and the implied
span cross-checked. 11 rows disagree by more than one frequency period:

| Goal ID | Goal | Occurrences say | Tracker end date |
|---|---|---|---|
| 114_M3_G01 | Daughter's Education UG | 4×Annual from 2030-03 → ~2033-03 | 2034-12-31 |
| 114_M3_G02 | Daughter's Education PG | 2×Annual from 2034-03 → ~2035-03 | 2036-12-31 |
| 114_M3_G03 | Son's Education UG | 4×Annual from 2036-03 → ~2039-03 | 2040-12-31 |
| 114_M3_G05 | Son's Education PG | 4×Annual from 2040-03 → ~2043-03 | 2044-12-31 |
| 114_M3_G07 | Travel | 7×Annual from 2030-01 → ~2036-01 | **2050-12-31** |
| 123_M3_G02 | Son's Education | 4×Annual from 2043-03 → ~2046-02 | 2047-12-31 |
| 150_M3_G01 | Child 1 UG | 3×Annual from 2037-01 → ~2039-01 | 2040-12-31 |
| 150_M3_G02 | Child 1 PG | 2×Annual from 2040-01 → ~2040-12 | 2042-12-31 |
| 150_M3_G03 | Child 2 UG | 3×Annual from 2042-01 → ~2044-01 | 2045-12-31 |
| 150_M3_G04 | Child 2 PG | 2×Annual from 2046-01 → ~2047-01 | 2048-12-31 |
| 151_M3_G01 | Child UG Education | 4×Annual from 2030-01 → ~2032-12 | 2034-12-31 |

Most look like the advisor recording "end of the education" (or rounding to
Dec-31) rather than the last payment date — harmless if occurrences are the
intent. **114_M3_G07 (Travel: 7 occurrences vs a 21-year window) is a real
contradiction** — either the count or the end date is wrong; the plan differs
materially depending on which.

### Mapping decisions of record

- Tracker stores goal growth in `Requirement_escalation_pct` (education rows
  leave `Inflation_assumption_pct` empty). Engine has a single
  `inflation_percent` doing double duty (PV→FV + per-occurrence escalation —
  SIMULATION_MODEL.md § Goals). Mapping: escalation first, inflation fallback,
  6% default. Verified: the two columns never both-set-and-differ in this
  export, so the collapse is lossless **for current data**. If advisory ever
  intends them to differ, the engine cannot represent that — flag at intake.
- Tracker percentages are fractions (0.07); engine takes percent (7.0).
- `Goal_type` casing normalized ("Non-negotiable" → "Non-Negotiable").

## Leg 2 — Corpus-wide invariant sweep (Ph2)

**Verdict: PASS — 0 invariant violations, 0 crashes across all 75 configs.**

Seven invariants implemented in `backend/tests/test_planning_invariants.py`
(permanent regression gate on a representative subset; full sweep run once for
this audit, 291s):

| Invariant | What it asserts | Result |
|---|---|---|
| I1 Payout-split identity | gross Replenishing payouts == investment-used + pool-funded, every month | PASS (all 50 feasible runs) |
| I2 Non-negative balances | Core / Debt pool / Hybrid pool / goal-tranche values never negative | PASS |
| I3 Withdrawal completeness | success=True ⇒ every withdrawal fully funded, zero shortfall | PASS |
| I4 Goal-chain delivery | every Non-replenishing tranche's chain delivers exactly the tranche FV | PASS |
| I5 Solver minimality | the found retirement date succeeds AND one month earlier fails | PASS |
| I6 Conservation of money | zero-return/zero-tax: Δwealth == investment + one-time − goal payments − net payouts, exactly (±₹2 on crore-scale flows) | PASS (all 25 clients) |
| I7 Snapshot consistency | service snapshot == comprehensive view == wealth_monthly row | PASS |

Sweep outcome over 25 clients × 3 synthesized profiles: **50 feasible, 25
infeasible, 0 violations, 0 errors.** Every infeasible case is economically
sensible (lean/moderate synthetic profiles against large near-term goals) and
produces a coherent failure diagnostic (the goal + date that broke the plan) —
no crash, no silent wrong answer. One marginal case of note: 108_M3 is
infeasible even at the wealthy profile because its single surviving goal is due
immediately (start = current date), so corpus == goal PV is exactly at the
boundary — correct behavior, an artifact of the synthesized profile.

## Leg 3 — Independent recomputation from the model doc (Ph3)

**Verdict: PASS — engine matches the documented model on every hand-computed
case, first run, no adjustments.**

`backend/tests/test_planning_oracle.py` (permanent): every expected value
computed by hand from SIMULATION_MODEL.md formulas, never from engine code:

| Oracle | Case | Expected (hand) | Engine |
|---|---|---|---|
| O1 | Lumpsum FV, 1,000,000 @ 7% over exactly 4.0 years | 1,310,796.01 | match |
| O2 | Recurring annual escalation, 3 occurrences @ 6% | 12,624.77 / 13,381.72 / 14,186.32 | match |
| O3 | FIFO tax lots: LTCG net back-solve / STCG net back-solve / gross FIFO order | units 500, tax 500 / tax 1,000 / tax 3,000, lot B untouched | match |
| O4 | Post-tax corpus back-solve P = E/((1+r)^t(1−τ)+τ) | 100,000 (τ=0) / 109,502.26 (τ=50%) | match |
| O5 | Pool 2+2 windows (zero-return): payouts at months 12/30/47/49 | cycle-1 refills: Debt 120,000, Hybrid 270,000; month-49 excluded | match |
| O6 | Annual step-up calendar: 100,000/mo, 10%, 2 years to retirement | 12×100,000 then 12×110,000; retirement month exclusive | match |
| O7 | Single-link glide chain back-solve, 730 days @ 12% | P = 797,317.56 (τ=0) / 813,812.10 (LTCG 10%) | match |
| O8 | Feasibility boundary (zero rates, zero inflation) | corpus 1,000,100 feasible / 999,900 infeasible — no hidden padding | match |

Conventions now pinned by test (any silent change breaks loudly):
PV→FV = `(1+i)^(days/365.25)`; per-lot tax = gain × (STCG if ≤365 d else
LTCG); pool windows Debt `[t, t+24m)` / Hybrid `[t+24m, t+48m)`; step-ups as
discrete anniversary events.

### Observation (not a defect): 365 vs 365.25 day-count mismatch

`generate_pseudo_nav` compounds daily at `(1+r)^(1/365) − 1` while every FV
and back-solve uses `days/365.25` year fractions. Over 10 years at 12% this is
a ~0.08% drift between the *displayed* tranche values (NAV-based) and the
back-solved targets. Goal funding itself is back-solve arithmetic, so goals
are funded exactly — only month-by-month displayed values carry the bps-level
drift. Inherited from v3; flagging for a future model-hygiene pass, not a
launch blocker.

## Leg 4 — Stack consistency: API / export / FE round-trip (Ph4)

**Verdict: PASS — no cross-layer drift found across all four stack layers.**

`backend/tests/test_planning_stack_consistency.py` (permanent, 17 tests):

| Class | Tests | Scope |
|---|---|---|
| TestEngineVsService | 4 | `simulate_plan()` dict vs engine raw: retirement date, snapshot total, goal names, wealth_monthly non-negative |
| TestServiceVsHttpApi | 5 | `POST /plan/simulate` HTTP vs service dict: retirement date, snapshot keys+types, snapshot total, wealth_monthly date format, goal FV non-negative |
| TestServiceVsAdvisorExport | 6 | openpyxl cells vs simulate response: retirement date cell, Goals row count, inflation stored as fraction, Personal corpus, Comprehensive sheet present, Investment Streams populated |
| TestInstrumentParamsPercFractionDrift | 2 | default engine params match service defaults; overridden percent↔fraction HTTP round-trip |

Canonical audit config `_AUDIT_FORM`: age 35, 8 M corpus, salary stream, Replenishing
Lifetime goal, Non-replenishing Annual 4-occurrence goal, one-time investment. All
glide-path DB reads mocked (D-P205-6).

`frontend/src/components/plan/__tests__/planForm.test.js` (modified, +11 tests):
`formFromInputs(buildConfig(form)) == form` for every input shape: personal fields,
Replenishing Lifetime, At-retirement null start_date, Non-replenishing Annual
occurrences, Lumpsum, Fixed-end stream, At-retirement stream, instrument_params
not-overridden, instrument_params percent↔fraction round-trip (15.0 → 0.15 → 15.0),
one_time_investments, null inputs fallback.

**No drift observed anywhere in the stack.** Two test-infrastructure fixes made
during authoring (not engine defects):

- The snapshot test searched for an exact date match in wealth_monthly for the
  retirement date. Monthly-period rows are end-of-month dates; the retirement date
  may fall on a day not in the series (e.g. "2033-09-01" vs the nearest row). Fixed
  the lookup to first row with `date >= retirement_date` — this is expected design,
  not a defect.

- The instrument-params round-trip test tried to reuse a closed `AsyncClient`
  instance. Refactored to two fresh `AsyncClient(ASGITransport(app=_app), ...)`
  instances — no semantic change to what is being tested.

**Tests: BE 698 → 715/0/0 (+17, Q16 monotonic). FE 231 → 242/0 (+11, Q16 monotonic).**

---

## Findings register

| # | Severity | What | Status |
|---|---|---|---|
| A1 | DATA (advisory team) | 8 tracker rows unusable (2 Cancelled, 5 validation-ERROR, 1 template); the three "Financial Freedom" rows suggest a template gap | Reported (Leg 1) |
| A2 | DATA (advisory team) | 11 end-date/occurrence span mismatches; 114_M3_G07 (Travel) is a genuine contradiction — 7 occurrences vs a 21-year window | Reported (Leg 1) |
| A3 | OBSERVATION (model) | NAV day-count 365 vs back-solve 365.25 — bps-level display drift, funding exact | Noted (Leg 3); future model-hygiene pass |
| A4 | PROCESS | Tracker `Requirement_escalation_pct`/`Inflation_assumption_pct` collapse into one engine field is lossless **today**; if advisory ever sets both differently the tool cannot represent it | Flag at advisory intake (Leg 1) |

**No engine calculation defect found in any leg (Legs 1–4).** All four stack layers
are consistent. The audit is complete.

---

## Launch recommendation

**SHIP.** No calculation defect found across four independent audit legs:

- **Leg 1 (Tracker mapping):** Corpus of 25 clients / 75 synthesized configs
  generated mechanically. All 75 pass `validate_plan_config`. Advisory data-quality
  items (A1, A2) are tracker-source issues for the advisory team to fix; they are
  cataloged above for handoff.

- **Leg 2 (Invariant sweep):** 7 financial invariants (payout-split identity,
  non-negative balances, full funding on success, goal-chain delivery, solver
  minimality, conservation of money, snapshot consistency) pass on all 50 feasible
  runs across 25 real clients × 3 synthesized profiles. 25 infeasible cases produce
  coherent diagnostics. 0 violations.

- **Leg 3 (Independent oracle):** 15 hand-computed-from-model-doc oracle tests pass
  first run. Engine matches SIMULATION_MODEL.md arithmetic on every asserted case
  (FV compounding, FIFO tax lots, pool windows, step-up calendar, glide back-solve,
  feasibility boundary). One observation (A3, NAV 365-day vs 365.25 back-solve) is
  bps-level display-only drift inherited from v3 — not a launch blocker.

- **Leg 4 (Stack consistency):** Same config produces identical numbers at every
  layer: engine raw → `simulate_plan()` service → `POST /plan/simulate` HTTP →
  advisor-export workbook cells → FE `formFromInputs(buildConfig(form))` round-trip.
  No percent↔fraction leak, no date coercion loss, no serialization gap.

**Permanent regression gates:** `test_planning_invariants.py`, `test_planning_oracle.py`,
and `test_planning_stack_consistency.py` will fail loudly if any future change
introduces a defect in these dimensions. The golden-master tests in
`test_planning_engine.py` continue to guard numeric reproducibility.

**Follow-up (not blockers):** A3 (365 vs 365.25 NAV day-count) is the one
model-hygiene item worth addressing in a future pass; A1/A2 are advisory-team
data fixes; A4 is an intake-process note.
