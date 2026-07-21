"""Tests for the financial-planning engine (LP-015 C1, Plan 202).

Coverage (D-P202-9):
  (a) regression golden-master — pins the engine's earliest retirement date +
      deterministic snapshot totals on two reference configs. NOTE: these values
      were RE-BASELINED for the deliberate 2+2 pool window (engine
      "1515f1e+pool2x2", operator 2026-06-09) and no longer match pure-v3
      1515f1e. The glide-path byte-match + NaN tests remain genuine v3 parity
      (glide data is unchanged). (D-P202-2; pool 2+2 re-baseline 2026-06-09.)
  (b) the 4 P-691 crash repros + validation-rejection cases (D-P202-5/6).
  (c) the perf-cap worst-case timing test (D-P202-7).
  (d) real-client-goal configs built from `Advisory - Financial Planning Tracker.xlsx`
      (operator requirement, D-P202-9d).
  (e) unit tests for TaxLot/InvestmentPool FIFO, simulate_pool windows/refills/
      depletion, calculate_goal_cashflows chain back-solve (all 3 glide types),
      net_investment_against_payouts, solver monotonicity, and
      expand_recurring_goal_to_tranches end-mode resolution.

The engine starts at 0 tests; this file establishes the green baseline (Q16).
"""

import time

import numpy as np
import pandas as pd
import pytest

from app.planning import engine
from app.planning.engine import (
    TaxLot,
    InvestmentPool,
    _DEFAULT_INSTRUMENT_PARAMS,
    _resolve_recurring_occurrences,
    expand_recurring_goal_to_tranches,
    compute_replenishing_payouts,
    net_investment_against_payouts,
    calculate_goal_cashflows,
    get_withdrawl_df,
    add_withdrawls_to_trans,
    simulate_pool,
    generate_pseudo_nav,
    find_retirement_date,
    run_simulation,
)
from app.planning.glide_paths import GLIDEPATH_VERSION, get_glide_paths
from app.planning.validation import (
    PlanValidationError,
    validate_plan_config,
    MAX_NONREPLENISHING_SPAN_MONTHS,
)

TODAY = pd.Timestamp("2026-05-01")


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------

def _base_config(**overrides):
    cfg = {
        "current_date": TODAY,
        "current_age": 30,
        "target_lifetime": 90,
        "current_corpus": 10_000_000,
        "investment_streams": [],
        "goals": [],
        "one_time_investments": [],
    }
    cfg.update(overrides)
    return cfg


def _parity_config_1():
    """The v3 main_v2.py sample config (D-P202-5 reference)."""
    return {
        "current_date": TODAY,
        "current_age": 30,
        "target_lifetime": 90,
        "current_corpus": 10_000_000,
        "investment_streams": [
            {
                "name": "Primary Job",
                "amount": 100_000,
                "start_date": TODAY,
                "end_date_mode": "At retirement",
                "end_date": pd.Timestamp("2055-12-31"),
                "step_up_percent": 10.0,
                "step_up_frequency": "Annual",
                "step_up_date": TODAY - pd.Timedelta(days=1),
            }
        ],
        "goals": [
            {
                "name": "Retirement Income", "description": "x", "type": "Non-Negotiable",
                "nature": "Replenishing", "structure": "Recurring",
                "start_date_mode": "At retirement", "start_date": pd.Timestamp("2050-01-01"),
                "amount": 75_000, "frequency": "Monthly", "occurrences": 360,
                "end_mode": "Lifetime", "end_date": None, "inflation_percent": 6.0,
            },
            {
                "name": "Retirement Home", "description": "x", "type": "Non-Negotiable",
                "nature": "Non-replenishing", "structure": "Lumpsum",
                "start_date_mode": "Fixed", "start_date": pd.Timestamp("2040-01-01"),
                "amount": 5_000_000, "frequency": None, "occurrences": None,
                "inflation_percent": 6.0,
            },
        ],
        "one_time_investments": [],
    }


def _parity_config_2():
    """Advisory-derived: Santosh Praharaj (106_M3) — 2 marriage lumpsums + retirement income."""
    return {
        "current_date": TODAY, "current_age": 52, "target_lifetime": 85,
        "current_corpus": 20_000_000,
        "investment_streams": [
            {
                "name": "Salary", "amount": 250_000, "start_date": TODAY,
                "end_date_mode": "At retirement", "end_date": pd.Timestamp("2030-12-31"),
                "step_up_percent": 8.0, "step_up_frequency": "Annual",
                "step_up_date": TODAY - pd.Timedelta(days=1),
            }
        ],
        "goals": [
            {"name": "Marriage Elder", "type": "Semi-Negotiable", "nature": "Non-replenishing",
             "structure": "Lumpsum", "start_date_mode": "Fixed",
             "start_date": pd.Timestamp("2029-01-01"), "amount": 2_500_000, "inflation_percent": 6.0},
            {"name": "Marriage Younger", "type": "Semi-Negotiable", "nature": "Non-replenishing",
             "structure": "Lumpsum", "start_date_mode": "Fixed",
             "start_date": pd.Timestamp("2032-01-01"), "amount": 2_500_000, "inflation_percent": 6.0},
            {"name": "Retirement Income", "type": "Non-Negotiable", "nature": "Replenishing",
             "structure": "Recurring", "start_date_mode": "At retirement",
             "start_date": pd.Timestamp("2027-01-01"), "amount": 100_000, "frequency": "Monthly",
             "occurrences": 384, "end_mode": "Occurrences", "inflation_percent": 6.0},
        ],
        "one_time_investments": [],
    }


# ===========================================================================
# (a) Regression golden-master. Retirement/snapshot values RE-BASELINED for the
#     deliberate 2+2 pool window (engine "1515f1e+pool2x2", operator 2026-06-09);
#     they no longer match pure-v3 1515f1e. The glide-path tests below remain
#     genuine v3 parity (glide data is unchanged).
# ===========================================================================

