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
    generate_pseudo_nav,
    find_retirement_date,
    run_simulation,
)
from app.planning.glide_paths import GLIDEPATH_VERSION, get_glide_paths
from app.planning.grid_engine import grid_slice_plan
from app.planning.goal_grid import goal_sleeves
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
# (a) Regression golden-master. RE-BASELINED for v2 (the goal grid,
#     2026-08-24). These are NOT v3/v1 parity any more - v2 provisions goals
#     from one grid instead of glide-path chains + shared pools, so every
#     number moves. They exist to freeze v2's behaviour and to make the next
#     change explain itself.
# ===========================================================================

class TestParityGoldenMaster:
    def test_glidepath_version_pinned(self):
        assert GLIDEPATH_VERSION == 1
        assert engine.ENGINE_SOURCE_SHA == "v2grid+goaltaxequity"

    def test_parity_config_1_retirement_date(self):
        res = find_retirement_date(_parity_config_1())
        assert res["success"] is True
        assert res["retirement_date"] == pd.Timestamp("2033-01-01")

    def test_parity_config_1_snapshot_totals(self):
        """Golden-master RE-BASELINED for v2 - the goal GRID (2026-08-24).

        Previously re-baselined for +goaltaxequity (2026-08-24), +poolprefund
        (2026-08-11), +monthgrid (Plan 223, 2026-06-17).

        v2 replaces glide-path chains AND the shared Debt/Hybrid pools with
        one grid read per cashflow, so these numbers are a NEW baseline, not a
        drift from the old one. What changed structurally:

        - Provisioning is now per-cashflow and continuous. This config's
          "Retirement Income" runs 360+ monthly occurrences; v1 funded them
          from a pool refilled once a year, v2 carves each occurrence off the
          grid as it enters the 5-year window. len(ft) 137 -> 2649 because
          every one of those carves is now its own Core->sleeve transaction
          instead of one annual pool refill.
        - Retirement 2032-09-01 -> 2033-01-01 (+4 months). v2 provisions
          MORE: v1's pool sized only a 48-month lookahead, while the grid
          keeps a rolling 5-year book per cashflow AND taxes each hop it
          actually takes. Four months is the cost of that honesty.
        - Amount.sum -424,422,147 -> -440,874,021 (+3.9% gross flow) and
          tax.sum 53,060,745 -> 55,109,703 (+3.9%): the same money moves in
          more, smaller steps, and each step is taxed at its own holding
          period. The two move by the SAME 3.9% - a consistency check.
        - units.sum 314.00 -> 17.07 and core_last 28,307,534 -> 1,538,606:
          the terminal residual is a small difference of large numbers, and
          v2 deliberately leaves less idle in Core (goal money sits in the
          sleeves that will pay it). debt_last is 2,515,229 - the last
          sleeve, not yet drawn - so the plan ends funded, not starved.

        Both goals still appear in goal_dfs and comp still spans 720 months.
        No unexplained drift - reconciliation PASS.
        """
        cfg = _parity_config_1()
        res = find_retirement_date(cfg)
        rd = res["retirement_date"]
        success, ft, fail, pm, gd, comp = run_simulation(cfg, rd, _DEFAULT_INSTRUMENT_PARAMS)
        assert success is True
        assert len(ft) == 2649
        assert ft["Amount"].sum() == pytest.approx(-440874021.476125, rel=1e-9)
        assert ft["units"].sum() == pytest.approx(17.0670471315, rel=1e-9)
        assert ft["tax"].sum() == pytest.approx(55109702.985419, rel=1e-9)
        assert sorted(gd.keys()) == ["Retirement Home", "Retirement Income"]
        assert len(comp) == 720
        assert comp["Core Corpus Value"].iloc[-1] == pytest.approx(1538606.499188, rel=1e-9)

    def test_parity_config_2_retirement_date(self):
        res = find_retirement_date(_parity_config_2())
        assert res["success"] is True
        assert res["retirement_date"] == pd.Timestamp("2028-05-01")

    def test_parity_config_2_snapshot_totals(self):
        """Golden-master RE-BASELINED for v2 - the goal GRID (2026-08-24).

        Same structural change as config 1. This config has two Semi-Negotiable
        marriage lumpsums plus a 384-occurrence income stream, so it exercises
        the grid's per-column reach: semi-negotiable goals start provisioning
        4 years out, not 5.

        - Retirement 2028-02-01 -> 2028-05-01 (+3 months), the same direction
          and comparable size to config 1 - cross-config coherence PASS.
        - len(ft) 64 -> 1557: per-cashflow carves replace annual pool refills.
        - Amount.sum -88,681,425 -> -93,706,279 (+5.7%); tax.sum 11,129,813
          -> 11,768,609 (+5.7%). Identical percentages again: more, smaller,
          individually-taxed movements of the same underlying money.
        - units.sum 2045.09 -> 3006.07 and core_last 8,626,826 -> 14,074,908
          rise here (they fell in config 1) because this plan ENDS while its
          income stream is still running: goal money is still sitting in Core
          waiting for its window, where config 1's had already been carved.
          Both are the same mechanism seen at different points in a plan.

        All three goals appear in goal_dfs. comp spans 407 months (v1: 396) -
        v2 runs to the last cashflow, which is past the death date here.
        No unexplained drift - reconciliation PASS.
        """
        cfg = _parity_config_2()
        res = find_retirement_date(cfg)
        rd = res["retirement_date"]
        success, ft, fail, pm, gd, comp = run_simulation(cfg, rd, _DEFAULT_INSTRUMENT_PARAMS)
        assert success is True
        assert len(ft) == 1557
        assert ft["Amount"].sum() == pytest.approx(-93706279.352605, rel=1e-9)
        assert ft["units"].sum() == pytest.approx(3006.0719124786, rel=1e-9)
        assert ft["tax"].sum() == pytest.approx(11768609.251287, rel=1e-9)
        assert sorted(gd.keys()) == ["Marriage Elder", "Marriage Younger", "Retirement Income"]
        assert len(comp) == 407
        assert comp["Core Corpus Value"].iloc[-1] == pytest.approx(14074908.194281, rel=1e-9)

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
    """v2 (2026-08-24): the 48-month span cap is RETIRED.

    The cap existed because v1 built one glide-path CHAIN per occurrence, so a
    long recurring goal exploded into thousands of chains — the cap was an
    engine performance guard wearing a modelling costume, and it forced real
    goals (school fees to graduation, a 10-year EMI) to be mis-modelled as
    "Replenishing". v2 reads each cashflow off one grid and only ever carves
    the near ones, so length costs nothing and nothing needs re-labelling.

    These cases are the old cap's rejection matrix, now asserted to PASS.
    """

    def _recurring(self, frequency, occurrences, **kw):
        goal = {
            "name": "Edu", "type": "Non-Negotiable",
            "structure": "Recurring", "start_date_mode": "Fixed",
            "start_date": pd.Timestamp("2030-01-01"), "amount": 100_000,
            "frequency": frequency, "occurrences": occurrences,
            "end_mode": "Occurrences", "inflation_percent": 6.0,
        }
        goal.update(kw)
        return _base_config(goals=[goal])

    def test_6x_annual_now_valid(self):
        validate_plan_config(self._recurring("Annual", 6))       # was: rejected

    def test_50x_monthly_now_valid(self):
        validate_plan_config(self._recurring("Monthly", 50))     # was: rejected

    def test_fixed_date_49mo_now_valid(self):
        validate_plan_config(self._recurring(
            "Monthly", None, end_mode="Fixed date",
            end_date=pd.Timestamp("2034-02-01")))                # was: rejected

    def test_lifetime_recurring_now_valid(self):
        """A lifetime stream is just a goal most of whose cashflows sit beyond
        the window — derived as replenishing, not a validation error."""
        validate_plan_config(self._recurring(
            "Monthly", None, end_mode="Lifetime"))               # was: rejected

    def test_still_valid_cases_stay_valid(self):
        for freq, occ in (("Annual", 5), ("Monthly", 49), ("Quarterly", 17)):
            validate_plan_config(self._recurring(freq, occ))


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
        # +goaltaxequity boundary rule: within LTCG_GRACE_DAYS (2) of a full
        # year counts as LTCG (the desk shifts the redemption 1-2 days to
        # cross the year). STCG only when genuinely short: <= 363 days.
        assert pool._get_tax_rate(TODAY, TODAY + pd.Timedelta(days=363)) == 0.20
        assert pool._get_tax_rate(TODAY, TODAY + pd.Timedelta(days=364)) == 0.125
        assert pool._get_tax_rate(TODAY, TODAY + pd.Timedelta(days=365)) == 0.125
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



