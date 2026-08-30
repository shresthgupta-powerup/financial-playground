# CRM goals contract — samples and notes

Built to Punit's flat-structure spec (2026-08-30). Engine
`v2grid+goaltaxequity+fixedstart`.

| File | What it is |
|---|---|
| `sample_crm_goals_upload.json` | **The upload file.** Exactly the contract: `{"goals": [...]}`, eleven keys per goal, CRM vocabulary, dates resolved. This is what the playground's "CRM goals upload" button produces. |
| `sample_inputs_A_as_entered.json` | Our own record of the same plan, as the CM entered it. Reloads into the form; not for CRM ingestion. |
| `sample_inputs_B_resolved_after_run.json` | The same, after the solve. Shown so you can see what our resolution does. |

The eight goals cover every shape a CM can build: all three
negotiabilities, one-off goals, a fixed-count series, a fixed-end-date
series, a lifetime income starting at retirement, a signed 0%-growth EMI,
and a duplicate goal name (numbered "Child Education 2" by us).

## How each contract field is produced

| Field | Source |
|---|---|
| `purpose_id` | `null` for a goal never uploaded. Once the CRM mints one, it is stored on the goal and re-sent on every later upload. Loading a CRM goals file back into the playground is how the ids arrive. |
| `goal_name` | Unique per plan, case-insensitively — we number duplicates ourselves. |
| `goal_type` | Chosen by the CM from your ten categories; `null` when not set. |
| `goal_negotiability` | `non_negotiable` / `semi_negotiable` / `negotiable`. |
| `goal_description` | Always a string, `""` when blank — never null. |
| `amount_per_occurrence` | Whole rupees, today's money, one payment (not the series total). |
| `occurrences` | Real count; `1` for a one-off. **500** for a lifetime series (see below). |
| `frequency` | `null` exactly when `occurrences == 1`, else `monthly` / `quarterly` / `half_yearly` / `yearly`. We have no `every_other_year` input. |
| `start_date` | `YYYY-MM-01`, always concrete — "at retirement" is resolved against the solved date, so **the upload file only exists for a plan that has been run successfully**. |
| `inflation` | Fraction (8% → `0.08`). |
| `goal_status` | Always `active` — the playground has no notion of a goal being achieved or cancelled. |

## Two things worth confirming

**1. `occurrences: 500` — we read it as "lifetime", and only that.** Your
spec says to write 500 for "open-ended (lifetime / at-retirement)". We apply
it only to a series whose length genuinely is not stated (our Lifetime end
mode). A series that merely *starts* at retirement but runs a stated 240
payments keeps 240 — writing 500 there would misstate a real number, and its
start date is resolved anyway. Shout if you meant 500 for both.

**2. 500 is load-bearing on the way back.** The contract drops `end_mode` and
`start_date_mode`, which is exactly what our fixed-vs-inflating rule reads:
every recurring goal is contract-fixed (amount escalates only to the FIRST
payment, then flat — EMIs are signed, fees lock at admission) **except**
income-like series, which keep escalating. After a round trip the only
surviving signal that a goal is income is `occurrences == 500`, so that is
what we key on. It holds in practice (500 monthly payments is 41 years, well
past any real EMI), but it does mean a genuine 500-payment fixed series would
be misread as income. If that ever matters, the cleanest fix is a flag you
store; for now nothing in our corpus comes close.

## Not sent, deliberately

`payments_fixed_at_start` stays derived on our side per your note.
`nature`, `structure`, `end_mode`, `start_date_mode` are gone from the upload
file. Our own inputs JSON still carries them — it is our save format, not
yours, and the CRM never sees it.