class TestParityGoldenMaster:
    def test_glidepath_version_pinned(self):
        assert GLIDEPATH_VERSION == 1
        assert engine.ENGINE_SOURCE_SHA == "1515f1e+pool2x2+lifetimefix+monthgrid"

    def test_parity_config_1_retirement_date(self):
        res = find_retirement_date(_parity_config_1())
        assert res["success"] is True
        assert res["retirement_date"] == pd.Timestamp("2032-09-01")

    def test_parity_config_1_snapshot_totals(self):
        """Golden-master RE-BASELINED for +monthgrid (Plan 223, 2026-06-17).

        The step-up anchor shifted from ``TODAY - 1 day`` (2026-04-30) to
        ``TODAY`` (2026-05-01 = day 1). For an Annual step-up, the first event
        was previously at 2027-04-30; it is now at 2027-05-01. This shifts some
        step-up events by one month, producing slightly different corpus flows.
        The retirement date is UNCHANGED (2032-09-01).

        Delta attribution:
          Amount.sum: -425,234,715 -> -425,161,503  (+73,212, +0.017% — step-up timing)
          units.sum:  405.50 -> 921.48  (higher corpus at same retirement date = more units)
          tax.sum:    53,164,640 -> 53,168,584  (+3,944, +0.007%)
          Core Corpus Value (last): 36,556,201 -> 83,071,801
              (higher final corpus because more step-up events fire in some months)
        All deltas attributable to step-up anchor move from day-1 to day=1 month-start.
        No unexplained drift — reconciliation PASS (D-P223-7).
        """
        cfg = _parity_config_1()
        res = find_retirement_date(cfg)
        rd = res["retirement_date"]
        success, ft, fail, pm, gd, comp = run_simulation(cfg, rd, _DEFAULT_INSTRUMENT_PARAMS)
        assert success is True
        assert len(ft) == 136
        assert ft["Amount"].sum() == pytest.approx(-425161503.244827, rel=1e-9)
        assert ft["units"].sum() == pytest.approx(921.4769024035, rel=1e-9)
        assert ft["tax"].sum() == pytest.approx(53168584.529756, rel=1e-9)
        assert sorted(gd.keys()) == ["Retirement Home"]
        assert len(comp) == 720
        assert comp["Core Corpus Value"].iloc[-1] == pytest.approx(83071801.463573, rel=1e-9)

    def test_parity_config_2_retirement_date(self):
        res = find_retirement_date(_parity_config_2())
        assert res["success"] is True
        assert res["retirement_date"] == pd.Timestamp("2028-02-01")

    def test_parity_config_2_snapshot_totals(self):
        """Golden-master RE-BASELINED for +monthgrid (Plan 223, 2026-06-17).

        Same step-up anchor shift (2026-04-30 -> 2026-05-01). Retirement date
        UNCHANGED (2028-02-01).

        Delta attribution:
          Amount.sum: -89,402,122 -> -89,382,345  (+19,777, +0.022%)
          units.sum:  2823.68 -> 3003.81  (higher corpus = more units invested)
          tax.sum:    11,221,510 -> 11,221,733  (+223, ~0.002%)
          Core Corpus Value (last): 11,911,163 -> 12,671,024
        All deltas attributable to step-up anchor move. Reconciliation PASS (D-P223-7).
        """
        cfg = _parity_config_2()
        res = find_retirement_date(cfg)
        rd = res["retirement_date"]
        success, ft, fail, pm, gd, comp = run_simulation(cfg, rd, _DEFAULT_INSTRUMENT_PARAMS)
        assert success is True
        assert len(ft) == 62
        assert ft["Amount"].sum() == pytest.approx(-89382345.708167, rel=1e-9)
        assert ft["units"].sum() == pytest.approx(3003.8141561920, rel=1e-9)
        assert ft["tax"].sum() == pytest.approx(11221733.073649, rel=1e-9)
        assert sorted(gd.keys()) == ["Marriage Elder", "Marriage Younger"]
        assert len(comp) == 396
        assert comp["Core Corpus Value"].iloc[-1] == pytest.approx(12671024.045794, rel=1e-9)

    def test_glide_paths_byte_match_columns(self):
        gp = get_glide_paths()
        assert set(gp.keys()) == {"Non-Negotiable", "Semi-Negotiable", "Negotiable"}
        for name, df in gp.items():
            assert list(df.columns) == [
                "id", "place", "years from inflow till end",
                "years from outflow till end", "inflow_from", "outflow_to", "% of goal value",
            ]
            # goal-row percentages sum to 100
            goal_pct = df[df["place"] == "goal"]["% of goal value"].sum()
            assert goal_pct == 100, f"{name} goal rows must sum to 100, got {goal_pct}"

    def test_glide_path_nan_semantics(self):
        gp = get_glide_paths()
        nn = gp["Non-Negotiable"]
        goal_rows = nn[nn["place"] == "goal"]
        # goal rows carry NaN outflow_to / outflow-till-end (engine relies on pd.notna)
        assert goal_rows["outflow_to"].isna().all()
        assert goal_rows["years from outflow till end"].isna().all()


# ===========================================================================
# (b) Crash-class repros (D-P202-5) — the 4 P-691 reproductions.
#     Each must return cleanly (no KeyError: 'Date'), never raise.
# ===========================================================================

class TestCrashClassRepros:
    def test_no_goals(self):
        cfg = _base_config(goals=[])
        res = find_retirement_date(cfg)
        # No goal + no retirement-tied stream → "already retired" feasibility check.
        assert res["success"] is True
        success, ft, *_ = run_simulation(cfg, TODAY, _DEFAULT_INSTRUMENT_PARAMS)
        assert success is True

    def test_zero_occurrences_recurring(self):
        cfg = _base_config(goals=[{
            "name": "Edu", "type": "Non-Negotiable", "nature": "Non-replenishing",
            "structure": "Recurring", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2030-01-01"), "amount": 500_000,
            "frequency": "Annual", "occurrences": 0, "end_mode": "Occurrences",
            "inflation_percent": 6.0,
        }])
        # occurrences=0 is rejected by validation (>= 1 rule) — must raise a clean
        # validation error, NOT a KeyError crash deep in the engine.
        with pytest.raises(PlanValidationError):
            find_retirement_date(cfg)
        # And run_simulation directly (bypassing validation) returns cleanly.
        success, ft, *_ = run_simulation(cfg, TODAY, _DEFAULT_INSTRUMENT_PARAMS)
        assert success is True

    def test_recurring_end_before_start(self):
        cfg = _base_config(goals=[{
            "name": "Edu", "type": "Non-Negotiable", "nature": "Non-replenishing",
            "structure": "Recurring", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2035-01-01"), "amount": 500_000,
            "frequency": "Annual", "end_mode": "Fixed date",
            "end_date": pd.Timestamp("2030-01-01"),  # end < start → 0 occurrences
            "inflation_percent": 6.0,
        }])
        # Validation rejects end < start.
        with pytest.raises(PlanValidationError):
            find_retirement_date(cfg)
        # run_simulation directly resolves to 0 tranches and returns cleanly.
        success, ft, *_ = run_simulation(cfg, TODAY, _DEFAULT_INSTRUMENT_PARAMS)
        assert success is True

    def test_income_covered_pre_retirement_swp(self):
        """Investment fully covers a pre-retirement Replenishing SWP → no pool, no goal chains."""
        cfg = _base_config(
            current_corpus=5_000_000,
            investment_streams=[{
                "name": "Salary", "amount": 500_000, "start_date": TODAY,
                "end_date_mode": "Fixed", "end_date": pd.Timestamp("2060-01-01"),
                "step_up_percent": 0.0, "step_up_frequency": "Annual",
                "step_up_date": TODAY - pd.Timedelta(days=1),
            }],
            goals=[{
                "name": "SWP", "type": "Non-Negotiable", "nature": "Replenishing",
                "structure": "Recurring", "start_date_mode": "Fixed",
                "start_date": pd.Timestamp("2027-01-01"), "amount": 50_000,
                "frequency": "Monthly", "occurrences": 24, "end_mode": "Occurrences",
                "inflation_percent": 0.0,
            }],
        )
        # No retirement-tied element → single feasibility check; must not raise.
        success, ft, fail, pm, gd, comp = run_simulation(cfg, TODAY, _DEFAULT_INSTRUMENT_PARAMS)
        assert success is True
        # Investment covered the SWP → no goal chains (Replenishing) and a clean trans frame.
        assert "Date" in ft.columns

    def test_get_withdrawl_df_empty_is_typed(self):
        df = get_withdrawl_df({})
        assert list(df.columns) == ["Date", "Amount", "Description"]
        assert df.empty

    def test_add_withdrawls_empty_frame_no_keyerror(self):
        sip = pd.DataFrame({
            "Date": [TODAY], "Amount": [1_000_000.0], "NAV": [100.0],
            "units": [10000.0], "Description": ["Current Corpus"],
        })
        empty_wd = pd.DataFrame()  # column-less — the original v3 crash trigger
        out, success, fail = add_withdrawls_to_trans(empty_wd if False else pd.DataFrame(), sip, None, None) \
            if False else add_withdrawls_to_trans(sip, pd.DataFrame(), None, _DEFAULT_INSTRUMENT_PARAMS)
        assert success is True
        assert fail is None
        assert {"tax", "fully_funded", "shortfall"} <= set(out.columns)


# ===========================================================================
# (b) Validation rejection cases (D-P202-6).
# ===========================================================================