# ===========================================================================
# (e) calculate_goal_cashflows — chain back-solve for all 3 glide types.
# ===========================================================================



# ===========================================================================
# (e) simulate_pool — windows / refills / depletion.
# ===========================================================================

class TestSimulatePool:
    def _navs(self, end="2040-01-01"):
        debt = generate_pseudo_nav(TODAY, pd.Timestamp(end), 0.06)
        hybrid = generate_pseudo_nav(TODAY, pd.Timestamp(end), 0.10)
        return debt, hybrid




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


# ===========================================================================
# (v2) THE GOAL GRID — provisioning + settlement (DECISIONS.md 2026-08-24).
#
# These replace the v1 chain/pool tests (TestSimulatePool, TestGoalCashflows'
# chain back-solve, TestPayoutsAndNetting's compute_replenishing_payouts /
# net_investment_against_payouts): those functions no longer exist. What they
# were really asserting — payouts get funded, income offsets them first, money
# de-risks as the goal nears — is asserted here against the grid instead.
# ===========================================================================

def _grid_config(goals, corpus=50_000_000, streams=None, current_date=None,
                 current_age=40, target_lifetime=90):
    return {
        "current_date": current_date or TODAY,
        "current_age": current_age, "target_lifetime": target_lifetime,
        "current_corpus": corpus,
        "investment_streams": streams or [],
        "goals": goals, "one_time_investments": [],
    }


