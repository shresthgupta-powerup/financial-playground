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
    _STCG_MAX_YEARS,
    _nav_value,
    calculate_corpus_required_for_future_expense,
    calculate_investment_cashflows,
    expand_recurring_goal_to_tranches,
    find_retirement_date,
    generate_pseudo_nav,
    run_simulation,
)
from app.planning.grid_engine import grid_slice_plan, slice_principal

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

    def test_redeem_net_at_the_year_boundary_is_ltcg(self):
        """Same lot redeemed on 2026-12-31 — 364 days held.

        RE-COMPUTED for the +goaltaxequity boundary rule (2026-08-24): within
        LTCG_GRACE_DAYS (2) of a full year counts as LONG-term, because the
        desk shifts the redemption a day or two to complete the year. Before
        that rule this was STCG at 20%.

        Hand: tax/unit = 10×0.10 = 1 → net/unit = 109.
        net 54,000 → units 54,000/109 = 495.412844...;
        gross = 495.412844×110 = 54,495.41; tax = 495.41.
        """
        pool = InvestmentPool("Debt", stcg_tax=0.20, ltcg_tax=0.10)
        pool.invest(pd.Timestamp("2026-01-01"), 100_000, nav=100)
        res = pool.redeem_net_amount(pd.Timestamp("2026-12-31"), 54_000, nav=110)
        assert -res["units"] == pytest.approx(54_000 / 109, abs=1e-9)
        assert res["tax"] == pytest.approx(54_000 / 109, abs=1e-6)
        assert res["net_received"] == pytest.approx(54_000.0, abs=1e-6)

    def test_redeem_net_stcg_back_solve(self):
        """A genuinely short holding — 363 days, one day inside the grace.

        Hand: STCG 20% ⇒ tax/unit = 10×0.20 = 2 → net/unit = 108.
        net 54,000 → units 500; gross 55,000; tax 1,000.
        (These are the original hand figures; only the DATE moved, to keep a
        real STCG case in the oracle after the boundary rule landed.)
        """
        pool = InvestmentPool("Debt", stcg_tax=0.20, ltcg_tax=0.10)
        pool.invest(pd.Timestamp("2026-01-01"), 100_000, nav=100)
        res = pool.redeem_net_amount(pd.Timestamp("2026-12-30"), 54_000, nav=110)
        assert (pd.Timestamp("2026-12-30") - pd.Timestamp("2026-01-01")).days == 363
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


class TestGridWindowOracle:
    """v2 replacement for the pool-window oracle (the pools are gone).

    What the old test pinned - "the right money is provisioned for the right
    window, and every payout is funded exactly once" - is now a property of
    the grid, so it is checked against hand-computed grid shares instead.
    """

    def test_grid_carves_hand_computed_shares(self):
        """A non-negotiable cashflow 4.6 years out, PLAN at 2026-06-01.

        Hand, straight off the grid (rows are floor(t), t on 365.25):
          t = 4.63 y -> row 4 -> 0% debt, 25% hybrid  -> first carve 25%
          then row 3 (25/25), row 2 (50/25), row 1 (75/25), row 0 (100/0).
        Successive rows add 25 points each time, and the hybrid quarter is
        carried from row 4 to row 1 before converting to debt at row 0.
        So there are exactly four slices of 25%, one of them hybrid-then-debt,
        and they sum to 100% of the cashflow.
        """
        cf = TODAY + pd.DateOffset(years=4, months=7)   # ~4.63 years out
        plan = grid_slice_plan("non-negotiable", cf, TODAY)

        assert [round(sl["share"], 10) for sl in plan] == [0.25, 0.25, 0.25, 0.25]
        assert sum(sl["share"] for sl in plan) == pytest.approx(1.0)

        hybrid_first = [sl for sl in plan if sl["hops"][0][0] == "hybrid"]
        assert len(hybrid_first) == 1                      # only the row-4 quarter
        assert [h[0] for h in hybrid_first[0]["hops"]] == ["hybrid", "debt"]
        assert all(len(sl["hops"]) == 1 for sl in plan if sl not in hybrid_first)

    def test_beyond_the_reach_nothing_is_carved(self):
        """Reach is 5 / 4 / 3 years by column. A cashflow at 4.6 years is
        inside for non-negotiable, outside for both others - so the negotiable
        and semi-negotiable plans start later, and a 6-year cashflow is
        outside every column (nothing is carved by anyone)."""
        cf = TODAY + pd.DateOffset(years=4, months=7)
        first_entry = {
            neg: min(sl["hops"][0][1] for sl in grid_slice_plan(neg, cf, TODAY))
            for neg in ("non-negotiable", "semi-negotiable", "negotiable")
        }
        assert first_entry["non-negotiable"] == TODAY      # already inside 5y
        assert first_entry["semi-negotiable"] > TODAY      # waits for 4y
        assert first_entry["negotiable"] > first_entry["semi-negotiable"]

        far = TODAY + pd.DateOffset(years=6)
        for neg in ("non-negotiable", "semi-negotiable", "negotiable"):
            entry = min(sl["hops"][0][1] for sl in grid_slice_plan(neg, far, TODAY))
            assert entry > TODAY                           # nothing carved today

    def test_every_cashflow_funded_exactly_once(self):
        """Four payouts across five years, all funded, none double-funded.

        Zero return / zero tax, corpus exactly the sum of the payouts: the
        plan is feasible with nothing to spare, which is only true if each
        payout is provisioned once.
        """
        amounts = [120_000.0, 130_000.0, 140_000.0, 150_000.0]
        goals = [{
            "name": "P%d" % i, "type": "Non-Negotiable", "structure": "Lumpsum",
            "start_date_mode": "Fixed",
            "start_date": TODAY + pd.DateOffset(months=m),
            "amount": a, "inflation_percent": 0.0,
        } for i, (m, a) in enumerate(zip((12, 30, 47, 49), amounts))]
        cfg = {
            "current_date": TODAY, "current_age": 45, "target_lifetime": 90,
            "current_corpus": sum(amounts), "investment_streams": [],
            "goals": goals, "one_time_investments": [],
        }
        ok, _ft, fail, _pm, _gd, _c = run_simulation(
            cfg, TODAY + pd.DateOffset(years=1), _ZERO_PARAMS)
        assert ok is True and fail is None

        cfg_short = dict(cfg, current_corpus=sum(amounts) - 1_000)
        ok2, _ft2, fail2, _pm2, _gd2, _c2 = run_simulation(
            cfg_short, TODAY + pd.DateOffset(years=1), _ZERO_PARAMS)
        assert ok2 is False and fail2 is not None


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