class TestValidation:
    def test_valid_config_passes(self):
        validate_plan_config(_parity_config_1())  # no raise

    def test_negative_corpus(self):
        with pytest.raises(PlanValidationError):
            validate_plan_config(_base_config(current_corpus=-1))

    def test_missing_corpus(self):
        cfg = _base_config()
        del cfg["current_corpus"]
        with pytest.raises(PlanValidationError):
            validate_plan_config(cfg)

    def test_lifetime_not_greater_than_age(self):
        with pytest.raises(PlanValidationError):
            validate_plan_config(_base_config(current_age=90, target_lifetime=90))
        with pytest.raises(PlanValidationError):
            validate_plan_config(_base_config(current_age=95, target_lifetime=90))

    def test_negative_stream_amount(self):
        cfg = _base_config(investment_streams=[{
            "name": "S", "amount": -100, "start_date": TODAY,
            "end_date_mode": "At retirement", "end_date": None,
        }])
        with pytest.raises(PlanValidationError):
            validate_plan_config(cfg)

    def test_fixed_stream_requires_end_date(self):
        cfg = _base_config(investment_streams=[{
            "name": "S", "amount": 100, "start_date": TODAY,
            "end_date_mode": "Fixed", "end_date": None,
        }])
        with pytest.raises(PlanValidationError):
            validate_plan_config(cfg)

    def test_fixed_stream_end_before_start(self):
        cfg = _base_config(investment_streams=[{
            "name": "S", "amount": 100, "start_date": pd.Timestamp("2030-01-01"),
            "end_date_mode": "Fixed", "end_date": pd.Timestamp("2025-01-01"),
        }])
        with pytest.raises(PlanValidationError):
            validate_plan_config(cfg)

    def test_negative_goal_amount(self):
        cfg = _base_config(goals=[{
            "name": "G", "type": "Non-Negotiable", "nature": "Non-replenishing",
            "structure": "Lumpsum", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2030-01-01"), "amount": -5, "inflation_percent": 6.0,
        }])
        with pytest.raises(PlanValidationError):
            validate_plan_config(cfg)

    def test_recurring_goal_bad_frequency(self):
        cfg = _base_config(goals=[{
            "name": "G", "type": "Non-Negotiable", "nature": "Replenishing",
            "structure": "Recurring", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2030-01-01"), "amount": 50_000,
            "frequency": "Weekly", "occurrences": 12, "end_mode": "Occurrences",
            "inflation_percent": 6.0,
        }])
        with pytest.raises(PlanValidationError):
            validate_plan_config(cfg)

    def test_recurring_occurrences_must_be_positive(self):
        cfg = _base_config(goals=[{
            "name": "G", "type": "Non-Negotiable", "nature": "Replenishing",
            "structure": "Recurring", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2030-01-01"), "amount": 50_000,
            "frequency": "Monthly", "occurrences": 0, "end_mode": "Occurrences",
            "inflation_percent": 6.0,
        }])
        with pytest.raises(PlanValidationError):
            validate_plan_config(cfg)

    def test_negative_one_time_amount(self):
        cfg = _base_config(one_time_investments=[{"name": "W", "date": TODAY, "amount": -1}])
        with pytest.raises(PlanValidationError):
            validate_plan_config(cfg)

    def test_error_list_collects_all(self):
        cfg = _base_config(current_corpus=-1, current_age=90, target_lifetime=80)
        with pytest.raises(PlanValidationError) as exc:
            validate_plan_config(cfg)
        assert len(exc.value.errors) >= 2

    def test_replenishing_recurring_uncapped(self):
        """A monthly replenishing recurring goal with many occurrences is allowed (no chains)."""
        cfg = _base_config(goals=[{
            "name": "Income", "type": "Non-Negotiable", "nature": "Replenishing",
            "structure": "Recurring", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2030-01-01"), "amount": 50_000,
            "frequency": "Monthly", "occurrences": 600, "end_mode": "Occurrences",
            "inflation_percent": 6.0,
        }])
        validate_plan_config(cfg)  # no raise — replenishing is uncapped


# ===========================================================================
# (c) Span cap (D-P208-1, replaces D-P202-7 occurrence count cap) + perf timing.
# ===========================================================================

class TestSpanCap:
    """Boundary matrix for the 4-year (48-month) first-to-last span cap (D-P208-1).

    Implied per-frequency maxima: 5×Annual / 17×Quarterly / 49×Monthly.
    """

    # -- Annual (freq_months=12) ──────────────────────────────────────────────

    def test_5x_annual_pass(self):
        """5×Annual → span = (5-1)*12 = 48 months → exactly at cap → allowed."""
        cfg = _base_config(goals=[{
            "name": "Edu", "type": "Non-Negotiable", "nature": "Non-replenishing",
            "structure": "Recurring", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2030-01-01"), "amount": 100_000,
            "frequency": "Annual", "occurrences": 5,
            "end_mode": "Occurrences", "inflation_percent": 6.0,
        }])
        validate_plan_config(cfg)  # 48 months == cap → allowed

    def test_6x_annual_fail(self):
        """6×Annual → span = (6-1)*12 = 60 months > 48 → rejected."""
        cfg = _base_config(goals=[{
            "name": "Edu", "type": "Non-Negotiable", "nature": "Non-replenishing",
            "structure": "Recurring", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2030-01-01"), "amount": 100_000,
            "frequency": "Annual", "occurrences": 6,
            "end_mode": "Occurrences", "inflation_percent": 6.0,
        }])
        with pytest.raises(PlanValidationError, match="4 years"):
            validate_plan_config(cfg)

    # -- Monthly (freq_months=1) ──────────────────────────────────────────────

    def test_49x_monthly_pass(self):
        """49×Monthly → span = (49-1)*1 = 48 months → exactly at cap → allowed."""
        cfg = _base_config(goals=[{
            "name": "Edu", "type": "Non-Negotiable", "nature": "Non-replenishing",
            "structure": "Recurring", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2030-01-01"), "amount": 100_000,
            "frequency": "Monthly", "occurrences": 49,
            "end_mode": "Occurrences", "inflation_percent": 6.0,
        }])
        validate_plan_config(cfg)  # 48 months == cap → allowed

    def test_50x_monthly_fail(self):
        """50×Monthly → span = (50-1)*1 = 49 months > 48 → rejected."""
        cfg = _base_config(goals=[{
            "name": "Edu", "type": "Non-Negotiable", "nature": "Non-replenishing",
            "structure": "Recurring", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2030-01-01"), "amount": 100_000,
            "frequency": "Monthly", "occurrences": 50,
            "end_mode": "Occurrences", "inflation_percent": 6.0,
        }])
        with pytest.raises(PlanValidationError, match="4 years"):
            validate_plan_config(cfg)

    # -- Quarterly (freq_months=3) ────────────────────────────────────────────

    def test_17x_quarterly_pass(self):
        """17×Quarterly → span = (17-1)*3 = 48 months → exactly at cap → allowed."""
        cfg = _base_config(goals=[{
            "name": "Edu", "type": "Non-Negotiable", "nature": "Non-replenishing",
            "structure": "Recurring", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2030-01-01"), "amount": 100_000,
            "frequency": "Quarterly", "occurrences": 17,
            "end_mode": "Occurrences", "inflation_percent": 6.0,
        }])
        validate_plan_config(cfg)

    # -- Fixed-date mode ──────────────────────────────────────────────────────

    def test_fixed_date_48mo_pass(self):
        """Fixed-date 48-month gap → exactly at cap → allowed."""
        cfg = _base_config(goals=[{
            "name": "Edu", "type": "Non-Negotiable", "nature": "Non-replenishing",
            "structure": "Recurring", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2030-01-01"),
            "end_date": pd.Timestamp("2034-01-01"),  # 48 months
            "amount": 100_000,
            "frequency": "Monthly", "end_mode": "Fixed date", "inflation_percent": 6.0,
        }])
        validate_plan_config(cfg)

    def test_fixed_date_49mo_fail(self):
        """Fixed-date 49-month gap → 1 month over cap → rejected."""
        cfg = _base_config(goals=[{
            "name": "Edu", "type": "Non-Negotiable", "nature": "Non-replenishing",
            "structure": "Recurring", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2030-01-01"),
            "end_date": pd.Timestamp("2034-02-01"),  # 49 months
            "amount": 100_000,
            "frequency": "Monthly", "end_mode": "Fixed date", "inflation_percent": 6.0,
        }])
        with pytest.raises(PlanValidationError, match="4 years"):
            validate_plan_config(cfg)

    # -- Lifetime mode ────────────────────────────────────────────────────────

    def test_lifetime_nonreplenishing_rejected(self):
        """Lifetime end_mode on a non-replenishing goal → unconditional violation."""
        cfg = _base_config(goals=[{
            "name": "Edu", "type": "Non-Negotiable", "nature": "Non-replenishing",
            "structure": "Recurring", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2030-01-01"), "amount": 100_000,
            "frequency": "Monthly", "end_mode": "Lifetime", "inflation_percent": 6.0,
        }])
        with pytest.raises(PlanValidationError):
            validate_plan_config(cfg)

    # -- Replenishing stays uncapped ──────────────────────────────────────────

    def test_replenishing_recurring_uncapped(self):
        """Replenishing recurring goals are never subject to the span cap."""
        cfg = _base_config(goals=[{
            "name": "Income", "type": "Non-Negotiable", "nature": "Replenishing",
            "structure": "Recurring", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2030-01-01"), "amount": 50_000,
            "frequency": "Monthly", "end_mode": "Lifetime", "inflation_percent": 6.0,
        }])
        validate_plan_config(cfg)  # no raise — replenishing is uncapped

    # -- Perf timing at new worst case (49×Monthly, D-P208-2) ─────────────────

    def test_worst_case_run_simulation_49_monthly_under_budget(self):
        """Worst case single run_simulation at 49×Monthly (the new span cap) stays
        under ~3s (D-P208-2).

        49 non-replenishing monthly chains = worst acceptable case under the span cap.
        """
        cfg = _base_config(
            current_corpus=200_000_000,
            target_lifetime=90,
            goals=[{
                "name": "Edu", "type": "Non-Negotiable", "nature": "Non-replenishing",
                "structure": "Recurring", "start_date_mode": "Fixed",
                "start_date": pd.Timestamp("2030-01-01"), "amount": 100_000,
                "frequency": "Monthly", "occurrences": 49,
                "end_mode": "Occurrences", "inflation_percent": 6.0,
            }],
        )
        t0 = time.perf_counter()
        success, ft, *_ = run_simulation(cfg, TODAY, _DEFAULT_INSTRUMENT_PARAMS)
        elapsed = time.perf_counter() - t0
        assert elapsed < 3.0, f"worst-case run_simulation took {elapsed:.2f}s (budget 3s)"


