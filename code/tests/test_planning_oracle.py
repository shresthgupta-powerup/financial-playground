"""Independent-recomputation oracle tests (Plan 207 Ph3, D-P207-7).

Every expected value in this module was computed BY HAND (or with a separate
throwaway calculation) from the formulas documented in the v3
``SIMULATION_MODEL.md`` — NOT by running the engine. The point is independence:
the golden master asserts "engine == engine at baseline"; these assert
"engine == the documented model". A failure here is either a real defect or a
genuine model surprise; both must be triaged (D-P207-8), never re-baselined
silently.

Conventions pinned here (from the model doc + DECISIONS.md):
  * PV→FV growth: ``PV * (1 + i) ** (days / 365.25)`` — actual-days year
    fraction, NOT calendar-year integer compounding. (The doc's "continuous
    compounding" phrasing means fractional-exponent discrete compounding.)
  * Tax back-solve: principal P for a post-tax target E over t years at
    return r, tax τ: ``P = E / ((1+r)**t * (1-τ) + τ)``.
  * FIFO tax lots: per-lot tax = gain × (STCG if holding ≤ 365 days else LTCG).
  * Pool windows (2+2, deliberate post-port divergence from v3's 2+3):
    Debt = payouts in [cycle, cycle+24m); Hybrid = [cycle+24m, cycle+48m).
  * Step-ups: discrete events on anchor-date anniversaries;
    amount = base × (1+s)^N with N events in (stream_start, date].
"""

import numpy as np
import pandas as pd
import pytest

from app.planning.engine import (
    InvestmentPool,
    calculate_corpus_required_for_future_expense,
    calculate_goal_cashflows,
    calculate_investment_cashflows,
    expand_recurring_goal_to_tranches,
    find_retirement_date,
    generate_pseudo_nav,
    simulate_pool,
)

TODAY = pd.Timestamp("2026-06-01")

_ZERO = {"return": 0.0, "stcg_tax": 0.0, "ltcg_tax": 0.0}
_ZERO_PARAMS = {k: dict(_ZERO) for k in ("core_corpus", "equity", "debt", "hybrid", "cash")}


# ---------------------------------------------------------------------------
# O1/O2 — Goal PV→FV growth
# ---------------------------------------------------------------------------


class TestGoalFvOracle:
    def test_lumpsum_fv_four_exact_years(self):
        """2026-06-01 → 2030-06-01 is exactly 1461 days = 4.0 years (one leap).

        Hand: FV = 1,000,000 × 1.07^4 = 1,000,000 × 1.31079601 = 1,310,796.01.
        """
        goal = {"name": "g", "structure": "Lumpsum", "amount": 1_000_000,
                "inflation_percent": 7.0, "start_date": pd.Timestamp("2030-06-01")}
        [(date, fv)] = expand_recurring_goal_to_tranches(goal, TODAY)
        assert date == pd.Timestamp("2030-06-01")
        assert fv == pytest.approx(1_310_796.01, abs=0.02)

    def test_recurring_annual_escalation_per_occurrence(self):
        """3 annual occurrences from 2030-06-01 at 6%, per-occurrence PV 10,000.

        Hand (days from 2026-06-01; FV = 10,000 × 1.06^(days/365.25)):
          occ0 2030-06-01: 1461 d → 12,624.77
          occ1 2031-06-01: 1826 d → 13,381.72
          occ2 2032-06-01: 2192 d → 14,186.32  (2032 leap adds a day)
        """
        goal = {"name": "g", "structure": "Recurring", "amount": 10_000,
                "inflation_percent": 6.0, "frequency": "Annual", "occurrences": 3,
                "start_date": pd.Timestamp("2030-06-01")}
        tranches = expand_recurring_goal_to_tranches(goal, TODAY)
        expected = [
            (pd.Timestamp("2030-06-01"), 12_624.77),
            (pd.Timestamp("2031-06-01"), 13_381.72),
            (pd.Timestamp("2032-06-01"), 14_186.32),
        ]
        assert len(tranches) == 3
        for (date, fv), (exp_date, exp_fv) in zip(tranches, expected):
            assert date == exp_date
            assert fv == pytest.approx(exp_fv, abs=0.02)


# ---------------------------------------------------------------------------
# O3 — FIFO tax-lot accounting
# ---------------------------------------------------------------------------


