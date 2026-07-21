# Financial Plan Demo: Sharma Family

A reference walkthrough for the financial-planning feature (LP-015 C4).
This document shows an advisor how to use the plan form from first open through
Save, and records the real engine output for the scenario so reviewers and
testers can verify the UI without re-running the engine.

> **Seeded in prod** on family **INF_H7DVI9I0** -- open its detail page ->
> "Financial Plan" card. It opens in VIEW mode showing the latest saved version;
> click **Edit** to walk the Edit -> Run -> Save lifecycle in Section 3.
> (Re-seeded 2026-06-09 with the corrected config below + the 2+2 pool update;
> engine `1515f1e+pool2x2+lifetimefix+monthgrid`. Plan 222 reverted the weekend redesign and
> fixed the pool death-date bug; Plan 223 added month-grid boundary coercion (all
> dates snap to the 1st). The demo net-off config and numbers are unaffected.)

---

## 1. Scenario Narrative

**Family:** Sharma (demo only -- not a real advisory client)

Rajesh Sharma (age 38) is a salaried professional with a current invested
corpus of 1.5 Cr (Core Corpus). He and his spouse want to:

1. Buy a home in 2029 (lump-sum, Non-Negotiable).
2. Fund their child's undergraduate education starting 2035 (Annual recurring
   over 5 years, Non-Negotiable).
3. Retire as early as feasible and draw a monthly income for life (Replenishing,
   Non-Negotiable).

He invests a **monthly** salary SIP (with annual step-up) and a smaller
**monthly** rental income (which continues into early retirement, then ends in
2040). He also expects an inheritance receipt in early 2028.

This scenario deliberately exercises EVERY input type the form supports:
personal & corpus (with Risk Profile = Balanced), two investment streams (one
At-retirement end-mode, one Fixed end-mode), a Lumpsum Non-replenishing goal,
a Recurring Non-replenishing goal with Annual growth %, a Replenishing Monthly
lifetime goal, and a one-time investment.

> **Note -- stream amounts are MONTHLY.** The form field is "Monthly amount
> (Rs at start)". Every investment stream amount is a per-month figure, grown by
> its step-up. (Goal amounts follow the goal's own basis: "Total amount" for a
> Lumpsum, "Amount per occurrence" for a Recurring goal.)

---

## 2. Field-by-Field Inputs

All amounts are in today's rupees (PV-today convention, D-LP015-10). The advisor
enters them in the INR amount fields; the form shows the "lakh/crore" hint.

### Personal & Corpus

| Form field        | Value         | Notes                              |
|-------------------|---------------|------------------------------------|
| Current date      | Jun 2026      | Month of the "as of" date (day=1 assumed) |
| Current age       | 38            | Years                              |
| Target lifetime   | 90            | Years                              |
| Current corpus    | 1,50,00,000   | 1.5 Cr in today's rupees           |
| Risk Profile      | Balanced      | Engine uses 12% Core Corpus return |

### Investment Streams

**Stream 1 -- Monthly Salary SIP** (At-retirement end-mode)

| Form field          | Value        | Notes                                      |
|---------------------|--------------|--------------------------------------------|
| Name                | Monthly Salary SIP | Free text                            |
| Monthly amount (Rs at start) | 1,50,000 | 1.5 L / month                         |
| Start date          | Jul 2026     | Month picker (day=1 assumed)               |
| End date mode       | At retirement| Stream stops on the solved retirement date |
| Annual step-up %    | 10.0         | 10% each year                              |
| Step-up frequency   | Annual       |                                            |
| Step-up date        | Jan 2027     | First step-up on 1 Jan 2027                |

**Stream 2 -- Rental Income** (Fixed end-mode -- exercises the "Fixed" branch)