def _goal(name="Car", type_="Non-Negotiable", structure="Lumpsum",
          start=None, amount=1_000_000, inflation=8.0, **kw):
    g = {"name": name, "type": type_, "structure": structure,
         "start_date_mode": "Fixed", "start_date": start or pd.Timestamp("2031-10-01"),
         "amount": amount, "inflation_percent": inflation}
    g.update(kw)
    return g


class TestGridSlicePlan:
    """The grid's shares become dated funding events with real routes."""

    def test_non_negotiable_five_year_shape(self):
        # 25% enters each year from 5y out; the 4-5y slice is the only hybrid
        # one, and it CONVERTS to debt when the final (0-1y) row arrives.
        plan = grid_slice_plan("non-negotiable", pd.Timestamp("2031-10-01"), TODAY)
        assert [round(s["share"], 10) for s in plan] == [0.25, 0.25, 0.25, 0.25]
        assert plan[0]["hops"][0][0] == "hybrid"
        assert plan[0]["hops"][1][0] == "debt"          # the conversion
        assert all(len(s["hops"]) == 1 and s["hops"][0][0] == "debt"
                   for s in plan[1:])

    def test_shares_sum_to_the_column_total(self):
        # Each column's shares must sum to its own 0-1yr row total: the goal is
        # fully provisioned by its date (non-neg 100%), and less-negotiable
        # goals are provisioned less (semi 100%, neg 100% — all reach 100% at
        # t=0 in this grid; the difference is WHEN, not how much).
        for neg in ("non-negotiable", "semi-negotiable", "negotiable"):
            plan = grid_slice_plan(neg, pd.Timestamp("2031-10-01"), TODAY)
            assert sum(s["share"] for s in plan) == pytest.approx(1.0)

    def test_reach_clips_the_start_row(self):
        # A negotiable goal reaches 3 years, so nothing is carved before then
        # even though the cashflow is 5 years out: its first entry month is
        # ~3 years away, not ~5.
        far = pd.Timestamp("2031-10-01")
        neg_plan = grid_slice_plan("negotiable", far, TODAY)
        non_plan = grid_slice_plan("non-negotiable", far, TODAY)
        assert min(s["hops"][0][1] for s in neg_plan) > \
               min(s["hops"][0][1] for s in non_plan)

    def test_late_plan_catches_up_in_one_event(self):
        # A goal already inside its final year has no runway: the whole 100%
        # enters at the plan start rather than gliding.
        soon = TODAY + pd.DateOffset(months=6)
        plan = grid_slice_plan("non-negotiable", soon, TODAY)
        assert len(plan) == 1
        assert plan[0]["share"] == pytest.approx(1.0)
        assert pd.Timestamp(plan[0]["hops"][0][1]) == TODAY


