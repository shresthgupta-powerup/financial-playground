"""Standalone demo: run the financial-plan engine with ZERO app dependency.

Proves the engine in code/app/planning/ needs nothing but pandas / numpy /
python-dateutil. No database, no FastAPI, no config -- the glide paths come
from the checked-in literals (glide_paths.get_glide_paths()) and the
instrument parameters default to engine._DEFAULT_INSTRUMENT_PARAMS.

    python run_example.py

The scenario loosely mirrors docs/financial_plan_demo.md (the Sharma family):
age 38, Rs 1.5 Cr corpus, one salary stream, a retirement-income goal, a
child-education goal and a one-time investment. For the full field-by-field
semantics read docs/INPUT_contract.md and v3_docs/SIMULATION_MODEL.md.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "code"))

import pandas as pd

from app.planning.engine import (
    find_retirement_date,
    run_simulation,
    ENGINE_SOURCE_SHA,
    _DEFAULT_INSTRUMENT_PARAMS,
)
from app.planning.glide_paths import get_glide_paths, GLIDEPATH_VERSION

TODAY = pd.Timestamp("2026-07-01")

CONFIG = {
    # Block 1 -- Personal & Corpus
    "current_date": TODAY,
    "current_age": 38,
    "target_lifetime": 90,
    "current_corpus": 15_000_000,          # Rs 1.5 Cr
    # Block 2 -- Investment Streams
    "investment_streams": [
        {
            "name": "Salary SIP",
            "amount": 150_000,             # per month
            "start_date": TODAY,
            "end_date_mode": "At retirement",
            "end_date": None,
            "step_up_percent": 8.0,
            "step_up_frequency": "Annual",
            "step_up_date": TODAY,
        }
    ],
    # Block 3 -- Goals
    "goals": [
        {
            "name": "Retirement Income", "description": "monthly living expense",
            "type": "Non-Negotiable", "nature": "Replenishing", "structure": "Recurring",
            "start_date_mode": "At retirement", "start_date": None,
            "amount": 100_000, "frequency": "Monthly", "occurrences": None,
            "end_mode": "Lifetime", "end_date": None, "inflation_percent": 6.0,
        },
        {
            "name": "Child Education", "description": "college fees",
            "type": "Non-Negotiable", "nature": "Non-replenishing", "structure": "Lumpsum",
            "start_date_mode": "Fixed", "start_date": pd.Timestamp("2036-06-01"),
            "amount": 4_000_000, "frequency": None, "occurrences": None,
            "end_mode": None, "end_date": None, "inflation_percent": 8.0,
        },
    ],
    # Block 4 -- One-time Investments
    "one_time_investments": [
        {"name": "Bonus", "date": pd.Timestamp("2027-03-01"), "amount": 1_000_000},
    ],
}


def main():
    print(f"engine  : {ENGINE_SOURCE_SHA}")
    print(f"glide   : version {GLIDEPATH_VERSION} (checked-in literals)")

    glide_paths = get_glide_paths()

    # Step 1 -- earliest feasible retirement date (validates + binary-searches).
    solved = find_retirement_date(CONFIG, instrument_params=None, glide_paths=glide_paths)
    if not solved["success"]:
        print("NOT FUNDABLE within the target lifetime:")
        print(solved["failure"])
        return 1
    retirement = solved["retirement_date"]
    years = (retirement - TODAY).days / 365.25
    print(f"\nEarliest feasible retirement: {retirement:%b %Y} "
          f"(age ~{38 + years:.1f})")

    # Step 2 -- full deterministic simulation at that date. Unlike
    # find_retirement_date, run_simulation does NOT default instrument_params
    # -- pass the engine defaults (or your own per-bucket return/tax dict).
    success, trans_df, failure, pools_df, goal_dfs, comp_df = run_simulation(
        CONFIG, retirement, _DEFAULT_INSTRUMENT_PARAMS, glide_paths=glide_paths
    )
    assert success, failure

    # Month-by-month wealth: sum every '*Value' column of the comprehensive view
    # (the same detection rule service._build_wealth_monthly uses).
    df = comp_df.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    value_cols = [c for c in df.columns if c.endswith("Value")]
    total = df[value_cols].fillna(0).sum(axis=1)
    at_ret = (df["Date"] - retirement).abs().idxmin()
    print(f"Total wealth at retirement : Rs {total.loc[at_ret]:,.0f}")
    print(f"Total wealth at lifetime end: Rs {total.iloc[-1]:,.0f}")
    print(f"Comprehensive view: {df.shape[0]} months x {df.shape[1]} columns")
    print(f"Non-replenishing goal frames: {list(goal_dfs.keys())}")
    print("\nOK -- the engine ran standalone (no DB, no app config).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
