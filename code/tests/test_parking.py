"""Goal parking: the grid's dated movements for a CRM goal, from the row alone."""
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))

from app.planning.parking import (  # noqa: E402
    STATUS_COMPLETED, STATUS_NOT_STARTED, STATUS_UNDERWAY,
    goal_from_purpose_row, parking_plan, plan_purposes,
)

AS_OF = pd.Timestamp("2026-09-01")


def _row(**kw):
    r = {"purpose_id": "PUR_TEST", "infinite_id": "INF_TEST", "purpose_type": "goal",
         "goal_name": "Goal", "goal_type": "other", "goal_negotiability": "non_negotiable",
         "goal_description": "", "amount_per_occurrence": 1_000_000, "occurrences": 1,
         "lifetime": None, "payments_fixed_at_start": None, "frequency": None,
         "start_date": "2033-06-01", "inflation": 0.08, "goal_status": "active",
         "amount_as_of": "2026-09-01"}
    r.update(kw)
    return r


class TestRowConversion:
    def test_tokens_and_flags(self):
        g = goal_from_purpose_row(_row(goal_negotiability="semi_negotiable",
                                       occurrences=12, frequency="half_yearly",
                                       lifetime=1.0, payments_fixed_at_start=0.0))
        assert g["type"] == "Semi-Negotiable"
        assert g["structure"] == "Recurring" and g["frequency"] == "Half-Yearly"
        assert g["lifetime"] is True and g["payments_fixed_at_start"] is False
        assert g["inflation_percent"] == pytest.approx(8.0)

    def test_lumpsum_ignores_recurring_fields(self):
        g = goal_from_purpose_row(_row(occurrences=1, frequency=float("nan"),
                                       lifetime=float("nan")))
        assert g["structure"] == "Lumpsum" and g["frequency"] is None
        assert g["payments_fixed_at_start"] is False

    def test_unknown_frequency_is_refused(self):
        with pytest.raises(ValueError, match="frequency"):
            goal_from_purpose_row(_row(occurrences=4, frequency="every_other_year"))

    def test_amount_as_of_falls_back_to_created_at(self):
        g = goal_from_purpose_row(_row(amount_as_of=float("nan"), goal_created_at="2026-05-14"))
        assert g["amount_as_of"] == pd.Timestamp("2026-05-01")


class TestFarGoalNotStarted:
    def test_first_move_is_five_years_before_a_non_negotiable_payment(self):
        p = parking_plan(goal_from_purpose_row(_row(start_date="2033-06-01")), AS_OF)
        assert p["status"] == STATUS_NOT_STARTED
        assert p["due_now"] == {"debt": 0.0, "hybrid": 0.0}
        # first slice enters the month the payment comes within 5 years
        assert p["first_move"] == pd.Timestamp("2028-06-01")
        first = [e for e in p["events"] if e["month"] == p["first_move"]]
        assert {e["bucket"] for e in first} == {"hybrid"}

    def test_negotiable_reach_is_three_years(self):
        """365.25 is load-bearing (doc SS4.2): Jun 2030 is 1,096 days before
        Jun 2033, which is >= 3 x 365.25, so it still floors to row 3. The
        first row-2 month - the first movement - is Jul 2030, not the
        calendar anniversary. Same leap-day effect as the 61-not-60 case."""
        p = parking_plan(goal_from_purpose_row(_row(goal_negotiability="negotiable",
                                                    start_date="2033-06-01")), AS_OF)
        assert p["first_move"] == pd.Timestamp("2030-07-01")


class TestNearGoalUnderway:
    def test_catch_up_lump_is_due_now_and_switch_is_scheduled(self):
        # 1.3 years out, non-negotiable: inside the window at as-of, so the
        # slices for the rows already passed are due NOW.
        p = parking_plan(goal_from_purpose_row(_row(start_date="2028-01-01",
                                                    amount_per_occurrence=6_000_000)), AS_OF)
        assert p["status"] == STATUS_UNDERWAY
        assert p["first_move"] == AS_OF
        assert p["due_now"]["debt"] > 0 and p["due_now"]["hybrid"] > 0
        switches = [e for e in p["events"] if e["kind"] == "switch"]
        assert switches and switches[0]["month"] == pd.Timestamp("2027-01-01")
        assert p["next_move"]["month"] == pd.Timestamp("2027-01-01")

    def test_slices_deliver_exactly_the_payment(self):
        p = parking_plan(goal_from_purpose_row(_row(start_date="2028-01-01",
                                                    amount_per_occurrence=6_000_000)), AS_OF)
        (_cf, fv), = p["remaining"]
        delivered = sum(e["delivers"] for e in p["events"] if e["kind"] == "add")
        assert delivered == pytest.approx(fv, rel=1e-9)


class TestEscalationAnchor:
    def test_amount_grows_from_amount_as_of_not_from_today(self):
        early = parking_plan(goal_from_purpose_row(_row(amount_as_of="2025-09-01")), AS_OF)
        late = parking_plan(goal_from_purpose_row(_row(amount_as_of="2026-09-01")), AS_OF)
        assert early["remaining_total"] > late["remaining_total"]
        assert early["remaining_total"] / late["remaining_total"] == pytest.approx(1.08, rel=2e-3)


class TestSeries:
    def test_fixed_series_has_equal_payments(self):
        p = parking_plan(goal_from_purpose_row(_row(occurrences=4, frequency="yearly",
                                                    payments_fixed_at_start=1.0)), AS_OF)
        amounts = {round(fv, 4) for _d, fv in p["remaining"]}
        assert len(amounts) == 1

    def test_income_series_keeps_escalating_and_moves_monthly(self):
        p = parking_plan(goal_from_purpose_row(_row(goal_name="SWP", occurrences=120,
                                                    frequency="monthly", lifetime=1.0,
                                                    payments_fixed_at_start=0.0,
                                                    start_date="2028-01-01",
                                                    inflation=0.07)), AS_OF)
        fvs = [fv for _d, fv in p["remaining"]]
        assert fvs[-1] > fvs[0]
        months = sorted({e["month"] for e in p["events"] if e["kind"] == "add"})
        # movements land every month once the series is inside the window
        assert len(months) > 100

    def test_past_lumpsum_is_completed(self):
        p = parking_plan(goal_from_purpose_row(_row(start_date="2025-01-01")), AS_OF)
        assert p["status"] == STATUS_COMPLETED and p["events"] == []


class TestFamily:
    def test_totals_and_filters(self):
        rows = [
            _row(purpose_id="A", start_date="2028-01-01", amount_per_occurrence=6_000_000),
            _row(purpose_id="B", start_date="2033-06-01"),
            _row(purpose_id="C", goal_status="cancelled", start_date="2028-01-01"),
        ]
        plans, totals = plan_purposes(rows, AS_OF)
        assert [p["purpose_id"] for p in plans] == ["A", "B"]
        a = plans[0]
        assert totals["debt_now"] == pytest.approx(a["due_now"]["debt"])
        assert totals["hybrid_now"] == pytest.approx(a["due_now"]["hybrid"])
        assert totals["next_month"] == pd.Timestamp("2027-01-01")
