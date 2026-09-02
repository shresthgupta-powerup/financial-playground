# CRM goals contract — samples and notes

Built to contract **v2** (Punit, 2026-08-31). Engine
`v2grid+goaltaxequity+fixedstart`.

| File | What it is |
|---|---|
| `sample_crm_goals_upload.json` | **The upload file.** Exactly the contract: `{"goals": [...]}`, twelve keys per goal, CRM vocabulary, dates resolved. This is what the playground's "CRM goals upload" button produces. |
| `sample_inputs_A_as_entered.json` | Our own record of the same plan, as the CM entered it. Reloads into the form; not for CRM ingestion. |
| `sample_inputs_B_resolved_after_run.json` | The same, after the solve. Shown so you can see what our resolution does. |

The eight goals cover every shape a CM can build: all three negotiabilities,
one-off goals, a fixed-count series, a fixed-end-date series, a lifetime
income starting at retirement, a signed 0%-growth EMI, and a duplicate goal
name (numbered "Child Education 2" by us).

## What changed from v1

- `purpose_id` **removed** — the upload is ADD-only, so nothing in the file
  addresses an existing goal.
- `lifetime` **added** (boolean, recurring only).
- `payments_fixed_at_start` **added** (boolean, recurring only).
- `occurrences` now always the **true simulated count**. The 500 sentinel is
  gone; nothing in the file is a magic number.
- `every_other_year` dropped from both enums.

## The twelve fields

| Field | Source |
|---|---|
| `goal_name` | Unique per plan, case-insensitively — we number duplicates ourselves. |
| `goal_type` | The CM's category, from your ten values. `null` when unset — see the note below. |
| `goal_negotiability` | `non_negotiable` / `semi_negotiable` / `negotiable`. |
| `goal_description` | Always a string, `""` when blank. |
| `amount_per_occurrence` | Whole rupees, today's money, **one payment** — not the series total. |
| `occurrences` | The real count the simulation used. `1` for a one-off. |
| `lifetime` | `true` when the series runs for the client's life; `null` when `occurrences == 1`. |
| `payments_fixed_at_start` | Our policy value, verbatim (see below); `null` when `occurrences == 1`. |
| `frequency` | `null` exactly when `occurrences == 1`, else `monthly` / `quarterly` / `half_yearly` / `yearly`. |
| `start_date` | `YYYY-MM-01`, always concrete — "at retirement" is resolved against the solved date, so **the upload file only exists for a plan that has been run successfully**. |
| `inflation` | Fraction (8% → `0.08`). |
| `goal_status` | Always `active` — the playground has no notion of a goal being achieved or cancelled. |

## `payments_fixed_at_start` — what it means

Policy on our side, derived per goal, never a CM choice: **every recurring
goal is contract-fixed** — the amount escalates at `inflation` only until the
FIRST payment, then every payment stays at that amount (an EMI is signed,
college fees lock at admission) — **except income-like series**, which keep
escalating with the cost of living.

You store and return it verbatim; we re-derive it from current policy when a
file is loaded back, so a stale row can never override the live rule.

## Why v2's explicit flags matter

v1 inferred "unbounded" from `occurrences == 500`. That was wrong on real
data — an income whose count resolved to 397 (or 540, as in this sample)
missed the sentinel and would have been read back as contract-fixed, silently
stopping a client's retirement income from tracking inflation.

The same loss existed in **our own** save format, independently of the CRM:
resolution replaced Lifetime with a count and "At retirement" with a date,
erasing both signals the policy reads. Our inputs JSON now carries `lifetime`
too, and both round trips are covered by tests.