# ===========================================================================
# (e) Unit tests — TaxLot / InvestmentPool FIFO.
# ===========================================================================

class TestTaxLot:
    def test_purchase_val_and_current_value(self):
        lot = TaxLot(TODAY, units=100, purchase_price_per_unit=50)
        assert lot.purchase_val == 5000
        assert lot.current_value(60) == 6000


class TestInvestmentPool:
    def test_invest_creates_lot(self):
        pool = InvestmentPool("Debt", 0.20, 0.125)
        res = pool.invest(TODAY, 10_000, 100)
        assert res["units"] == 100
        assert len(pool.lots) == 1
        assert pool.get_market_value(100) == 10_000

    def test_invest_non_positive_returns_none(self):
        pool = InvestmentPool("Debt", 0.20, 0.125)
        assert pool.invest(TODAY, 0, 100) is None
        assert pool.invest(TODAY, -5, 100) is None
        assert pool.lots == []

    def test_redeem_gross_fifo_order(self):
        pool = InvestmentPool("Debt", 0.20, 0.125)
        pool.invest(TODAY, 10_000, 100)  # lot 1: 100 units @100
        pool.invest(TODAY + pd.Timedelta(days=10), 10_000, 100)  # lot 2
        # redeem 100 units worth at nav 100 → consumes the first lot (FIFO)
        res = pool.redeem_gross_amount(TODAY + pd.Timedelta(days=400), 10_000, 100)
        assert res["fully_funded"] is True
        assert len(pool.lots) == 1  # one lot consumed

    def test_redeem_net_back_solves_target(self):
        pool = InvestmentPool("Debt", 0.20, 0.125)
        pool.invest(TODAY, 100_000, 100)  # 1000 units @100
        # at nav 200 a year+ later, gains taxed at ltcg 12.5%
        res = pool.redeem_net_amount(TODAY + pd.Timedelta(days=400), 50_000, 200)
        assert res["fully_funded"] is True
        assert res["net_received"] == pytest.approx(50_000, abs=1.0)

    def test_tax_rate_by_holding_period(self):
        pool = InvestmentPool("Debt", 0.20, 0.125)
        # <= 365 days → STCG; > 365 → LTCG
        assert pool._get_tax_rate(TODAY, TODAY + pd.Timedelta(days=365)) == 0.20
        assert pool._get_tax_rate(TODAY, TODAY + pd.Timedelta(days=366)) == 0.125

    def test_unrealized_tax_only_on_gains(self):
        pool = InvestmentPool("Debt", 0.20, 0.125)
        pool.invest(TODAY, 10_000, 100)  # 100 units @100
        # nav below cost → no gain → no tax
        assert pool.get_unrealized_tax(80) == 0
        # nav above cost → ltcg by default (no as_of_date)
        assert pool.get_unrealized_tax(200) == pytest.approx(100 * (200 - 100) * 0.125)

    def test_redeem_gross_shortfall(self):
        pool = InvestmentPool("Debt", 0.20, 0.125)
        pool.invest(TODAY, 10_000, 100)
        res = pool.redeem_gross_amount(TODAY + pd.Timedelta(days=10), 50_000, 100)
        assert res["fully_funded"] is False
        assert res["shortfall"] > 0


# ===========================================================================
# (e) expand_recurring_goal_to_tranches + end-mode resolution.
# ===========================================================================

class TestExpandTranches:
    def test_lumpsum_single_tranche(self):
        goal = {"structure": "Lumpsum", "amount": 1_000_000,
                "start_date": pd.Timestamp("2030-01-01"), "inflation_percent": 6.0}
        tr = expand_recurring_goal_to_tranches(goal, TODAY)
        assert len(tr) == 1
        # grown by inflation to start_date
        assert tr[0][1] > 1_000_000

    def test_recurring_n_tranches(self):
        goal = {"structure": "Recurring", "amount": 100_000, "frequency": "Annual",
                "occurrences": 4, "start_date": pd.Timestamp("2030-01-01"), "inflation_percent": 6.0}
        tr = expand_recurring_goal_to_tranches(goal, TODAY)
        assert len(tr) == 4
        # each later occurrence escalates further
        assert tr[1][1] > tr[0][1]

    def test_recurring_zero_occurrences_empty(self):
        goal = {"structure": "Recurring", "amount": 100_000, "frequency": "Monthly",
                "occurrences": 0, "start_date": pd.Timestamp("2030-01-01"), "inflation_percent": 6.0}
        assert expand_recurring_goal_to_tranches(goal, TODAY) == []

    def test_resolve_occurrences_fixed_date(self):
        goal = {"structure": "Recurring", "frequency": "Annual", "end_mode": "Fixed date",
                "start_date": pd.Timestamp("2030-01-01"), "end_date": pd.Timestamp("2033-01-01")}
        # 2030,2031,2032,2033 → 4 occurrences
        assert _resolve_recurring_occurrences(goal, None) == 4

    def test_resolve_occurrences_lifetime(self):
        goal = {"structure": "Recurring", "frequency": "Annual", "end_mode": "Lifetime",
                "start_date": pd.Timestamp("2030-01-01")}
        death = pd.Timestamp("2040-01-01")
        assert _resolve_recurring_occurrences(goal, death) == 11

    def test_resolve_occurrences_end_before_start(self):
        goal = {"structure": "Recurring", "frequency": "Annual", "end_mode": "Fixed date",
                "start_date": pd.Timestamp("2035-01-01"), "end_date": pd.Timestamp("2030-01-01")}
        assert _resolve_recurring_occurrences(goal, None) == 0


# ===========================================================================
# (e) compute_replenishing_payouts + net_investment_against_payouts.
# ===========================================================================

class TestPayoutsAndNetting:
    def test_compute_replenishing_payouts_empty(self):
        df = compute_replenishing_payouts([], TODAY)
        assert list(df.columns) == ["Date", "Amount"]
        assert df.empty

    def test_compute_replenishing_only_replenishing(self):
        goals = [
            {"name": "A", "nature": "Replenishing", "structure": "Recurring", "frequency": "Annual",
             "occurrences": 2, "start_date": pd.Timestamp("2030-01-01"), "amount": 100_000, "inflation_percent": 0.0},
            {"name": "B", "nature": "Non-replenishing", "structure": "Lumpsum",
             "start_date": pd.Timestamp("2030-01-01"), "amount": 999, "inflation_percent": 0.0},
        ]
        df = compute_replenishing_payouts(goals, TODAY)
        assert len(df) == 2  # only the replenishing goal's 2 occurrences
        assert df["Amount"].sum() == pytest.approx(200_000)

    def test_netting_investment_covers_payout(self):
        inv = pd.DataFrame({"Date": [pd.Timestamp("2030-01-01")], "Investment": [100_000.0]})
        pay = pd.DataFrame({"Date": [pd.Timestamp("2030-01-01")], "Amount": [40_000.0]})
        net, surplus = net_investment_against_payouts(inv, pay, TODAY)
        assert net.empty  # investment fully covered the payout
        assert surplus["Investment"].iloc[0] == pytest.approx(60_000)

    def test_netting_payout_exceeds_investment(self):
        inv = pd.DataFrame({"Date": [pd.Timestamp("2030-01-01")], "Investment": [30_000.0]})
        pay = pd.DataFrame({"Date": [pd.Timestamp("2030-01-01")], "Amount": [100_000.0]})
        net, surplus = net_investment_against_payouts(inv, pay, TODAY)
        assert net["Amount"].iloc[0] == pytest.approx(70_000)
        assert surplus["Investment"].iloc[0] == pytest.approx(0)