class TestGridFundsGoalsExactly:
    """Path-consistent sizing: the goal receives its FV, net of every tax."""

    def test_lumpsum_goal_paid_to_the_rupee(self):
        cf = pd.Timestamp("2031-10-01")
        cfg = _grid_config([_goal(start=cf, amount=1_000_000, inflation=8.0)])
        ok, _ft, fail, _pm, goal_dfs, _comp = run_simulation(
            cfg, pd.Timestamp("2040-01-01"), _DEFAULT_INSTRUMENT_PARAMS)
        assert ok is True and fail is None
        t = (cf - TODAY).days / 365.25
        target_fv = 1_000_000 * (1.08 ** t)
        gdf = goal_dfs["Car"]
        paid = gdf[gdf["place"] == "goal"]["inflow_amount"].sum()
        assert paid == pytest.approx(target_fv, abs=1.0)

    def test_sizing_is_path_consistent_not_doc_literal(self):
        """The hybrid slice is sized for the route it TRAVELS (hybrid then debt).

        The doc's formula discounts the hybrid share at hybrid growth for the
        whole distance; the grid actually moves that money into debt for the
        final year, and taxes the switch. Sizing must follow the real route —
        so the principal is HIGHER than the doc-literal figure (operator
        decision, 2026-08-24; goal_grid.invest_today keeps the doc behaviour).
        """
        cf = pd.Timestamp("2031-10-01")
        cfg = _grid_config([_goal(start=cf, amount=1_000_000, inflation=8.0)])
        _ok, _ft, _f, _pm, goal_dfs, _c = run_simulation(
            cfg, pd.Timestamp("2040-01-01"), _DEFAULT_INSTRUMENT_PARAMS)
        gdf = goal_dfs["Car"]
        hybrid_entry = gdf[(gdf["inflow_from"] == "core corpus")
                           & (gdf["place"] == "hybrid")]
        assert len(hybrid_entry) == 1
        engine_principal = float(hybrid_entry.iloc[0]["inflow_amount"])

        entry_date = pd.Timestamp(hybrid_entry.iloc[0]["inflow_date"])
        t_entry = (cf - entry_date).days / 365.25
        t_plan = (cf - TODAY).days / 365.25
        fv = 1_000_000 * (1.08 ** t_plan)
        doc_literal = (fv * 0.25) / (1.10 ** t_entry)     # never taxed, never switched
        assert engine_principal > doc_literal
        assert engine_principal == pytest.approx(doc_literal * 1.10, rel=0.06)

    def test_recurring_goal_every_occurrence_paid(self):
        cfg = _grid_config([_goal(
            name="Fees", structure="Recurring", start=pd.Timestamp("2029-04-01"),
            amount=500_000, inflation=10.0, frequency="Annual",
            occurrences=4, end_mode="Occurrences")])
        ok, _ft, fail, _pm, goal_dfs, _c = run_simulation(
            cfg, pd.Timestamp("2040-01-01"), _DEFAULT_INSTRUMENT_PARAMS)
        assert ok is True and fail is None
        gdf = goal_dfs["Fees"]
        goal_rows = gdf[gdf["place"] == "goal"]
        assert goal_rows["inflow_date"].nunique() == 4
        for d, grp in goal_rows.groupby("inflow_date"):
            t = (pd.Timestamp(d) - TODAY).days / 365.25
            assert grp["inflow_amount"].sum() == pytest.approx(
                500_000 * (1.10 ** t), abs=1.0)