class TestTaxLotOracle:
    def test_redeem_net_ltcg_back_solve(self):
        """1000 units bought at NAV 100 on 2026-01-01; redeem on 2027-01-02
        (367 days > 365 → LTCG 10%) at NAV 110 for a NET 54,500.

        Hand: tax/unit = (110−100)×0.10 = 1 → net/unit = 109.
        units = 54,500 / 109 = 500; gross = 55,000; tax = 500.
        """
        pool = InvestmentPool("Debt", stcg_tax=0.20, ltcg_tax=0.10)
        pool.invest(pd.Timestamp("2026-01-01"), 100_000, nav=100)
        res = pool.redeem_net_amount(pd.Timestamp("2027-01-02"), 54_500, nav=110)
        assert res["fully_funded"] is True
        assert -res["units"] == pytest.approx(500.0, abs=1e-9)
        assert -res["Amount"] == pytest.approx(55_000.0, abs=1e-6)
        assert res["tax"] == pytest.approx(500.0, abs=1e-6)
        assert res["net_received"] == pytest.approx(54_500.0, abs=1e-6)

    def test_redeem_net_stcg_back_solve(self):
        """Same lot redeemed on 2026-12-31 (364 days ≤ 365 → STCG 20%).

        Hand: tax/unit = 10×0.20 = 2 → net/unit = 108.
        net 54,000 → units 500; gross 55,000; tax 1,000.
        """
        pool = InvestmentPool("Debt", stcg_tax=0.20, ltcg_tax=0.10)
        pool.invest(pd.Timestamp("2026-01-01"), 100_000, nav=100)
        res = pool.redeem_net_amount(pd.Timestamp("2026-12-31"), 54_000, nav=110)
        assert -res["units"] == pytest.approx(500.0, abs=1e-9)
        assert res["tax"] == pytest.approx(1_000.0, abs=1e-6)
        assert res["net_received"] == pytest.approx(54_000.0, abs=1e-6)

    def test_redeem_gross_fifo_consumes_oldest_lot_first(self):
        """Lot A: 1000 @100 (2026-01-01); lot B: 500 @120 (2026-06-01).
        Redeem GROSS 130,000 on 2027-01-02 at NAV 130.

        Hand: lot A value = 130,000 exactly → consumed whole (held 367 d, LTCG
        10%): gain 30,000 → tax 3,000. Lot B untouched (FIFO).
        """
        pool = InvestmentPool("Debt", stcg_tax=0.20, ltcg_tax=0.10)
        pool.invest(pd.Timestamp("2026-01-01"), 100_000, nav=100)
        pool.invest(pd.Timestamp("2026-06-01"), 60_000, nav=120)
        res = pool.redeem_gross_amount(pd.Timestamp("2027-01-02"), 130_000, nav=130)
        assert -res["Amount"] == pytest.approx(130_000.0, abs=1e-6)
        assert res["tax"] == pytest.approx(3_000.0, abs=1e-6)
        assert len(pool.lots) == 1
        assert pool.lots[0].units == pytest.approx(500.0)
        assert pool.lots[0].purchase_price == pytest.approx(120.0)


# ---------------------------------------------------------------------------
# O4 — Post-tax corpus-required back-solve formula
# ---------------------------------------------------------------------------


class TestCorpusRequiredOracle:
    def test_no_tax_is_pure_discounting(self):
        """E=121,000, t=2, r=10%, τ=0 → P = 121,000 / 1.21 = 100,000."""
        p = calculate_corpus_required_for_future_expense(121_000, 2, 0.10, 0.0)
        assert p == pytest.approx(100_000.0, abs=1e-6)

    def test_with_tax_back_solves_post_tax_target(self):
        """E=121,000, t=2, r=10%, τ=50% → P = 121,000/(1.21×0.5+0.5) = 109,502.26.

        Check: P grows to 132,497.74; gain 22,995.48; tax 11,497.74;
        net = 121,000.00 exactly.
        """
        p = calculate_corpus_required_for_future_expense(121_000, 2, 0.10, 0.50)
        assert p == pytest.approx(109_502.26, abs=0.01)
        grown = p * 1.21
        net = grown - (grown - p) * 0.50
        assert net == pytest.approx(121_000.0, abs=0.01)

    def test_zero_return_makes_tax_irrelevant(self):
        """r=0 → no gain → no tax → P = E for ANY tax rate."""
        assert calculate_corpus_required_for_future_expense(50_000, 3, 0.0, 0.99) \
            == pytest.approx(50_000.0, abs=1e-9)


