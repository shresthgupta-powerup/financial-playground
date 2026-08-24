"""Golden tests for the v2 goal grid, taken from Punit's "Goal Algo" document.

The document's worked tables are computed WITHOUT tax, so they validate layers
1-3 (grid lookup -> carve at FV -> discount to today). Layer 4 (the tax gross-up)
is tested separately against the engine's existing convention.

Tolerance: the document notes its own figures move ~Rs 172/day with the date they
were struck on, so exact-to-the-rupee equality is not the right bar; a few rupees
is.
"""

import pandas as pd
import pytest

from app.planning import goal_grid as gg
from app.planning.engine import _DEFAULT_INSTRUMENT_PARAMS

RUPEE = 25.0  # tolerance in rupees on a Rs 10,00,000 cashflow


class TestGrid:
    def test_columns_reach_full_funding_inside_a_year(self):
        """Every column totals 100% in the 0-1 band: nothing is unfunded at payout."""
        for neg in ("non-negotiable", "semi-negotiable", "negotiable"):
            debt, hybrid = gg.carve_shares(0.5, neg)
            assert debt + hybrid == pytest.approx(1.0)

    def test_each_column_has_its_own_reach(self):
        assert gg.carve_shares(4.5, "non-negotiable") == (0.00, 0.25)
        assert gg.carve_shares(4.5, "semi-negotiable") == (0.0, 0.0)
        assert gg.carve_shares(3.5, "negotiable") == (0.0, 0.0)

    def test_beyond_five_years_nobody_carves(self):
        for neg in ("non-negotiable", "semi-negotiable", "negotiable"):
            assert gg.carve_shares(5.5, neg) == (0.0, 0.0)
            assert gg.is_beyond_window(5.5, neg) is True

    def test_past_cashflows_are_not_ours_to_fund(self):
        assert gg.carve_shares(-0.1, "non-negotiable") == (0.0, 0.0)
        assert gg.invest_today(1_000_000, -0.1, "non-negotiable", 0.06) == (0.0, 0.0)

    def test_mix_glides_toward_debt_as_the_date_approaches(self):
        debt_by_year = [gg.carve_shares(t + 0.5, "non-negotiable")[0] for t in range(5)]
        assert debt_by_year == [1.00, 0.75, 0.50, 0.25, 0.00]


class TestWorkedSingleCashflow:
    """Doc: 'Worked - a single cashflow of Rs 10,00,000', 6% inflation."""

    AMOUNT = 1_000_000
    INFLATION = 0.06
    CASES = [
        (0.5, "non-negotiable", 1_000_000, 0),
        (0.5, "semi-negotiable", 750_000, 246_529),
        (0.5, "negotiable", 500_000, 493_057),
        (1.5, "non-negotiable", 750_000, 239_748),
        (1.5, "semi-negotiable", 500_000, 239_748),
        (1.5, "negotiable", 300_000, 383_597),
        (2.5, "non-negotiable", 500_000, 233_154),
        (2.5, "semi-negotiable", 250_000, 233_154),
        (2.5, "negotiable", 0, 279_785),
        (3.5, "non-negotiable", 250_000, 226_741),
        (3.5, "semi-negotiable", 0, 226_741),
        (3.5, "negotiable", 0, 0),
        (4.5, "non-negotiable", 0, 220_488),
        (4.5, "semi-negotiable", 0, 0),
        (5.5, "non-negotiable", 0, 0),
    ]

    @pytest.mark.parametrize("t,neg,exp_debt,exp_hybrid", CASES)
    def test_matches_document(self, t, neg, exp_debt, exp_hybrid):
        debt, hybrid = gg.invest_today(self.AMOUNT, t, neg, self.INFLATION)
        assert debt == pytest.approx(exp_debt, abs=RUPEE)
        assert hybrid == pytest.approx(exp_hybrid, abs=RUPEE)


class TestWorkedInflationCannotBeShortcut:
    """Doc: 'Worked - why inflation cannot be shortcut', 3.5y non-negotiable."""

    CASES = [
        (0.00, 203_890, 184_921),
        (0.06, 250_000, 226_741),
        (0.08, 266_897, 242_066),
        (0.12, 303_115, 274_915),
    ]

    @pytest.mark.parametrize("inflation,exp_debt,exp_hybrid", CASES)
    def test_matches_document(self, inflation, exp_debt, exp_hybrid):
        debt, hybrid = gg.invest_today(1_000_000, 3.5, "non-negotiable", inflation)
        assert debt == pytest.approx(exp_debt, abs=RUPEE)
        assert hybrid == pytest.approx(exp_hybrid, abs=RUPEE)

    def test_shortcut_only_holds_when_inflation_equals_debt_growth(self):
        """At 6% the factors cancel and debt collapses to amount x grid%."""
        debt_at_6, _ = gg.invest_today(1_000_000, 3.5, "non-negotiable", 0.06)
        assert debt_at_6 == pytest.approx(1_000_000 * 0.25, abs=1.0)
        debt_at_12, _ = gg.invest_today(1_000_000, 3.5, "non-negotiable", 0.12)
        shortfall = debt_at_12 - 1_000_000 * 0.25
        assert shortfall == pytest.approx(53_115, abs=100)  # doc: 17.5% short