class TestSliceBackSolveOracle:
    """The back-solve algebra, unchanged from v1's chains to v2's slices.

    v1 walked a glide-path chain link by link; v2 walks a slice's hops. Same
    formula per leg - P = G / (g(1-tau) + tau) - so the hand-computed values
    below are the ORIGINAL chain oracles, re-pointed at slice_principal.
    """

    START = pd.Timestamp("2028-06-01")
    END = pd.Timestamp("2030-06-01")          # 730 days = 1.998631 y on 365.25

    def _navs(self, rate):
        months = pd.date_range(TODAY, pd.Timestamp("2032-01-01"), freq="MS")
        return {place: {m: _nav_value(rate, TODAY, m) for m in months}
                for place in ("core", "debt", "hybrid")}

    def _params(self, tax):
        return {"debt": {"return": 0.0, "stcg_tax": tax, "ltcg_tax": tax},
                "hybrid": {"return": 0.0, "stcg_tax": tax, "ltcg_tax": tax},
                "core": {"return": 0.0, "stcg_tax": tax, "ltcg_tax": tax}}

    def test_no_tax_pure_discounting(self):
        """G = 1,000,000 held 730 days at 12%, tau = 0.

        Hand: t = 730/365.25 = 1.998631 y -> growth = 1.12^t = 1.254206...
        -> P = 1,000,000 / 1.254206 = 797,317.56.
        (The engine's pseudo-NAV compounds daily on a 365-day year, so the
        growth factor differs in the 5th decimal; abs=250 covers that.)
        """
        got = slice_principal(1_000_000.0, [("debt", self.START, self.END)],
                              self.START, self._navs(0.12), self._params(0.0),
                              _STCG_MAX_YEARS)
        assert got == pytest.approx(797_317.56, abs=250.0)

    def test_ltcg_back_solve(self):
        """Same leg with tau = 10% (held ~2y > 1y -> LTCG).

        Hand: P = G / (g(1-tau)+tau), g = 1.12^1.998631 = 1.254206...
        -> P = 1,000,000 / (1.254206x0.9 + 0.1) = 813,812.10.
        """
        got = slice_principal(1_000_000.0, [("debt", self.START, self.END)],
                              self.START, self._navs(0.12), self._params(0.10),
                              _STCG_MAX_YEARS)
        assert got == pytest.approx(813_812.10, abs=250.0)

    def test_zero_return_principal_equals_goal(self):
        """r = 0 => no gain => tax irrelevant => P = G for any tau."""
        got = slice_principal(1_000_000.0, [("debt", self.START, self.END)],
                              self.START, self._navs(0.0), self._params(0.99),
                              _STCG_MAX_YEARS)
        assert got == pytest.approx(1_000_000.0, abs=0.01)

    def test_two_hop_slice_taxes_both_legs(self):
        """A hybrid->debt slice pays tax at the SWITCH as well as at the goal.

        Hand, zero-growth so only the tax terms bite: with tau = 0 on both
        legs P = G; with tau > 0 and growth > 0, the two-hop principal must
        exceed the one-hop principal over the same total span, because the
        switch realises a gain mid-way and taxes it.
        """
        mid = pd.Timestamp("2029-06-01")
        navs, params = self._navs(0.12), self._params(0.125)
        one_hop = slice_principal(1_000_000.0, [("debt", self.START, self.END)],
                                  self.START, navs, params, _STCG_MAX_YEARS)
        two_hop = slice_principal(
            1_000_000.0,
            [("hybrid", self.START, mid), ("debt", mid, self.END)],
            self.START, navs, params, _STCG_MAX_YEARS)
        assert two_hop > one_hop


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
