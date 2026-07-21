# INPUT contract — how the engine's input is created

This is Part 1 of the two-part architecture: everything that happens BEFORE
`engine.find_retirement_date()` is called. The engine consumes a **plain Python
dict** (deliberately not a Pydantic model — v3 decision D-P202-1); the layers
below exist to build, coerce, and validate that dict.

Field **semantics** (what each input means to the model) are canonical in
`../v3_docs/SIMULATION_MODEL.md` — this file describes the **structure** and the
coercion/validation gates, and it is verifiable line-by-line against the copied
code. Where the two ever seem to disagree, the code in `../code/app/planning/`
wins (it is byte-identical to the shipped app at the snapshot date).

## The input pipeline (in the app)

```
advisor's browser form                      (code/frontend/planForm.js)
  └─ buildConfig(form)  → JSON request body
       └─ Pydantic models                   (code/app/planning/schemas.py)
            · types/enums enforced
            · every date snapped to day=1 (month-grid invariant)
            · risk_profile → core-corpus return   (service layer)
       └─ plain dict config
            └─ engine._normalise_config_dates()   (second, defensive day=1 snap)
            └─ validation.validate_plan_config()  (business-rule gate; raises)
            └─ engine simulation
```

A playground can enter this pipeline at ANY level. The only hard requirements
before calling the engine are the dict shape below — the engine itself re-applies
the date normalisation and validation at its entry (`find_retirement_date`), so
even a hand-built dict is guarded.

## The config dict — 5 blocks

Exact shape consumed by the engine (see `run_example.py` for a working literal;
dates may be `pd.Timestamp`, `datetime.date`, or ISO strings — the engine
coerces and snaps them):

### 1. Personal & Corpus
| key | type | notes |
|---|---|---|
| `current_date` | date | snapped to the 1st of its month |
| `current_age` | float | |
| `target_lifetime` | float | must be > `current_age`; death_date = `current_date + (target_lifetime − current_age)` years |
| `current_corpus` | float ≥ 0 | starting Core Corpus, rupees |