class TestPurposeIsDerived:
    """Replenishing is derived from the cashflow series, never typed."""

    PLAN = pd.Timestamp("2026-08-01")

    def test_eight_quarterly_payouts_inside_two_years_are_non_replenishing(self):
        goal = {"structure": "recurring", "frequency": "Quarterly", "occurrences": 8,
                "amount": 100_000, "start_date": self.PLAN,
                "negotiability": "non-negotiable", "inflation": 0.06}
        out = gg.goal_sleeves(goal, self.PLAN)
        assert out["beyond"] == 0
        assert out["purpose"] == gg.GOAL_NON_REPLENISH

    def test_a_long_income_stream_is_replenishing(self):
        goal = {"structure": "recurring", "frequency": "Monthly", "occurrences": 487,
                "amount": 125_000, "start_date": self.PLAN,
                "negotiability": "non-negotiable", "inflation": 0.06}
        out = gg.goal_sleeves(goal, self.PLAN)
        # 61, NOT the naive 60. The 61st cashflow is 60 calendar months out =
        # 1826 days (a leap year in there) = 4.9993 years on the 365.25
        # convention, so floor(t) is 4 and it still lands in the 4-5 band. This
        # is the document's own warning that "365.25 is load-bearing" -- counting
        # whole months / 12 would drop an occurrence.
        assert out["inside"] == 61
        assert out["beyond"] == 426
        assert out["inside"] + out["beyond"] == 487
        assert out["purpose"] == gg.GOAL_REPLENISH

    def test_reach_shortens_the_window_for_negotiable_goals(self):
        base = {"structure": "recurring", "frequency": "Annual", "occurrences": 5,
                "amount": 100_000, "start_date": self.PLAN, "inflation": 0.06}
        nn = gg.goal_sleeves(dict(base, negotiability="non-negotiable"), self.PLAN)
        neg = gg.goal_sleeves(dict(base, negotiability="negotiable"), self.PLAN)
        assert nn["inside"] == 5 and nn["purpose"] == gg.GOAL_NON_REPLENISH
        assert neg["inside"] == 3 and neg["purpose"] == gg.GOAL_REPLENISH


class TestTaxLayer:
    """Layer 4 follows the engine's existing gross-up convention."""

    def test_gross_up_matches_engine_convention(self):
        from app.planning.engine import calculate_goal_cashflows  # noqa: F401
        target, ret, t = 100_000.0, 0.06, 3.0
        ltcg = _DEFAULT_INSTRUMENT_PARAMS["debt"]["ltcg_tax"]
        got = gg.gross_up_for_tax(target, ret, t, 0.20, ltcg)
        expected = target / ((1 + ret) ** t * (1 - ltcg) + ltcg)
        assert got == pytest.approx(expected, rel=1e-12)

    def test_tax_makes_the_sleeve_bigger_not_smaller(self):
        plain = gg.invest_today(1_000_000, 3.5, "non-negotiable", 0.06)
        taxed = gg.invest_today_taxed(1_000_000, 3.5, "non-negotiable", 0.06)
        assert taxed[0] > plain[0]
        assert taxed[1] > plain[1]

    def test_stcg_applies_within_a_year(self):
        within = gg.gross_up_for_tax(100_000.0, 0.06, 0.9, 0.20, 0.125)
        beyond = gg.gross_up_for_tax(100_000.0, 0.06, 1.1, 0.20, 0.125)
        assert within > 0 and beyond > 0


class TestOrdering:
    PLAN = pd.Timestamp("2026-08-01")

    def test_negotiability_then_earliest_then_larger(self):
        goals = [
            {"name": "neg", "negotiability": "negotiable", "structure": "one-time",
             "amount": 500_000, "start_date": pd.Timestamp("2028-01-01")},
            {"name": "nn_late", "negotiability": "non-negotiable", "structure": "one-time",
             "amount": 100_000, "start_date": pd.Timestamp("2030-01-01")},
            {"name": "nn_early_small", "negotiability": "non-negotiable",
             "structure": "one-time", "amount": 100_000,
             "start_date": pd.Timestamp("2027-01-01")},
            {"name": "nn_early_big", "negotiability": "non-negotiable",
             "structure": "one-time", "amount": 900_000,
             "start_date": pd.Timestamp("2027-01-01")},
            {"name": "semi", "negotiability": "semi-negotiable", "structure": "one-time",
             "amount": 100_000, "start_date": pd.Timestamp("2027-01-01")},
        ]
        order = [g["name"] for g in gg.order_goals(goals, self.PLAN)]
        assert order == ["nn_early_big", "nn_early_small", "nn_late", "semi", "neg"]


class TestCashflowExpansion:
    PLAN = pd.Timestamp("2026-08-01")

    def test_one_time_is_a_single_cashflow(self):
        goal = {"structure": "one-time", "amount": 250_000,
                "start_date": pd.Timestamp("2030-03-01")}
        assert gg.expand_goal_to_cashflows(goal, self.PLAN) == [
            (pd.Timestamp("2030-03-01"), 250_000.0)]

    def test_recurring_steps_by_frequency(self):
        goal = {"structure": "recurring", "frequency": "Quarterly", "occurrences": 4,
                "amount": 50_000, "start_date": pd.Timestamp("2027-01-01")}
        dates = [d for d, _a in gg.expand_goal_to_cashflows(goal, self.PLAN)]
        assert dates == [pd.Timestamp("2027-01-01"), pd.Timestamp("2027-04-01"),
                         pd.Timestamp("2027-07-01"), pd.Timestamp("2027-10-01")]

    def test_month_end_clamps(self):
        goal = {"structure": "recurring", "frequency": "Monthly", "occurrences": 3,
                "amount": 1_000, "start_date": pd.Timestamp("2027-01-31")}
        dates = [d for d, _a in gg.expand_goal_to_cashflows(goal, self.PLAN)]
        assert dates == [pd.Timestamp("2027-01-31"), pd.Timestamp("2027-02-28"),
                         pd.Timestamp("2027-03-31")]

    def test_recurring_without_frequency_is_rejected(self):
        goal = {"structure": "recurring", "occurrences": 3, "amount": 1_000,
                "start_date": self.PLAN}
        with pytest.raises(ValueError):
            gg.expand_goal_to_cashflows(goal, self.PLAN)