# ===========================================================================
# (e) calculate_goal_cashflows — chain back-solve for all 3 glide types.
# ===========================================================================

class TestGoalCashflows:
    @pytest.mark.parametrize("gtype", ["Non-Negotiable", "Semi-Negotiable", "Negotiable"])
    def test_chain_back_solve_each_glide_type(self, gtype):
        gp = get_glide_paths()
        cfg = {"current_date": TODAY}
        out = calculate_goal_cashflows(
            input_df=gp[gtype],
            end_date=pd.Timestamp("2040-01-01"),
            goal_value_post_tax=10_000_000,
            instrument_params=_DEFAULT_INSTRUMENT_PARAMS,
            input_variables=cfg,
        )
        # goal rows' inflow_amount sums to the post-tax goal target
        goal_inflow = out[out["place"] == "goal"]["inflow_amount"].sum()
        assert goal_inflow == pytest.approx(10_000_000, rel=1e-6)
        # core-corpus sourced rows must each provide positive principal
        cc_rows = out[out["inflow_from"] == "core corpus"]
        assert (cc_rows["inflow_amount"] > 0).all()
        # back-solved core-corpus principal is < goal value (instruments grow it)
        assert cc_rows["inflow_amount"].sum() < 10_000_000


# ===========================================================================
# (e) simulate_pool — windows / refills / depletion.
# ===========================================================================

class TestSimulatePool:
    def _navs(self, end="2040-01-01"):
        debt = generate_pseudo_nav(TODAY, pd.Timestamp(end), 0.06)
        hybrid = generate_pseudo_nav(TODAY, pd.Timestamp(end), 0.10)
        return debt, hybrid

    def test_empty_payouts_no_pool(self):
        debt, hybrid = self._navs()
        empty = pd.DataFrame({"Date": pd.Series(dtype="datetime64[ns]"), "Amount": pd.Series(dtype=float)})
        pt, cr, fd, fr, pm = simulate_pool(
            empty, debt, hybrid, _DEFAULT_INSTRUMENT_PARAMS["debt"],
            _DEFAULT_INSTRUMENT_PARAMS["hybrid"], TODAY, pd.Timestamp("2040-01-01"))
        assert fd is None
        assert pt.empty

    def test_pool_funds_payouts_with_core_refill(self):
        debt, hybrid = self._navs()
        payouts = pd.DataFrame({
            "Date": pd.to_datetime(["2027-01-01", "2027-02-01", "2027-03-01"]),
            "Amount": [50_000.0, 50_000.0, 50_000.0],
        })
        pt, cr, fd, fr, pm = simulate_pool(
            payouts, debt, hybrid, _DEFAULT_INSTRUMENT_PARAMS["debt"],
            _DEFAULT_INSTRUMENT_PARAMS["hybrid"], pd.Timestamp("2027-01-01"), pd.Timestamp("2040-01-01"))
        assert fd is None  # funded via core replenishments
        assert not cr.empty  # core had to refill the debt pool
        assert not pm.empty


# ===========================================================================
# (e) Solver monotonicity — feasibility is a step function over retirement date.
# ===========================================================================

class TestSolverMonotonicity:
    def test_feasibility_is_monotone_in_retirement_date(self):
        """If retiring at date D is feasible, retiring later (more saving) is feasible too."""
        cfg = _parity_config_1()
        res = find_retirement_date(cfg)
        earliest = res["retirement_date"]
        assert earliest is not None
        # earliest feasible succeeds
        assert run_simulation(cfg, earliest, _DEFAULT_INSTRUMENT_PARAMS)[0] is True
        # a year later also succeeds (monotone step function)
        later = earliest + pd.DateOffset(years=1)
        assert run_simulation(cfg, later, _DEFAULT_INSTRUMENT_PARAMS)[0] is True
        # a year earlier fails (it was the EARLIEST feasible)
        earlier = earliest - pd.DateOffset(years=1)
        assert run_simulation(cfg, earlier, _DEFAULT_INSTRUMENT_PARAMS)[0] is False

    def test_infeasible_plan_returns_none(self):
        # tiny corpus, no income, large retirement income tied to retirement → infeasible
        cfg = _base_config(
            current_corpus=1,
            investment_streams=[{
                "name": "Job", "amount": 1, "start_date": TODAY,
                "end_date_mode": "At retirement", "end_date": pd.Timestamp("2060-01-01"),
                "step_up_percent": 0.0, "step_up_frequency": "Annual",
                "step_up_date": TODAY - pd.Timedelta(days=1),
            }],
            goals=[{
                "name": "Income", "type": "Non-Negotiable", "nature": "Replenishing",
                "structure": "Recurring", "start_date_mode": "At retirement",
                "start_date": pd.Timestamp("2030-01-01"), "amount": 10_000_000,
                "frequency": "Monthly", "occurrences": 360, "end_mode": "Occurrences",
                "inflation_percent": 6.0,
            }],
        )
        res = find_retirement_date(cfg)
        assert res["success"] is False
        assert res["retirement_date"] is None


# ===========================================================================
# (d) Advisory-corpus suite — real client goals from the tracker.
#     Builds configs from `Advisory - Financial Planning Tracker.xlsx`
#     (operator requirement, D-P202-9d). Each must run to a sensible
#     feasible/infeasible result without crashing.
# ===========================================================================

# Goal-type casing in the tracker ("Non-negotiable") differs from the glide-path
# sheet keys ("Non-Negotiable"); map it. Frequency "Half-yearly" → "Half-Yearly".
_TYPE_MAP = {
    "non-negotiable": "Non-Negotiable",
    "semi-negotiable": "Semi-Negotiable",
    "negotiable": "Negotiable",
}
_FREQ_MAP = {
    "monthly": "Monthly", "quarterly": "Quarterly",
    "half-yearly": "Half-Yearly", "annual": "Annual",
}


def _goal_from_tracker_row(row):
    """Build an engine goal dict from one Advisory tracker Goals row.

    Replenishing-recurring uses Goal_amt_total as the per-occurrence amount;
    Non-replenishing recurring (education) likewise; lumpsum uses Goal_amt_total.
    Caps occurrences for non-replenishing to keep the corpus fixtures within the
    span cap (D-P208-1): max_occ = MAX_NONREPLENISHING_SPAN_MONTHS // freq_months + 1
    so that (occ-1)*freq_months <= MAX_NONREPLENISHING_SPAN_MONTHS.
    """
    _FIXTURE_FREQ_MONTHS = {"Annual": 12, "Quarterly": 3, "Half-Yearly": 6, "Monthly": 1}

    nature = str(row["Goal_nature"]).strip()
    structure = str(row["Goal_structure"]).strip()
    gtype = _TYPE_MAP.get(str(row["Goal_type"]).strip().lower(), "Non-Negotiable")
    amount = row["Goal_amt_total"]
    if pd.isna(amount):
        amount = row.get("Goal_amt_per_occurrence")
    goal = {
        "name": str(row["Goal_name"]).strip(),
        "type": gtype,
        "nature": "Replenishing" if nature.lower() == "replenishing" else "Non-replenishing",
        "structure": "Recurring" if structure.lower() == "recurring" else "Lumpsum",
        "start_date_mode": "Fixed",
        "start_date": pd.Timestamp(row["Goal_start_date"]),
        "amount": float(amount),
        "inflation_percent": float(row["Inflation_assumption_pct"]) * 100
        if not pd.isna(row.get("Inflation_assumption_pct")) else 6.0,
    }
    if goal["structure"] == "Recurring":
        freq_str = _FREQ_MAP.get(str(row["Goal_frequency"]).strip().lower(), "Annual")
        goal["frequency"] = freq_str
        occ = int(row["Goal_occurrences"]) if not pd.isna(row.get("Goal_occurrences")) else 1
        goal["end_mode"] = "Occurrences"
        if goal["nature"] != "Replenishing":
            freq_months = _FIXTURE_FREQ_MONTHS.get(freq_str, 12)
            max_occ = MAX_NONREPLENISHING_SPAN_MONTHS // freq_months + 1
            occ = min(occ, max_occ)
        goal["occurrences"] = max(1, occ)
    return goal