### 2. `investment_streams` — list of
| key | type | notes |
|---|---|---|
| `name` | str | |
| `amount` | float ≥ 0 | per month, **as of `start_date`** (not today's rupees — see DECISIONS.md 2026-06-02) |
| `start_date` | date | |
| `end_date_mode` | `"At retirement"` \| `"Fixed"` | Fixed requires `end_date ≥ start_date`. **Trap for hand-built dicts:** the Pydantic schema (and the app form) default an absent value to `"At retirement"`, but `validation.py` and the engine default an ABSENT key to `"Fixed"` (which then demands an `end_date`, and the stream won't bind to the retirement search). Always set it explicitly in a raw dict. |
| `end_date` | date \| null | used only in Fixed mode |
| `step_up_percent` | float | schema default 10.0 |
| `step_up_frequency` | `"Annual"` \| `"Half-Yearly"` \| `"Quarterly"` \| `"Monthly"` | |
| `step_up_date` | date \| null | anchor; defaults to `current_date` when absent |

### 3. `goals` — list of
| key | type | notes |
|---|---|---|
| `name`, `description` | str | |
| `type` | `"Non-Negotiable"` \| `"Semi-Negotiable"` \| `"Negotiable"` | selects the glide-path sheet (Non-replenishing goals only) |
| `nature` | `"Non-replenishing"` \| `"Replenishing"` | Replenishing = ongoing payout (expense) funded via the Debt/Hybrid pools; Non-replenishing = provisioned via a glide path |
| `structure` | `"Lumpsum"` \| `"Recurring"` | |
| `start_date_mode` | `"Fixed"` \| `"At retirement"` | "At retirement" links the goal to the solver's candidate date |
| `start_date` | date \| null | required for Fixed mode |
| `amount` | float ≥ 0 | **PV in today's rupees**, grown to the goal date at `inflation_percent` (PV-in / FV-out — deliberate asymmetry vs streams) |
| `frequency` | `"Monthly"` \| `"Quarterly"` \| `"Half-Yearly"` \| `"Annual"` \| null | required for Recurring |
| `occurrences` | int ≥ 1 \| null | required when `end_mode="Occurrences"` |
| `end_mode` | `"Occurrences"` \| `"Fixed date"` \| `"Lifetime"` \| null | Recurring only; absent defaults to Occurrences |
| `end_date` | date \| null | required when `end_mode="Fixed date"`; must be ≥ start |
| `inflation_percent` | float | schema default 6.0; UI labels it "Annual growth %" |

### 4. `one_time_investments` — list of
| key | type | notes |
|---|---|---|
| `name` | str | |
| `date` | date | |
| `amount` | float ≥ 0 | face value on that date, no PV adjustment |

### 5. Instrument parameters (HTTP: `risk_profile`; engine: `instrument_params`)
The public API accepts only `risk_profile` (one of 5 values, default
`"Balanced"`); the service maps it to the **core-corpus return alone** and
leaves every other bucket at the engine defaults (`schemas.py
RISK_PROFILE_CORE_RETURNS`, decision D-P208-4/5):

| profile | core return |
|---|---|
| Very Conservative | 8% |
| Conservative | 10% |
| Balanced | 12% |
| Aggressive | 13.5% |
| Very Aggressive | 15% |

The engine itself takes the full `instrument_params` dict — per-bucket
`{'return', 'stcg_tax', 'ltcg_tax'}` for `core_corpus` / `equity` / `debt` /
`hybrid` / `cash` (`engine._DEFAULT_INSTRUMENT_PARAMS`: returns 12/12/6/10/4%,
STCG 20%, LTCG 12.5% everywhere). **A playground with "more dynamic inputs" can
pass its own dict here directly** — the 5-profile restriction is an app-API
choice, not an engine limitation.

## The two coercion gates (month-grid invariant)

ALL user-supplied dates are silently snapped to the 1st of their month
(`day=1`), at two independent points:
1. `schemas.py` — `field_validator(mode="before")` on every date field;
2. `engine._normalise_config_dates()` — at the entry of both
   `find_retirement_date` and `run_simulation`, for direct callers.

The double application is idempotent. Consequence: the simulation's time grid
is purely monthly; a playground UI only needs month+year pickers (that is
exactly what the app's form does). Rationale: `../v3_docs/SIMULATION_MODEL.md
§ Date discipline` + the app's Plan 223 (eliminated end-of-month `relativedelta`
clamping drift and an intra-month provisioning-gap bug class).

## The validation gate (`validation.py`)

`validate_plan_config(config)` raises `PlanValidationError` (a `ValueError`
subclass; `.errors` = list of ALL problems found, not just the first):

- `current_corpus` present and ≥ 0; `target_lifetime > current_age`
- streams: `amount ≥ 0`; Fixed end-mode needs `end_date ≥ start_date`
- goals: `amount ≥ 0`; Recurring needs a valid `frequency`; Occurrences mode
  needs `occurrences ≥ 1`; Fixed-date mode needs `end_date ≥ start_date`
- **span cap** (`MAX_NONREPLENISHING_SPAN_MONTHS = 48`): a NON-replenishing
  Recurring goal may span at most 4 years first-to-last occurrence —
  Occurrences mode: `(occurrences−1) × freq_months`; Fixed-date mode: calendar
  month diff; Lifetime mode: unconditional violation. Implied maxima:
  49 Monthly / 17 Quarterly / 9 Half-Yearly / 5 Annual occurrences.
  Replenishing Recurring goals are UNcapped (they're pool payouts, not glide-path
  fan-outs). This is a performance cliff guard — each non-replenishing occurrence
  spawns its own glide-path tranche chain.
- one-time investments: `amount ≥ 0`

`find_retirement_date` calls this itself; `run_simulation` does NOT (the solver
pre-validates — direct `run_simulation` callers are expected to validate first).

## What the app injects that the engine ignores

The app's route adds `client_name` and `m3_id` to the config (used only by the
Excel export for headers). The engine reads neither. `PlanSimulateRequest` has
`model_config = {"extra": "ignore"}` — unknown request fields are dropped.
