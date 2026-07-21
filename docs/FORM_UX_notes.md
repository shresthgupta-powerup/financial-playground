# Form UX notes — how the app collects the inputs

Reference for building a playground input surface. Source of truth:
`../code/frontend/planForm.js` (byte-identical copy of the app's pure form
model — no React in it, so every function is directly reusable/portable).
The app's page/hook/components are NOT copied (they are React-app-coupled);
what matters for a new tool is the input model below.

## Why this file matters for a playground

The app spent several design iterations learning how advisors actually enter
plans. The lessons are encoded in `planForm.js` as pure functions — a new tool
can lift them wholesale (it's dependency-free JS) or mirror the ideas:

### 1. Start from a filled form, not an empty one (`makeDefaultForm`)
Defaults: age 30, lifetime 90, ₹1 Cr corpus, Balanced profile, ONE salary
stream (₹1L/month, 10% annual step-up, ends at retirement) and ONE
Retirement-Income goal (₹75k/month, Replenishing, Lifetime, starts at
retirement). Advisors edit numbers; they don't assemble structures from scratch.

### 2. Goal templates (`GOAL_TEMPLATES`, `makeGoalFromTemplate`)
"Add goal" offers 5 choices, each pre-filling the structural fields so the user
only supplies amount + timing:
| template | nature / structure / type | prefills |
|---|---|---|
| Retirement Income | Replenishing / Recurring / — | ₹75k monthly, Lifetime, starts at retirement, 6% growth |
| Child Education | Non-replenishing / Recurring / Non-Negotiable | ₹15L annual ×4, starts +12y, 8% growth |
| Marriage | Non-replenishing / Lumpsum / Semi-Negotiable | ₹30L, +20y, 7% growth |
| Home Purchase | Non-replenishing / Lumpsum / Negotiable | ₹50L, +8y, 6% growth |
| Custom | the full generic form | — |

### 3. Progressive disclosure (`normaliseGoal`)
Inapplicable fields are never rendered, and a field change resets the fields it
invalidates so the engine never sees a stale combination:
- Replenishing → hides `type` (no glide path) and forces Recurring
- Lumpsum → hides frequency / occurrences / end-mode
- `start_date_mode='At retirement'` → hides the start-date picker
- end_mode: Occurrences → count field; Fixed date → end-date field; Lifetime → nothing
- Streams: `end_date_mode='At retirement'` collapses the end-date field

### 4. Month/year pickers only (`snapMonthStartISO`, `MonthYearField` in the app)
The engine's grid is monthly (see INPUT_contract.md) — the app's lowest date
grain is a month dropdown + 4-digit year input; the wire value is a plain ISO
`YYYY-MM-01` string. `buildConfig` snaps every emitted date to day=1;
`formFromInputs` (the reverse mapping, for loading a saved plan back into the
form) defensively snaps stored dates too, so legacy day-precision data still loads.

### 5. Client-side mirror of server validation (`validateConfig`, `hasErrors`)
Same rules as `validation.py` (negative amounts, lifetime ≤ age, end < start,
recurring needs frequency + occurrences ≥ 1, 48-month span cap) — rendered
next to the field as-you-type, with Run/Export disabled while any error exists.
The server stays the authoritative backstop (422 with `{errors: [...]}`).
If you fork the validation rules, change BOTH sides or drop the client mirror.

### 6. Money entry (`formatINR` in the app, hint strings here)
Amount fields take plain rupees and show a live grouped INR rendering plus a
lakh/crore hint ("1 L", "1.5 Cr"). `RISK_PROFILE_RETURNS` /
`FIXED_BUCKET_RETURNS` are display mirrors of the backend mapping — the UI
shows "the engine will use X%" next to the risk-profile picker (debt 6% and
hybrid 10% are fixed and labelled as such).

### 7. The two build/load mappings
- `buildConfig(form)` → the exact request body / engine dict (at-retirement
  goals send `start_date: null`; `risk_profile` sent, never instrument params).
- `formFromInputs(inputs)` → reverse mapping used to reopen a saved plan.
Keeping these as a pure, tested pair is what makes save/load round-trips safe —
recommended pattern for any playground that persists scenarios.

## The app's lifecycle around the form (context, not required for a playground)

The app locks the form by default (VIEW of the latest saved version), with an
explicit Edit → Run simulation → Save flow; any edit after a run invalidates
the shown result; only feasible results can be saved; saves are immutable
versioned rows stamped with `engine_version` + `glidepath_version`. A
what-if playground will likely want the opposite default (always live), but the
two stamps are worth keeping for reproducibility of anything a user saves or
shares.