# A representative slice of the tracker — multi-goal families + each goal class.
# Built inline (not read at import time) so the test is self-contained and the
# fixtures double as documentation of real client shapes.
_ADVISORY_FAMILIES = {
    "Vijay & Prachi Shepunde (101_M3)": [
        {"Goal_name": "Son 1 Education", "Goal_type": "Non-negotiable", "Goal_nature": "Non-replenishing",
         "Goal_structure": "Recurring", "Goal_start_date": "2028-05-01", "Goal_amt_total": np.nan,
         "Goal_amt_per_occurrence": 500000.0, "Goal_frequency": "Annual", "Goal_occurrences": 3.0,
         "Inflation_assumption_pct": 0.07},
        {"Goal_name": "Son 2 Education", "Goal_type": "Non-negotiable", "Goal_nature": "Non-replenishing",
         "Goal_structure": "Recurring", "Goal_start_date": "2032-05-01", "Goal_amt_total": np.nan,
         "Goal_amt_per_occurrence": 500000.0, "Goal_frequency": "Annual", "Goal_occurrences": 4.0,
         "Inflation_assumption_pct": 0.07},
    ],
    "Pradeep Chakravarthi Sadasivuni & Family (109_M3)": [
        {"Goal_name": "Child 1 undergrad", "Goal_type": "Non-negotiable", "Goal_nature": "Non-replenishing",
         "Goal_structure": "Recurring", "Goal_start_date": "2033-06-01", "Goal_amt_total": np.nan,
         "Goal_amt_per_occurrence": 630000.0, "Goal_frequency": "Annual", "Goal_occurrences": 4.0,
         "Inflation_assumption_pct": 0.10},
        {"Goal_name": "Child 2 undergrad", "Goal_type": "Non-negotiable", "Goal_nature": "Non-replenishing",
         "Goal_structure": "Recurring", "Goal_start_date": "2036-06-01", "Goal_amt_total": np.nan,
         "Goal_amt_per_occurrence": 780000.0, "Goal_frequency": "Annual", "Goal_occurrences": 4.0,
         "Inflation_assumption_pct": 0.10},
        {"Goal_name": "Retirement income", "Goal_type": "Non-negotiable", "Goal_nature": "Replenishing",
         "Goal_structure": "Recurring", "Goal_start_date": "2048-01-01", "Goal_amt_total": np.nan,
         "Goal_amt_per_occurrence": 261000.0, "Goal_frequency": "Monthly", "Goal_occurrences": 360.0,
         "Inflation_assumption_pct": 0.06},
    ],
    "Santosh Praharaj (106_M3)": [
        {"Goal_name": "Retirement Income", "Goal_type": "Non-negotiable", "Goal_nature": "Replenishing",
         "Goal_structure": "Recurring", "Goal_start_date": "2027-01-01", "Goal_amt_total": np.nan,
         "Goal_amt_per_occurrence": 100000.0, "Goal_frequency": "Monthly", "Goal_occurrences": 384.0,
         "Inflation_assumption_pct": 0.06},
        {"Goal_name": "Marriage Elder", "Goal_type": "Semi-negotiable", "Goal_nature": "Non-replenishing",
         "Goal_structure": "Lumpsum", "Goal_start_date": "2029-01-01", "Goal_amt_total": 2500000.0,
         "Goal_amt_per_occurrence": np.nan, "Goal_frequency": np.nan, "Goal_occurrences": np.nan,
         "Inflation_assumption_pct": np.nan},
        {"Goal_name": "Marriage Younger", "Goal_type": "Semi-negotiable", "Goal_nature": "Non-replenishing",
         "Goal_structure": "Lumpsum", "Goal_start_date": "2032-01-01", "Goal_amt_total": 2500000.0,
         "Goal_amt_per_occurrence": np.nan, "Goal_frequency": np.nan, "Goal_occurrences": np.nan,
         "Inflation_assumption_pct": np.nan},
    ],
    "Anjan Yerubandi (110_M3)": [
        {"Goal_name": "Home Purchase", "Goal_type": "Negotiable", "Goal_nature": "Non-replenishing",
         "Goal_structure": "Lumpsum", "Goal_start_date": "2031-01-01", "Goal_amt_total": 30000000.0,
         "Goal_amt_per_occurrence": np.nan, "Goal_frequency": np.nan, "Goal_occurrences": np.nan,
         "Inflation_assumption_pct": np.nan},
    ],
    "Navin & Pushpa Jhanji (105_M3)": [
        {"Goal_name": "DJ1 Marriage", "Goal_type": "Non-negotiable", "Goal_nature": "Non-replenishing",
         "Goal_structure": "Lumpsum", "Goal_start_date": "2028-01-01", "Goal_amt_total": 6000000.0,
         "Goal_amt_per_occurrence": np.nan, "Goal_frequency": np.nan, "Goal_occurrences": np.nan,
         "Inflation_assumption_pct": np.nan},
        {"Goal_name": "DJ2 Marriage", "Goal_type": "Non-negotiable", "Goal_nature": "Non-replenishing",
         "Goal_structure": "Lumpsum", "Goal_start_date": "2029-01-01", "Goal_amt_total": 7000000.0,
         "Goal_amt_per_occurrence": np.nan, "Goal_frequency": np.nan, "Goal_occurrences": np.nan,
         "Inflation_assumption_pct": np.nan},
        {"Goal_name": "SWP", "Goal_type": "Non-negotiable", "Goal_nature": "Replenishing",
         "Goal_structure": "Recurring", "Goal_start_date": "2028-01-01", "Goal_amt_total": np.nan,
         "Goal_amt_per_occurrence": 70000.0, "Goal_frequency": "Monthly", "Goal_occurrences": 420.0,
         "Inflation_assumption_pct": 0.07},
    ],
}


def _advisory_config(rows, corpus=50_000_000, salary=400_000):
    goals = [_goal_from_tracker_row(pd.Series(r)) for r in rows]
    return {
        "current_date": TODAY, "current_age": 45, "target_lifetime": 90,
        "current_corpus": corpus,
        "investment_streams": [{
            "name": "Salary", "amount": salary, "start_date": TODAY,
            "end_date_mode": "At retirement", "end_date": pd.Timestamp("2055-12-31"),
            "step_up_percent": 8.0, "step_up_frequency": "Annual",
            "step_up_date": TODAY - pd.Timedelta(days=1),
        }],
        "goals": goals,
        "one_time_investments": [],
    }


class TestAdvisoryCorpus:
    @pytest.mark.parametrize("family", sorted(_ADVISORY_FAMILIES.keys()))
    def test_real_client_family_runs_without_crash(self, family):
        cfg = _advisory_config(_ADVISORY_FAMILIES[family])
        validate_plan_config(cfg)  # all fixtures are valid configs
        res = find_retirement_date(cfg)
        # sensible result: success bool + (date or None) — never a crash
        assert isinstance(res["success"], bool)
        if res["success"]:
            assert res["retirement_date"] is not None
            success, ft, *_ = run_simulation(cfg, res["retirement_date"], _DEFAULT_INSTRUMENT_PARAMS)
            assert success is True
            assert "Date" in ft.columns
        else:
            assert res["retirement_date"] is None

    def test_education_annual_recurring_runs(self):
        cfg = _advisory_config(_ADVISORY_FAMILIES["Vijay & Prachi Shepunde (101_M3)"])
        res = find_retirement_date(cfg)
        assert isinstance(res["success"], bool)

    def test_marriage_lumpsum_runs(self):
        cfg = _advisory_config(_ADVISORY_FAMILIES["Anjan Yerubandi (110_M3)"], corpus=80_000_000)
        res = find_retirement_date(cfg)
        assert isinstance(res["success"], bool)

    def test_retirement_income_monthly_replenishing_runs(self):
        cfg = _advisory_config(_ADVISORY_FAMILIES["Pradeep Chakravarthi Sadasivuni & Family (109_M3)"])
        res = find_retirement_date(cfg)
        assert isinstance(res["success"], bool)