# ---------------------------------------------------------------------------
# O5 — Pool 2+2 window membership + refill arithmetic (zero-return)
# ---------------------------------------------------------------------------


class TestPoolWindowOracle:
    def test_first_cycle_refills_match_window_sums(self):
        """Payouts at months +12 (120k), +30 (130k), +47 (140k), +49 (150k)
        from sim start. Zero return / zero tax ⇒ refill = plain window sum.

        Hand (first annual cycle at sim start):
          Debt   window [0, 24m)  → 120,000
          Hybrid window [24m, 48m) → 130,000 + 140,000 = 270,000
          month-49 payout excluded from cycle 1 entirely.
        """
        start = TODAY
        payouts = pd.DataFrame({
            "Date": [start + pd.DateOffset(months=m) for m in (12, 30, 47, 49)],
            "Amount": [120_000.0, 130_000.0, 140_000.0, 150_000.0],
        })
        final = start + pd.DateOffset(months=60)
        nav = generate_pseudo_nav(start, final, 0.0)
        pool_trans, repl, fail_date, fail_reason, _ = simulate_pool(
            payouts, nav, nav, _ZERO, _ZERO, start, final)
        assert fail_date is None

        first_cycle = repl[repl["Date"] == start]
        debt = first_cycle[first_cycle["Description"] == "Replenishment: Debt Pool"]
        hybrid = first_cycle[first_cycle["Description"] == "Replenishment: Hybrid Pool"]
        assert float(debt["Amount"].sum()) == pytest.approx(120_000.0, abs=0.02)
        assert float(hybrid["Amount"].sum()) == pytest.approx(270_000.0, abs=0.02)
        # Cycle-1 total must NOT include the month-49 payout.
        assert float(first_cycle["Amount"].sum()) == pytest.approx(390_000.0, abs=0.05)
        # Across the whole horizon every payout is funded exactly once.
        assert float(repl["Amount"].sum()) == pytest.approx(540_000.0, abs=0.10)


# ---------------------------------------------------------------------------
# O6 — Investment stream step-up calendar
# ---------------------------------------------------------------------------


class TestStepUpOracle:
    def test_annual_step_up_discrete_events(self):
        """Stream 100,000/mo from 2026-06-01, 10% annual step-up anchored
        2026-05-31, At-retirement end with retirement 2028-06-01.

        Hand: months 2026-06 .. 2027-05 (12 rows) pay 100,000 (first
        anniversary 2027-05-31 falls AFTER the 2027-05-01 row); months
        2027-06 .. 2028-05 (12 rows) pay 110,000. Retirement month exclusive →
        24 paying rows; total = 12×100,000 + 12×110,000 = 2,520,000.
        """
        cfg = {
            "current_date": TODAY,
            "investment_streams": [{
                "name": "Salary", "amount": 100_000, "start_date": TODAY,
                "end_date_mode": "At retirement", "end_date": None,
                "step_up_percent": 10.0, "step_up_frequency": "Annual",
                "step_up_date": TODAY - pd.Timedelta(days=1),
            }],
        }
        ret = pd.Timestamp("2028-06-01")
        df = calculate_investment_cashflows(cfg, ret, ret + pd.DateOffset(months=1))
        paying = df[df["Investment"] > 0]
        assert len(paying) == 24
        assert paying["Investment"].iloc[:12].tolist() == pytest.approx([100_000.0] * 12)
        assert paying["Investment"].iloc[12:24].tolist() == pytest.approx([110_000.0] * 12)
        assert float(df["Investment"].sum()) == pytest.approx(2_520_000.0, abs=1e-6)


# ---------------------------------------------------------------------------
# O7 — Glide-chain back-solve on a hand-built single-link chain
# ---------------------------------------------------------------------------


def _single_link_glide():
    """Minimal chain: core corpus → debt (2y before goal end → end) → goal."""
    return pd.DataFrame([
        {"id": 1, "place": "debt", "years from inflow till end": 2,
         "years from outflow till end": 0, "inflow_from": "core corpus",
         "outflow_to": 2, "% of goal value": 100},
        {"id": 2, "place": "goal", "years from inflow till end": 0,
         "years from outflow till end": np.nan, "inflow_from": 1,
         "outflow_to": np.nan, "% of goal value": 100},
    ])