| Form field          | Value        | Notes                                      |
|---------------------|--------------|--------------------------------------------|
| Name                | Rental Income | Free text                                 |
| Monthly amount (Rs at start) | 40,000 | 0.4 L / month (a small side income)     |
| Start date          | Jul 2026     | Month picker (day=1 assumed)               |
| End date mode       | Fixed        | Stream runs until a fixed end date         |
| End date            | Dec 2040     | Rental ends a few years into retirement    |
| Annual step-up %    | 5.0          |                                            |
| Step-up frequency   | Annual       |                                            |
| Step-up date        | Jul 2027     |                                            |

### Goals

**Goal 1 -- Home Purchase** (Lumpsum Non-replenishing Non-Negotiable)

| Form field             | Value         | Notes                                   |
|------------------------|---------------|-----------------------------------------|
| Name                   | Home Purchase |                                         |
| Type                   | Non-Negotiable| Glide-path sheet                        |
| Nature                 | Non-replenishing |                                      |
| Structure              | Lumpsum       | Single disbursement                     |
| Start date mode        | Fixed         |                                         |
| Start date             | Jun 2029      | Month picker (day=1 assumed)            |
| Total amount (Rs today)| 80,00,000     | 80 L today (label: "Total amount")      |
| Annual growth %        | 7.0           | Property inflation                      |

**Goal 2 -- Child Education** (Recurring Non-replenishing, Annual over 5 years)

| Form field              | Value         | Notes                                  |
|-------------------------|---------------|----------------------------------------|
| Name                    | Child Education |                                      |
| Type                    | Non-Negotiable|                                        |
| Nature                  | Non-replenishing |                                     |
| Structure               | Recurring     |                                        |
| Start date mode         | Fixed         |                                        |
| Start date              | Jun 2035      | Month picker (day=1 assumed)           |
| Amount per occurrence (Rs today) | 5,00,000 | 5 L per year (label: "Amount per occurrence") |
| Frequency               | Annual        |                                        |
| End mode                | Occurrences   |                                        |
| Number of occurrences   | 5             | 5 annual instalments                   |
| Annual growth %         | 8.0           | Education cost inflation               |

**Goal 3 -- Retirement Income** (Replenishing Monthly Lifetime)

| Form field              | Value         | Notes                                  |
|-------------------------|---------------|----------------------------------------|
| Name                    | Retirement Income |                                    |
| Nature                  | Replenishing  | For Replenishing: type/structure are fixed by the form (always Recurring payout) |
| Start date mode         | At retirement | Begins on the solved retirement date   |
| Amount per occurrence (Rs today) | 2,00,000 | 2 L / month in today's rupees   |
| Frequency               | Monthly       |                                        |
| End mode                | Lifetime      | Runs until target lifetime (age 90)    |
| Annual growth %         | 6.0           | Retirement income inflation            |

### One-Time Investments

| Field       | Value        | Notes                              |
|-------------|--------------|------------------------------------|
| Name        | Inheritance Receipt |                             |
| Date        | Jan 2028     | Month picker (day=1 assumed)       |
| Amount      | 20,00,000    | 20 L lump                          |

### Risk Profile

Risk Profile is set to **Balanced** in the Personal & Corpus section (the form
default). The form shows a read-only hint: "Engine will use this return for the
selected risk profile." For Balanced the hint reads **12%**.

The engine uses this return for the Core Corpus bucket only; debt/hybrid returns
and all tax rates come from engine defaults and are not configurable from the form.

> The Advanced Assumptions section has been removed (D-P208-7). Risk Profile is
> now the single user-facing control for the core return assumption.

---

## 3. How to Use It -- Step-by-Step Walkthrough

The walkthrough maps to the VIEW -> Edit -> Run -> Save lifecycle (D-P206-7).

### Step A: Open the plan page