class TestGridDynamics:
    """Decisions of 2026-08-24: monthly Core re-read, derived purpose."""

    def test_long_income_is_replenishing_and_funds_rollingly(self):
        # 487 monthly occurrences: only the near ones are ever carved; the rest
        # stay Core's job until their window arrives. The plan must still pay
        # every one of them, and the sleeves must keep refilling for decades.
        income = _goal(name="Retirement Income", structure="Recurring",
                       start=pd.Timestamp("2026-09-01"), amount=125_000,
                       inflation=6.0, frequency="Monthly", occurrences=487,
                       end_mode="Occurrences")
        sleeves = goal_sleeves(income, TODAY, apply_tax=False)
        assert sleeves["purpose"] == "GOAL_REPLENISH"
        assert sleeves["beyond"] > sleeves["inside"] > 0

        cfg = _grid_config([income], corpus=200_000_000,
                           current_age=60, target_lifetime=100)
        ok, _ft, fail, _pm, _gd, comp = run_simulation(
            cfg, pd.Timestamp("2026-09-01"), _DEFAULT_INSTRUMENT_PARAMS)
        assert ok is True and fail is None
        # Sleeves are alive in the middle of the plan, not only at the start —
        # this is the monthly re-read against Core doing its job.
        mid = comp.iloc[len(comp) // 2]
        assert mid["Debt Pool Value"] > 0

    def test_short_recurring_goal_is_not_replenishing(self):
        # "Recurring is not the same as replenishing" — 8 quarterly payouts
        # inside two years are fully funded now.
        g = _goal(name="Fees", structure="Recurring",
                  start=TODAY + pd.DateOffset(months=3), amount=100_000,
                  frequency="Quarterly", occurrences=8, end_mode="Occurrences")
        assert goal_sleeves(g, TODAY, apply_tax=False)["purpose"] == "GOAL_NON_REPLENISH"

    def test_nature_input_is_ignored(self):
        """`nature` is no longer an input — the same goal plans identically
        whether it is labelled Replenishing, Non-replenishing, or nothing."""
        cf = pd.Timestamp("2031-10-01")
        outs = []
        for nature in ("Replenishing", "Non-replenishing", None):
            g = _goal(start=cf)
            if nature is not None:
                g["nature"] = nature
            _ok, _ft, _f, _pm, gd, _c = run_simulation(
                _grid_config([g]), pd.Timestamp("2040-01-01"),
                _DEFAULT_INSTRUMENT_PARAMS)
            outs.append(gd["Car"]["inflow_amount"].sum())
        assert outs[0] == pytest.approx(outs[1]) == pytest.approx(outs[2])

    def test_income_nets_against_due_cashflows_untaxed(self):
        """Income funds a due cashflow directly — no sleeve, no tax, no carve.

        A goal fully covered by that month's income needs no provisioning at
        all, so it produces no Core->bucket rows.
        """
        due = TODAY + pd.DateOffset(months=6)
        stream = {"name": "Salary", "amount": 5_000_000, "start_date": TODAY,
                  "end_date_mode": "Fixed", "end_date": pd.Timestamp("2040-01-01"),
                  "step_up_percent": 0.0, "step_up_frequency": "Annual",
                  "step_up_date": TODAY}
        cfg = _grid_config([_goal(start=due, amount=1_000_000)],
                           corpus=1_000_000, streams=[stream])
        ok, _ft, fail, _pm, goal_dfs, _c = run_simulation(
            cfg, pd.Timestamp("2040-01-01"), _DEFAULT_INSTRUMENT_PARAMS)
        assert ok is True and fail is None
        assert goal_dfs == {} or goal_dfs["Car"].empty


class TestGridFailureSemantics:
    """Failure means ALL pools are depleted — nothing weaker (decision #3)."""

    def test_failure_only_when_everything_is_empty(self):
        income = _goal(name="Retirement Income", structure="Recurring",
                       start=pd.Timestamp("2026-09-01"), amount=125_000,
                       inflation=6.0, frequency="Monthly", occurrences=487,
                       end_mode="Occurrences")
        cfg = _grid_config([income], corpus=5_000_000,
                           current_age=60, target_lifetime=100)
        ok, _ft, fail, _pm, _gd, comp = run_simulation(
            cfg, pd.Timestamp("2026-09-01"), _DEFAULT_INSTRUMENT_PARAMS)
        assert ok is False
        assert "All pools depleted" in fail["description"]
        row = comp[comp["Date"] >= pd.Timestamp(fail["date"])].iloc[0]
        assert row["Core Corpus Value"] == pytest.approx(0, abs=1.0)
        assert row["Debt Pool Value"] == pytest.approx(0, abs=1.0)
        assert row["Hybrid Pool Value"] == pytest.approx(0, abs=1.0)

    def test_core_short_of_a_provisioning_event_is_not_failure(self):
        """A month Core cannot fully provision is a RETRY, not a failure.

        Corpus is small at the plan start but a large income stream arrives
        later, well before the goal is due: the slice re-sizes and completes in
        a later month, and the plan succeeds.
        """
        cf = TODAY + pd.DateOffset(years=4)
        stream = {"name": "Salary", "amount": 400_000, "start_date": TODAY,
                  "end_date_mode": "Fixed", "end_date": cf,
                  "step_up_percent": 0.0, "step_up_frequency": "Annual",
                  "step_up_date": TODAY}
        cfg = _grid_config([_goal(start=cf, amount=10_000_000, inflation=6.0)],
                           corpus=100_000, streams=[stream])
        ok, _ft, fail, _pm, _gd, _c = run_simulation(
            cfg, pd.Timestamp("2040-01-01"), _DEFAULT_INSTRUMENT_PARAMS)
        assert ok is True and fail is None

    def test_a_goal_may_raid_another_goals_sleeve_before_failing(self):
        """Sequential funding means the LAST-priority goal gives money up.

        Two goals due the same month with only enough for one: the plan drains
        the negotiable goal's sleeve to pay the non-negotiable one, so the
        failure (if any) is reported against the negotiable goal, never the
        non-negotiable one.
        """
        due = TODAY + pd.DateOffset(years=3)
        goals = [_goal(name="Must", type_="Non-Negotiable", start=due,
                       amount=5_000_000, inflation=6.0),
                 _goal(name="Maybe", type_="Negotiable", start=due,
                       amount=5_000_000, inflation=6.0)]
        cfg = _grid_config(goals, corpus=6_000_000)
        ok, _ft, fail, _pm, _gd, _c = run_simulation(
            cfg, pd.Timestamp("2040-01-01"), _DEFAULT_INSTRUMENT_PARAMS)
        assert ok is False
        assert "Maybe" in fail["description"]


class TestGridLongRecurringIsValidNow:
    """The 48-month non-replenishing span cap is retired with the chains."""

    def test_long_recurring_goal_validates(self):
        g = _goal(name="School Fees", structure="Recurring",
                  start=pd.Timestamp("2029-04-01"), amount=300_000,
                  frequency="Annual", occurrences=12, end_mode="Occurrences")
        validate_plan_config(_grid_config([g]))  # no raise
