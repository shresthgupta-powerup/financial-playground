# Inputs JSON — contract samples for CRM ingestion

Two files, **one identical plan**, exported through the playground's real
download code (engine `v2grid+goaltaxequity+fixedstart`):

| File | What it is |
|---|---|
| `sample_inputs_A_as_entered.json` | The plan exactly as the CM entered it. This is what the version log stores and what "Inputs (JSON)" downloads before a run. |
| `sample_inputs_B_resolved_after_run.json` | The same plan after a successful solve (earliest retirement Sep 2031). Open-ended series are resolved to concrete numbers — this file matches the simulation exactly. |

Diff the two to see exactly what resolution does; everything else is
byte-identical.

## What resolution changes (A → B)

- **"At retirement" start** → a concrete `start_date` (the solved retirement
  month) and `start_date_mode` becomes `Fixed`.
- **`end_mode: "Lifetime"`** → a concrete `occurrences` count (to the plan's
  lifetime end) and a concrete `end_date`.
- **`end_mode: "Fixed date"`** → the implied `occurrences` count is filled in
  (start → end at the frequency's step, inclusive).

## The goals in the sample, and what each demonstrates

| Goal | Shape | Demonstrates |
|---|---|---|
| Home Purchase Down Payment | Lumpsum, Non-negotiable, 4y out | Single cashflow; no recurring fields in the JSON |
| Marriage | Lumpsum, Semi-negotiable, 12y out | `nature: Replenishing` on a one-time goal — see §4.5 note below |
| World Trip | Lumpsum, Negotiable | Third negotiability value |
| Child Education | Recurring, Annual ×4 | `payments_fixed_at_start: true` — fees lock at admission |
| Home Loan EMI | Recurring, Monthly ×180, 0% growth | A signed EMI: flat at today's amount for its whole life |
| Parents Support | Recurring, Quarterly, Fixed-date end | `end_date` instead of `occurrences`; count filled in file B |
| Retirement Income | Recurring, starts At retirement, Lifetime | The one INCOME goal: `payments_fixed_at_start: false` |
| Child Education 2 | Duplicate name | The app numbers duplicates so both goals fund (never silently merged) |

## Field notes

**`payments_fixed_at_start`** (recurring goals only) — **derived by policy,
not chosen by the CM**: every recurring goal is contract-fixed (the amount
escalates at `inflation_percent` only until the FIRST payment, then every
payment stays at that amount) EXCEPT income-like series, which keep
escalating throughout. "Income" is structural: the goal starts At retirement,
or its payments run for Lifetime. Please persist this field with the goal —
a plan re-imported later must keep the same treatment. This deliberately
diverges from §4.2's per-occurrence escalation for the contract-fixed cases;
your worked tables still reproduce against our doc-literal reference module.

**`nature`** — derived per §4.5, never typed: "Replenishing" means *the goal
has cashflows beyond the carve window today* (5/4/3 years by negotiability),
i.e. a later plan carries it forward. Read it as a statement about the window,
not about the goal being endless — a one-time Marriage 12 years out is
correctly "Replenishing" in this vocabulary, and becomes "Non-replenishing"
by itself once it drifts inside the window. If you derive nature on your side,
derive it from the cashflow series + the grid's reach; do not key logic off
our exported value being stable over time.

**`amount`** — today's rupees, **per payment** for recurring goals (one
installment, not the series total). Escalation to actual payment dates is the
engine's job per the rules above.

**Dates** — always `YYYY-MM-01`: the engine snaps everything to a day-1 month
grid.

**`type`** — negotiability: `Non-negotiable` / `Semi-negotiable` /
`Negotiable` (CRM casing).

**Goal IDs** — none in this export by agreement: you mint IDs at ingestion.
Names are unique within a plan (duplicates arrive numbered).

**Top-level** — `engine_version` stamps which model produced the file;
`simulation_id` ties it to the run log; `retirement_mode` / `target_date` /
`target_age` describe the solve mode (`earliest` here, so targets are null).
`personal`, `investment_streams`, `one_time_investments` are a faithful record
of the form; the CRM contract only requires `goals` + `personal.client_name`
+ `generated_at`.