1. Navigate to the Sharma family's detail page in the CRM.
2. Click the "Financial Plan" card (hidden for archived families).
3. The page opens in **VIEW mode**.
   - If no plan has been saved yet: an empty-state message ("No financial plan
     yet") shows with the **[Create plan]** button top-right. No form fields are
     shown until you click Create plan.
   - If a plan was saved previously: the banner shows "Viewing v{n} - saved
     {date}" and the form is shown locked (read-only) with the stored results.

### Step B: Enter edit mode

4. Click **[Create plan]** (or **[Edit]** if a saved plan already exists).
5. The status banner changes to: "Editing - run a simulation to save".
6. The form fields appear (editable). The **[Run simulation]** button appears.

### Step C: Fill in the inputs

7. In "Personal & Corpus", set the fields from Section 2. The **Risk Profile**
   selector appears here; set it to **Balanced**. The form shows a read-only
   hint: "12%" (the Core Corpus return the engine will use).
8. In "Investment Streams", the form starts with one default stream. Edit it
   to match Stream 1 (Monthly Salary SIP). Use **[+ Add stream]** to add
   Stream 2 (Rental Income) and set its End date mode to "Fixed" + an end date.
9. In "Goals":
   - The form starts with a default Retirement Income goal. Edit it to match
     Goal 3 (Replenishing Monthly, At retirement, 2 L/month, Annual growth 6%).
   - Click **[+ Add goal]** -> choose template "Home Purchase". Fill in Goal 1.
     Note the amount label: "Total amount (Rs today)".
   - Click **[+ Add goal]** -> choose template "Child Education". Fill in Goal 2.
     Note the amount label: "Amount per occurrence (Rs today)" and the
     "Annual growth %" field (with the helper: "In today's rupees; grown to the
     goal date at this rate.").
10. In "One-time Investments", add the Inheritance Receipt entry.
11. Inline validation runs as you type. Negative amounts or impossible dates
    block the Run button with inline error messages.

### Step D: Run the simulation

13. Once all fields are valid, click **[Run simulation]**.
14. The status banner changes to: "Simulated - review the result and Save".
15. The right pane shows the results (see Section 4 for the expected numbers).
16. **Any edit to a field after this point** resets the mode to EDIT and hides
    the Save button -- the displayed result always matches the current inputs.

### Step E: Save the plan

17. If the result is feasible (success=true), the **[Save plan]** button appears.
18. Click **[Save plan]**. The server re-runs the simulation authoritatively
    (D-P206-3) and persists a new version row in `inf_financial_plans`.
19. The status banner updates to: "Viewing v{n} - saved {timestamp}".
20. The form returns to locked (VIEW mode). The results pane shows the stored
    results (never a silent recompute -- D-P206-6).

### Infeasibility case (for testing)

To see the infeasibility diagnostic: drop the corpus to a small value (e.g.
5,00,000) and run. The banner shows "Not fundable - adjust the inputs to save"
and the Save button stays hidden. Restore the corpus to 1.5 Cr to make it
feasible again.

### Archived family (for testing)

Open the plan page for any archived family. The Edit/Create buttons are absent
and the banner carries a "Read-only" badge. The form stays locked.

---

## 4. Expected Results (Real Engine Output)

Captured by running `service.simulate_plan` against the Section-2 config with the
literal glide paths (`GLIDEPATH_VERSION = 1`) and Risk Profile = Balanced (12%
Core Corpus return).

Engine version: `ENGINE_SOURCE_SHA = 1515f1e+pool2x2+lifetimefix+monthgrid` (the v3 `1515f1e`
port with the 2+2 pool window + Plan 222 death-date provisioning fix + Plan 223
month-grid boundary coercion).
Glide-path version: `GLIDEPATH_VERSION = 1`

### Retirement Date

| Field               | Value            |
|---------------------|------------------|
| Earliest retirement | Jul 2037         |
| Age at retirement   | 49.1 years       |

### Wealth Snapshot at Retirement (Jul 2037)

| Bucket         | Amount (Rs)     |
|----------------|-----------------|
| Core Corpus    | 7,56,87,918     |
| Debt Pool      | 74,09,222       |
| Hybrid Pool    | 74,67,665       |
| Goal (Debt)    | 24,62,571       |
| Goal (Hybrid)  | 0               |
| **Total**      | **9,30,27,375** |

### Per-Goal Funding Status

| Goal              | PV (today, Rs) | FV at start (Rs) | Start date  | Nature          | Structure |
|-------------------|----------------|------------------|-------------|-----------------|-----------|
| Home Purchase     | 80,00,000      | 97,86,285        | Jun 2029    | Non-replenishing| Lumpsum   |
| Child Education   | 5,00,000       | 9,97,766         | Jun 2035    | Non-replenishing| Recurring |
| Retirement Income | 2,00,000       | 3,81,010         | Jul 2037    | Replenishing    | Recurring |

FV at start = PV x (1 + Annual growth %)^(years to start) -- the amount in
rupees of the goal year.

### Monthly Wealth Table (sample rows)

Total rows: 624 (Jun 2026 to May 2078, the full target lifetime).

| Phase            | Date     | Total (Rs)    | Core (Rs)    | Debt (Rs)  | Hybrid (Rs) |
|------------------|----------|---------------|--------------|------------|-------------|
| Build-up (first) | Jun 2026 | 1,50,82,627   | 88,12,517    | 0          | 0           |
| At retirement    | Jul 2037 | 9,30,27,375   | 7,56,87,918  | 74,09,222  | 74,67,665   |
| End (age 90)     | May 2078 | 3,34,11,426   | 1,61,68,918  | 44,93,200  | 1,27,49,308 |

The corpus draws down through retirement and still lands positive at age 90 --
a comfortable plan. (The terminal value is sensitive to the exact retirement
month: the solver picks the *earliest feasible* date, which sits near the
funding frontier. Retiring a little later would leave a much larger estate --
a property of the deterministic 12%-return model, no sequence-of-returns risk.)

### Understanding the Debt / Hybrid pools (why they're non-zero here)

Replenishing payouts (the retirement income) are funded only from the **Debt
pool**; the **Hybrid pool** is a buffer that feeds the Debt pool and never pays
a payout directly. At each annual review the engine pre-funds the Debt pool for
the **next 2 years** of net payouts and the Hybrid pool for the **2 years after
that** (the 2+2 window), topping up from Core Corpus.

The pools fund only the payout that **investment doesn't already cover**. In
this demo the salary stops at retirement and the rental (0.4 L/month) is far
smaller than the 2 L/month income, so net payouts hit the pool right at
retirement -- hence the Debt and Hybrid pools are both funded (~74 L each) in
the snapshot. (If an income stream large enough to cover the payouts ran past
retirement, the Debt pool would correctly read 0 until that stream ended --
there would simply be nothing for it to fund yet.)

---

## 5. Input-Type Coverage

| Input type                                    | Covered by this demo config?  |
|-----------------------------------------------|-------------------------------|
| Personal & Corpus block                        | Yes -- age 38, corpus 1.5 Cr  |
| Risk Profile selector                          | Yes -- Balanced (12%)         |
| Investment stream, end_date_mode = At retirement | Yes -- Stream 1 (salary SIP) |
| Investment stream, end_date_mode = Fixed        | Yes -- Stream 2 (rental)      |
| Annual step-up on a stream                     | Yes -- both streams            |
| Lumpsum Non-replenishing goal (Non-Negotiable) | Yes -- Home Purchase          |
| Recurring Non-replenishing goal (Annual, N occurrences) | Yes -- Child Education |
| Annual growth % on a Non-replenishing recurring goal | Yes -- Child Education 8% |
| Replenishing Monthly Lifetime goal             | Yes -- Retirement Income      |
| Annual growth % on a Replenishing goal         | Yes -- Retirement Income 6%   |
| At-retirement start_date_mode on a goal        | Yes -- Retirement Income      |
| One-time investment                            | Yes -- Inheritance Receipt    |