class TestChainBackSolveOracle:
    END = pd.Timestamp("2030-06-01")  # debt inflow 2028-06-01 → 730 d = 1.998631 y

    def _params(self, ret, tax):
        return {"debt": {"return": ret, "stcg_tax": tax, "ltcg_tax": tax}}

    def test_no_tax_pure_discounting(self):
        """G = 1,000,000 held 730 days at 12%, τ=0.

        Hand: t = 730/365.25 = 1.998631 y → growth = 1.12^t = 1.254206…
        → P = 1,000,000 / 1.254206 = 797,317.56 (engine rounds to 2 dp;
        assert ±1 for the rounding of intermediate steps).
        """
        df = calculate_goal_cashflows(_single_link_glide(), self.END, 1_000_000,
                                      self._params(0.12, 0.0), {"current_date": TODAY})
        goal_row = df[df["place"] == "goal"].iloc[0]
        debt_row = df[df["place"] == "debt"].iloc[0]
        assert goal_row["inflow_amount"] == pytest.approx(1_000_000.0, abs=0.01)
        assert debt_row["inflow_amount"] == pytest.approx(797_317.56, abs=1.0)
        # The link's own outflow reconciles: principal grows back to the goal.
        assert debt_row["total_outflow_amount"] == pytest.approx(1_000_000.0, abs=1.0)
        assert debt_row["tax_out_of_outflow"] == pytest.approx(0.0, abs=0.01)

    def test_ltcg_back_solve(self):
        """Same chain with τ = 10% (held ~2y > 1y → LTCG).

        Hand: P = G / (g(1−τ)+τ) with g = 1.12^1.998631 = 1.254206…
        → P = 1,000,000 / (1.254206×0.9 + 0.1) = 813,812.10 (±1).
        """
        df = calculate_goal_cashflows(_single_link_glide(), self.END, 1_000_000,
                                      self._params(0.12, 0.10), {"current_date": TODAY})
        debt_row = df[df["place"] == "debt"].iloc[0]
        assert debt_row["inflow_amount"] == pytest.approx(813_812.10, abs=1.0)
        # Verify the round trip from the engine's own outflow figures:
        # outflow − tax must equal the goal target.
        net = debt_row["total_outflow_amount"] - debt_row["tax_out_of_outflow"]
        assert net == pytest.approx(1_000_000.0, abs=1.0)

    def test_zero_return_principal_equals_goal(self):
        """r = 0 ⇒ no gain ⇒ tax irrelevant ⇒ P = G for any τ."""
        df = calculate_goal_cashflows(_single_link_glide(), self.END, 1_000_000,
                                      self._params(0.0, 0.99), {"current_date": TODAY})
        debt_row = df[df["place"] == "debt"].iloc[0]
        assert debt_row["inflow_amount"] == pytest.approx(1_000_000.0, abs=0.01)


# ---------------------------------------------------------------------------
# O8 — End-to-end feasibility boundary under zero rates
# ---------------------------------------------------------------------------


class TestFeasibilityBoundaryOracle:
    def _config(self, corpus):
        return {
            "current_date": TODAY, "current_age": 45, "target_lifetime": 90,
            "current_corpus": corpus,
            "investment_streams": [],
            "goals": [{
                "name": "House", "type": "Non-Negotiable",
                "nature": "Non-replenishing", "structure": "Lumpsum",
                "start_date_mode": "Fixed",
                "start_date": pd.Timestamp("2029-06-01"),
                "amount": 1_000_000, "inflation_percent": 0.0,
            }],
            "one_time_investments": [],
        }

    def test_corpus_just_above_goal_is_feasible(self):
        """Zero returns + zero taxes + zero inflation: the model needs exactly
        the goal amount, nothing more. Corpus 1,000,100 ≥ 1,000,000 → feasible
        (nothing retirement-linked → solver short-circuits to one check).
        """
        res = find_retirement_date(self._config(1_000_100), _ZERO_PARAMS)
        assert res["success"] is True

    def test_corpus_just_below_goal_is_infeasible(self):
        """Corpus 999,900 < 1,000,000 → must fail: no padding, no rounding
        slack bigger than the engine's ₹1 funding tolerance."""
        res = find_retirement_date(self._config(999_900), _ZERO_PARAMS)
        assert res["success"] is False