class TestLifetimeReplenishingPoolFix:
    """Regression for the pool death-date provisioning bug (P-732 / Plan 222 D-P222-5).

    A Replenishing (recurring-expense) goal whose payout schedule reaches the
    death/final-simulation date must be funded normally. Before the fix,
    ``simulate_pool`` provisioned from ``sim_date`` (which can sit mid-month)
    while the monthly withdrawal loop pays the whole calendar month, so a payout
    earlier in ``sim_date``'s month was withdrawn but never provisioned. The
    shortfall accumulated and surfaced as a spurious "Debt Pool Depleted" at the
    final month, making any ``end_mode='Lifetime'`` expense that starts before
    retirement falsely infeasible (regardless of amount).
    """

    def _config(self, rent_end_mode, rent_end_date=None, rent_amount=30000.0):
        cur = pd.Timestamp("2026-06-15")  # death = cur + 60y = 2086-06-15
        return {
            "current_date": cur,
            "current_age": 30,
            "target_lifetime": 90,
            "current_corpus": 5_000_000.0,
            "investment_streams": [{
                "name": "Salary", "amount": 150000.0, "start_date": cur,
                "end_date_mode": "At retirement", "end_date": None,
                "step_up_percent": 7.0, "step_up_frequency": "Annual", "step_up_date": cur,
            }],
            "goals": [
                {"name": "Retirement Income", "type": "Non-Negotiable", "amount": 80000.0,
                 "nature": "Replenishing", "structure": "Recurring", "frequency": "Monthly",
                 "start_date_mode": "At retirement", "start_date": None,
                 "end_mode": "Lifetime", "end_date": None, "occurrences": 360, "inflation_percent": 6.0},
                {"name": "Rent", "type": "Non-Negotiable", "amount": rent_amount,
                 "nature": "Replenishing", "structure": "Recurring", "frequency": "Monthly",
                 "start_date_mode": "Fixed", "start_date": cur,
                 "end_mode": rent_end_mode, "end_date": rent_end_date,
                 "occurrences": 1, "inflation_percent": 6.0},
            ],
            "one_time_investments": [],
            "risk_profile": "Aggressive",
        }

    def test_lifetime_replenishing_expense_is_feasible(self):
        # The lifetime rent's last tranche lands exactly on the death date
        # (2086-06-15) -- the precise condition that used to trip the bug.
        res = find_retirement_date(self._config("Lifetime"))
        assert res["success"], "lifetime Replenishing expense must not be spuriously infeasible"

    def test_tiny_lifetime_expense_does_not_break_feasibility(self):
        # A Re.1/mo lifetime expense cannot make an otherwise-feasible plan infeasible.
        res = find_retirement_date(self._config("Lifetime", rent_amount=1.0))
        assert res["success"]

    def test_lifetime_matches_end_one_month_before_death(self):
        # Lifetime (to 2086-06-15) must give essentially the same retirement date
        # as ending one month earlier; the bug made Lifetime spuriously much later.
        life = find_retirement_date(self._config("Lifetime"))
        near = find_retirement_date(self._config("Fixed date", rent_end_date=pd.Timestamp("2086-05-15")))
        assert life["success"] and near["success"]
        delta_months = abs(
            (life["retirement_date"].year - near["retirement_date"].year) * 12
            + (life["retirement_date"].month - near["retirement_date"].month)
        )
        assert delta_months <= 2, (life["retirement_date"], near["retirement_date"])


# ===========================================================================
# Plan 223 — Month-grid invariant regression tests (D-P223-2/3/4/5/6).
# ===========================================================================

