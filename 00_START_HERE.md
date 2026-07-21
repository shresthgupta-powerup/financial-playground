# Financial-Plan Engine — Handoff Package

**Purpose:** everything needed to build an independent financial-planning
"playground" (more dynamic inputs, planning variations) on top of the engine
that powers the Infinite internal tool's Financial Plan page.

**Snapshot:** 2026-07-17, taken from the INF_APP_BETA repo at commit `fffcf39`
(the code deployed to production). Engine version stamp:
`ENGINE_SOURCE_SHA = "1515f1e+pool2x2+lifetimefix+monthgrid"`. This package is
a frozen copy — it does not track later app changes.

## The two-part architecture

The tool splits cleanly into:

1. **Input assembly** — turns human-facing inputs into the engine's plain-dict
   config: form model → Pydantic schema (types + month-snapping + risk-profile
   mapping) → validation. Read **`docs/INPUT_contract.md`**.
2. **The engine** — a pure, deterministic simulator (`find_retirement_date`
   solver + `run_simulation`): glide-path goal funding, Debt/Hybrid payout
   pools, FIFO tax-lot accounting, month-by-month wealth view. Read
   **`docs/ENGINE_contract.md`**.

## Quickstart (5 minutes)

```bash
python -m venv .venv
.venv/Scripts/activate            # (Windows; source .venv/bin/activate elsewhere)
pip install -r requirements.txt
python run_example.py             # engine solves a sample plan, no DB/app needed
pytest code/tests -q              # 117 passed, 6 skipped expected (~90s)
```

The 6 skips are by-design guards inside the invariants suite ("infeasible under
moderate profile"); pandas PerformanceWarnings are known/inherited, not defects.

## What's in the box

| path | what | status |
|---|---|---|
| `code/app/planning/` | the full planning package from the app — 10 files | **byte-identical copies** |
| `code/app/database_pg.py` | import stub so app-coupled files import without a DB | handoff-only, NOT a copy |
| `code/tests/` | engine golden-master + oracle + invariants suites + corpus fixture | copies (+ a new minimal `conftest.py`) |
| `code/frontend/planForm.js` | the app's pure form model (defaults, templates, validation, buildConfig) | byte-identical copy |
| `run_example.py` | standalone engine demo | handoff-only |
| `docs/INPUT_contract.md` | Part 1 — the config-dict contract + coercion/validation gates | authored for this handoff |
| `docs/ENGINE_contract.md` | Part 2 — entrypoints, pools, tax lots, deliberate choices | authored for this handoff |
| `docs/FORM_UX_notes.md` | input-UX lessons worth stealing for a playground | authored for this handoff |
| `docs/financial_plan_demo.md` | worked scenario with real expected outputs (Sharma family) | copy from the app repo |
| `docs/financial_plan_audit.md` | the pre-launch accuracy audit (verdict: SHIP, 0 calc defects) | copy from the app repo |
| `v3_docs/SIMULATION_MODEL.md` | **canonical: how the model works, end-to-end** | verbatim from the v3 repo |
| `v3_docs/DECISIONS.md` | **canonical: why each non-obvious modelling choice** | verbatim from the v3 repo |

## Portable vs app-coupled

**Runs anywhere (the playground core):**
- `engine.py` — the simulator (pandas/numpy/dateutil only)
- `validation.py` — input rules
- `glide_paths.py` — versioned glide-path data (`get_glide_paths()` literals)
- `schemas.py` — the Pydantic input models + risk-profile→return mapping
- `advisor_export.py` — advisor Excel workbook (imports only the engine plus pandas/dateutil; openpyxl is used at write time — nothing app-coupled)
- `planForm.js` — dependency-free JS form model

**App-coupled (included as REFERENCE — imports work via the stub, DB calls raise):**
- `service.py` — response shaping for the app's API; its `_build_snapshot` /
  `_build_wealth_monthly` helpers are pure and show how to turn engine output
  into UI rows, but `simulate_plan` reads glide paths from the app's PostgreSQL
- `glide_paths_repo.py`, `plans_repo.py` — DB loaders / versioned plan persistence
- `routes.py` — the FastAPI endpoints (also needs fastapi, not in requirements)

## Reading order for the new tool's author

1. This file, then `run_example.py` (run it, read it — ~100 lines).
2. `v3_docs/SIMULATION_MODEL.md` — the model itself. Non-negotiable read.
   One known staleness: it describes the Hybrid pool window as months 25–60;
   the shipped engine deliberately narrowed it to 25–48 (`+pool2x2` — see
   ENGINE_contract.md), and the oracle tests encode the engine's 25–48.
3. `docs/INPUT_contract.md` + `docs/ENGINE_contract.md` — the precise surfaces.
4. `v3_docs/DECISIONS.md` — before changing ANY behaviour: several things that
   look wrong are deliberate (income net-off, PV-in/FV-out asymmetry,
   tranche-and-chain glide paths…). The engine contract lists the traps.
5. `docs/financial_plan_demo.md` — end-to-end worked example with numbers.
6. `code/tests/` — the executable spec. `test_planning_oracle.py` was written
   from SIMULATION_MODEL.md alone (never from engine code): if you re-implement
   or modify the engine, these 15 tests + the invariants suite are your safety
   net. Keep them green, or know exactly why they moved.

## Ground rules for the fork

- The copies here are **byte-identical** to production at the snapshot date —
  verified by hash at packaging time, and the test run above re-proves behaviour.
  If you edit them, you own the divergence: bump your own version stamp
  (see `ENGINE_SOURCE_SHA` in `docs/ENGINE_contract.md`) so results stay
  attributable to an engine version.
- The 48-month span cap on non-replenishing recurring goals is a performance
  guard, not a model constraint — a playground may relax it, but read the
  perf note in ENGINE_contract.md first.
- The 5-risk-profile restriction is an app-API choice; the engine accepts a
  full per-bucket `instrument_params` dict — the natural "more dynamic inputs"
  extension point.

## Contact

Questions about model intent: Punit Patel (owner of the app + the v3 source
repo). The app-side context lives in the INF_APP_BETA repo
(`.context/modules/financial-planning.md`) if you are ever given access.