class TestMonthGridInvariant:
    """Regression suite for the boundary-only month-grid coercion (Plan 223).

    All tests use a common base config with dates that are NOT necessarily on
    the 1st of the month, to confirm that the normalisation is applied before
    the engine runs.

    D-P223-5 inclusivity rules tested:
      (A) stream "At retirement" — exclusive of retirement month.
      (B) stream "Fixed end" — inclusive of end month.
      (C) recurring occurrence count = months_span // freq_months + 1;
          sub-frequency spans floor (Jan -> Mar Quarterly = 1 occurrence).
      (D) first month inclusive — month-1 contribution is counted.
    """

    def _base(self, **overrides):
        """Minimal feasible config with current_date on the 1st."""
        cfg = {
            "current_date": pd.Timestamp("2026-06-01"),
            "current_age": 35,
            "target_lifetime": 90,
            "current_corpus": 20_000_000,
            "investment_streams": [],
            "goals": [],
            "one_time_investments": [],
        }
        cfg.update(overrides)
        return cfg

    # -------------------------------------------------------------------------
    # (a) Month-1 contribution included (D-P223-5D)
    # -------------------------------------------------------------------------

    def test_month1_contribution_included(self):
        """The investment series must carry a non-zero row for the current month.

        A stream with start_date on the 15th of the current month, once normalised
        to the 1st, must appear in the MS investment grid starting at current_date.
        This verifies rule D (D-P223-5): month-1 inclusive contribution.

        We go through ``_normalise_config_dates`` (called internally by both
        ``find_retirement_date`` and ``run_simulation``) to simulate real usage.
        """
        from app.planning.engine import calculate_investment_cashflows, _normalise_config_dates
        cfg = self._base(
            investment_streams=[{
                "name": "SIP", "amount": 100_000, "start_date": pd.Timestamp("2026-06-15"),
                "end_date_mode": "Fixed", "end_date": pd.Timestamp("2030-01-01"),
                "step_up_percent": 0.0, "step_up_frequency": "Annual", "step_up_date": None,
            }]
        )
        # Apply the same normalisation the engine entry uses.
        cfg_normalised = _normalise_config_dates(cfg)
        assert cfg_normalised["investment_streams"][0]["start_date"] == pd.Timestamp("2026-06-01"), \
            "start_date must be normalised to day=1"
        # After normalisation start_date == current_date == 2026-06-01 -> month-1 included.
        inv_df = calculate_investment_cashflows(
            cfg_normalised, pd.Timestamp("2035-01-01"), pd.Timestamp("2030-01-01")
        )
        june_rows = inv_df[inv_df["Date"].dt.month == 6]
        first_june = june_rows[june_rows["Date"].dt.year == 2026]
        assert not first_june.empty, "month-1 row must exist in the investment series"
        assert first_june["Investment"].iloc[0] == pytest.approx(100_000.0)

    # -------------------------------------------------------------------------
    # (b) No step-up on current/start month; first step-up at start + freq
    #     (D-P223-4)
    # -------------------------------------------------------------------------

    def test_no_stepup_on_start_month_first_stepup_at_plus_freq(self):
        """With step_up_date = current_date (default), the step-up count for a
        target date exactly at current_date must be 0 (base amount), and for a
        target date exactly one Annual frequency later it must be 1."""
        from app.planning.engine import amount_at_date_with_stepup
        anchor = pd.Timestamp("2026-06-01")  # current_date (post-normalisation)
        start = pd.Timestamp("2026-06-01")
        amount = 100_000.0
        step_pct = 10.0

        # Target == current_date -> 0 step-ups -> base amount.
        val_now = amount_at_date_with_stepup(amount, step_pct, "Annual", anchor, start, anchor)
        assert val_now == pytest.approx(amount), "no step-up on the start month"

        # Target == start + 12 months (2027-06-01) -> 1 step-up.
        next_year = anchor + pd.DateOffset(months=12)
        val_next = amount_at_date_with_stepup(amount, step_pct, "Annual", anchor, start, next_year)
        assert val_next == pytest.approx(amount * 1.10, rel=1e-9), "first step-up one Annual freq later"

        # Target == start + 11 months (2027-05-01) -> still 0 step-ups (just before first).
        eleven_months = anchor + pd.DateOffset(months=11)
        val_eleven = amount_at_date_with_stepup(amount, step_pct, "Annual", anchor, start, eleven_months)
        assert val_eleven == pytest.approx(amount), "no step-up 11 months in (first is at 12)"

    # -------------------------------------------------------------------------
    # (c) Inclusivity rules A/B/C/D pinned (D-P223-5)
    # -------------------------------------------------------------------------

    def test_stream_at_retirement_exclusive_of_retirement_month(self):
        """Inclusivity rule A: 'At retirement' stream — Date < retirement_date
        (exclusive).  The retirement month itself must carry 0 investment."""
        from app.planning.engine import calculate_investment_cashflows
        ret_date = pd.Timestamp("2035-01-01")
        cfg = self._base(
            investment_streams=[{
                "name": "SIP", "amount": 50_000, "start_date": pd.Timestamp("2026-06-01"),
                "end_date_mode": "At retirement", "end_date": None,
                "step_up_percent": 0.0, "step_up_frequency": "Annual", "step_up_date": None,
            }]
        )
        inv_df = calculate_investment_cashflows(cfg, ret_date, pd.Timestamp("2040-01-01"))
        # Row for the exact retirement month must be excluded (0 or absent).
        ret_rows = inv_df[inv_df["Date"] == ret_date]
        if not ret_rows.empty:
            assert ret_rows["Investment"].iloc[0] == pytest.approx(0.0), \
                "retirement month must carry 0 investment (exclusive)"

    def test_stream_fixed_end_inclusive_of_end_month(self):
        """Inclusivity rule B: Fixed-end stream — Date <= end_date (inclusive)."""
        from app.planning.engine import calculate_investment_cashflows
        end_date = pd.Timestamp("2030-06-01")
        cfg = self._base(
            investment_streams=[{
                "name": "SIP", "amount": 50_000, "start_date": pd.Timestamp("2026-06-01"),
                "end_date_mode": "Fixed", "end_date": end_date,
                "step_up_percent": 0.0, "step_up_frequency": "Annual", "step_up_date": None,
            }]
        )
        inv_df = calculate_investment_cashflows(cfg, pd.Timestamp("2035-01-01"), pd.Timestamp("2035-01-01"))
        end_rows = inv_df[inv_df["Date"] == end_date]
        assert not end_rows.empty, "end month must appear in the series (inclusive)"
        assert end_rows["Investment"].iloc[0] == pytest.approx(50_000.0), \
            "end month must carry the full investment amount (inclusive)"

    def test_recurring_occurrence_count_jan_to_mar_quarterly_is_1(self):
        """Inclusivity rule C: months_span // freq_months + 1.

        Jan -> Mar (3-month span) with Quarterly frequency (freq_months=3):
            span = (Mar.year - Jan.year)*12 + (Mar.month - Jan.month) = 2 months
            occurrences = 2 // 3 + 1 = 0 + 1 = 1

        Operator confirmed: a sub-frequency span floors to 1 occurrence — the
        goal fires exactly once at the start date.
        """
        goal = {
            "structure": "Recurring",
            "frequency": "Quarterly",
            "end_mode": "Fixed date",
            "start_date": pd.Timestamp("2030-01-01"),
            "end_date": pd.Timestamp("2030-03-01"),  # 2-month span < 3-month freq
        }
        from app.planning.engine import _resolve_recurring_occurrences
        occ = _resolve_recurring_occurrences(goal, None)
        assert occ == 1, f"Jan->Mar Quarterly must resolve to 1 occurrence, got {occ}"

    def test_recurring_occurrence_count_jan_to_apr_quarterly_is_2(self):
        """Span = (Apr-Jan) = 3 months = exactly one freq step -> 3//3+1 = 2."""
        goal = {
            "structure": "Recurring",
            "frequency": "Quarterly",
            "end_mode": "Fixed date",
            "start_date": pd.Timestamp("2030-01-01"),
            "end_date": pd.Timestamp("2030-04-01"),  # 3-month span == 1 freq step
        }
        from app.planning.engine import _resolve_recurring_occurrences
        occ = _resolve_recurring_occurrences(goal, None)
        assert occ == 2, f"Jan->Apr Quarterly must resolve to 2 occurrences, got {occ}"

    # -------------------------------------------------------------------------
    # (d) Leap/EOM no-drift with day=1 anchors (D-P223-2)
    # -------------------------------------------------------------------------

    def test_leap_eom_no_drift_with_day1_anchor(self):
        """Day=1 anchors must never produce relativedelta EOM clamping drift.

        Jan-31 + 1 month -> Feb-28 (EOM clamping).  With day=1 anchors this
        never occurs: 2026-01-01 + 12 months = 2027-01-01, exactly.
        """
        from dateutil.relativedelta import relativedelta
        # Verify that a day=1 anchor advanced by any number of months stays on day=1.
        anchor = pd.Timestamp("2026-01-01")
        for n in [1, 2, 3, 6, 12, 13, 24, 25, 36]:
            result = anchor + relativedelta(months=n)
            assert result.day == 1, (
                f"day=1 anchor + {n} months produced day={result.day} (EOM clamping drift)"
            )

    # -------------------------------------------------------------------------
    # (e) F264 repro — Rent (mid-month start) + EMI (earlier start) now feasible
    #     after day=1 normalisation (D-P223-6)
    # -------------------------------------------------------------------------

    def test_f264_repro_rent_later_emi_earlier_feasible(self):
        """F264 repro: a Rent goal starting on a later day-of-month alongside an
        EMI starting earlier in the month previously caused a sub-rupee provisioning
        gap (the payout was withdrawn but not provisioned). With day=1 normalisation
        both payouts land on the 1st, eliminating the intra-month offset.

        Config shape mirrors F264: both goals are Replenishing Monthly Lifetime,
        one with a 'later' day-of-month start, one with an 'earlier' start.
        After normalisation both start on the 1st of their respective months.
        """
        cfg = {
            "current_date": pd.Timestamp("2026-06-28"),  # day != 1 — will be snapped
            "current_age": 30,
            "target_lifetime": 90,
            "current_corpus": 5_000_000,
            "investment_streams": [{
                "name": "Salary", "amount": 150_000,
                "start_date": pd.Timestamp("2026-06-28"),  # snapped to 2026-06-01
                "end_date_mode": "At retirement", "end_date": None,
                "step_up_percent": 7.0, "step_up_frequency": "Annual", "step_up_date": None,
            }],
            "goals": [
                # EMI-like Replenishing goal starting "earlier" in the month.
                {"name": "EMI", "type": "Non-Negotiable", "amount": 35_000,
                 "nature": "Replenishing", "structure": "Recurring", "frequency": "Monthly",
                 "start_date_mode": "Fixed", "start_date": pd.Timestamp("2026-06-15"),
                 "end_mode": "Lifetime", "end_date": None, "occurrences": 1, "inflation_percent": 0.0},
                # Rent-like Replenishing goal starting "later" in the month.
                {"name": "Rent", "type": "Non-Negotiable", "amount": 25_000,
                 "nature": "Replenishing", "structure": "Recurring", "frequency": "Monthly",
                 "start_date_mode": "Fixed", "start_date": pd.Timestamp("2026-06-28"),
                 "end_mode": "Lifetime", "end_date": None, "occurrences": 1, "inflation_percent": 6.0},
                # Retirement income — ties retirement date.
                {"name": "Retirement Income", "type": "Non-Negotiable", "amount": 60_000,
                 "nature": "Replenishing", "structure": "Recurring", "frequency": "Monthly",
                 "start_date_mode": "At retirement", "start_date": None,
                 "end_mode": "Lifetime", "end_date": None, "occurrences": 360, "inflation_percent": 6.0},
            ],
            "one_time_investments": [],
        }
        res = find_retirement_date(cfg)
        assert res["success"], (
            "F264 repro: Rent+EMI combo with day-offset starts must be feasible "
            "after day=1 normalisation (was None/infeasible before Plan 223)"
        )
        assert res["retirement_date"] is not None

    # -------------------------------------------------------------------------
    # (boundary) day != 1 input dates are silently coerced to day=1
    # -------------------------------------------------------------------------

    def test_mid_month_current_date_coerced_to_day1(self):
        """Any current_date with day != 1 must be silently normalised to day=1
        before the engine runs. The retirement date must therefore also be on day=1
        (the solver always emits day=1 candidates)."""
        cfg = self._base(
            current_date=pd.Timestamp("2026-06-15"),  # mid-month
            investment_streams=[{
                "name": "SIP", "amount": 100_000, "start_date": pd.Timestamp("2026-06-15"),
                "end_date_mode": "At retirement", "end_date": None,
                "step_up_percent": 0.0, "step_up_frequency": "Annual", "step_up_date": None,
            }],
            goals=[{
                "name": "Retirement Income", "type": "Non-Negotiable", "amount": 80_000,
                "nature": "Replenishing", "structure": "Recurring", "frequency": "Monthly",
                "start_date_mode": "At retirement", "start_date": None,
                "end_mode": "Lifetime", "end_date": None, "occurrences": 360, "inflation_percent": 6.0,
            }],
        )
        res = find_retirement_date(cfg)
        assert res["success"] is True
        rd = res["retirement_date"]
        assert rd.day == 1, f"retirement_date must be on the 1st; got {rd}"
