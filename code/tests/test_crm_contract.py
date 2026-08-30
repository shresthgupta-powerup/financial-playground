"""The CRM goals contract (Punit's spec, 2026-08-30).

The CRM interprets nothing: a missing key, an unknown key, a null where one is
not allowed, or a token in the wrong case rejects that goal with the reason
shown to the uploader. So these tests pin the produced file against the spec
key by key and token by token - a drift here is a silent rejection for a CM.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "code"))

_spec = importlib.util.spec_from_file_location("sa_crm", REPO / "streamlit_app.py")
sa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sa)

from app.planning.engine import find_retirement_date  # noqa: E402

# The eleven keys, in the spec's order. Every goal carries all of them.
CONTRACT_KEYS = [
    "purpose_id", "goal_name", "goal_type", "goal_negotiability",
    "goal_description", "amount_per_occurrence", "occurrences", "frequency",
    "start_date", "inflation", "goal_status",
]
NEGOTIABILITY = {"non_negotiable", "semi_negotiable", "negotiable"}
FREQUENCIES = {"monthly", "quarterly", "half_yearly", "yearly",
               "every_other_year"}
CATEGORIES = {"education", "marriage", "home", "vehicle", "travel",
              "retirement", "healthcare", "emergency", "business", "other"}

TODAY = pd.Timestamp("2026-08-01")


def _goal(**kw):
    g = {"name": "Goal", "description": "", "type": "Non-Negotiable",
         "structure": "Lumpsum", "start_date_mode": "Fixed",
         "start_date": TODAY + pd.DateOffset(years=5), "amount": 1_000_000,
         "frequency": None, "end_mode": None, "occurrences": 1,
         "end_date": None, "inflation_percent": 6.0,
         "purpose_id": None, "goal_category": None}
    g.update(kw)
    return g


def _config(goals, corpus=30_000_000):
    return {
        "current_date": TODAY, "current_age": 40, "target_lifetime": 90,
        "current_corpus": corpus, "risk_profile": "Balanced",
        "client_name": "T", "investment_streams": [], "one_time_investments": [],
        "goals": goals,
    }


def _upload(goals, retirement=None):
    cfg = _config(goals)
    return json.loads(sa.crm_goals_upload_json(cfg, retirement or TODAY))


class TestUploadFileShape:
    def test_envelope_is_goals_only(self):
        doc = _upload([_goal()])
        assert list(doc.keys()) == ["goals"]

    def test_every_goal_carries_exactly_the_eleven_keys_in_order(self):
        doc = _upload([
            _goal(name="Lumpsum"),
            _goal(name="Series", structure="Recurring", frequency="Annual",
                  end_mode="Occurrences", occurrences=4),
        ])
        for g in doc["goals"]:
            assert list(g.keys()) == CONTRACT_KEYS

    def test_tokens_are_the_crm_vocabulary(self):
        doc = _upload([
            _goal(name="A", type="Non-Negotiable", goal_category="education",
                  structure="Recurring", frequency="Half-Yearly",
                  end_mode="Occurrences", occurrences=6),
            _goal(name="B", type="Semi-Negotiable", goal_category="travel"),
            _goal(name="C", type="Negotiable"),
        ])
        for g in doc["goals"]:
            assert g["goal_negotiability"] in NEGOTIABILITY
            assert g["goal_type"] is None or g["goal_type"] in CATEGORIES
            assert g["frequency"] is None or g["frequency"] in FREQUENCIES
            assert g["goal_status"] == "active"
        # our "Annual" is their "yearly"; "Half-Yearly" is "half_yearly"
        assert doc["goals"][0]["frequency"] == "half_yearly"
        assert _upload([_goal(structure="Recurring", frequency="Annual",
                              end_mode="Occurrences", occurrences=3)]
                       )["goals"][0]["frequency"] == "yearly"

    def test_amount_is_whole_rupees_and_inflation_is_a_fraction(self):
        g = _upload([_goal(amount=1_234_567.89, inflation_percent=8.0)])["goals"][0]
        assert g["amount_per_occurrence"] == 1_234_568
        assert isinstance(g["amount_per_occurrence"], int)
        assert g["inflation"] == pytest.approx(0.08)

    def test_frequency_is_null_exactly_when_one_occurrence(self):
        for g in _upload([
            _goal(name="One"),
            _goal(name="Many", structure="Recurring", frequency="Monthly",
                  end_mode="Occurrences", occurrences=12),
        ])["goals"]:
            assert (g["frequency"] is None) == (g["occurrences"] == 1)

    def test_start_date_is_always_a_first_of_month(self):
        g = _upload([_goal(start_date=pd.Timestamp("2031-03-01"))])["goals"][0]
        assert g["start_date"] == "2031-03-01"

    def test_description_is_a_string_never_null(self):
        g = _upload([_goal(description=None)])["goals"][0]
        assert g["goal_description"] == ""


class TestOpenEndedAndResolution:
    """Their two hard requirements on open-ended series."""

    def test_lifetime_series_is_written_as_the_open_ended_marker(self):
        g = _upload([_goal(name="Income", structure="Recurring",
                           frequency="Monthly", end_mode="Lifetime",
                           occurrences=None)])["goals"][0]
        assert g["occurrences"] == sa.CRM_OPEN_ENDED_OCCURRENCES == 500

    def test_a_bounded_series_keeps_its_real_count(self):
        """Even one that starts at retirement: its length IS known."""
        g = _upload([_goal(name="Bridge", structure="Recurring",
                           start_date_mode="At retirement", start_date=None,
                           frequency="Monthly", end_mode="Occurrences",
                           occurrences=240)],
                    retirement=pd.Timestamp("2035-06-01"))["goals"][0]
        assert g["occurrences"] == 240

    def test_at_retirement_start_is_resolved_to_a_concrete_date(self):
        g = _upload([_goal(name="Income", structure="Recurring",
                           start_date_mode="At retirement", start_date=None,
                           frequency="Monthly", end_mode="Lifetime",
                           occurrences=None)],
                    retirement=pd.Timestamp("2035-06-01"))["goals"][0]
        assert g["start_date"] == "2035-06-01"


class TestRoundTrip:
    """CRM-minted ids must ride back, and the income policy must survive."""

    def _round_trip(self, goals, retirement=None):
        doc = _upload(goals, retirement)
        _p, _s, back, _o = sa.form_state_from_inputs({"goals": doc["goals"]})
        return doc, back

    def test_purpose_ids_survive_the_trip(self):
        doc, back = self._round_trip([_goal(purpose_id="PUR_123")])
        assert doc["goals"][0]["purpose_id"] == "PUR_123"
        assert back[0]["purpose_id"] == "PUR_123"

    def test_a_new_goal_uploads_a_null_id(self):
        assert _upload([_goal()])["goals"][0]["purpose_id"] is None

    def test_income_still_inflates_after_a_crm_round_trip(self):
        """The contract drops end_mode/start_date_mode, which is what our
        fixed-vs-inflating policy reads. The open-ended marker (500) is the
        only surviving signal - losing it would silently make a retirement
        income stop tracking cost of living.
        """
        _doc, back = self._round_trip(
            [_goal(name="Income", structure="Recurring", frequency="Monthly",
                   end_mode="Lifetime", occurrences=None)],
            retirement=pd.Timestamp("2035-06-01"))
        assert back[0]["end_mode"] == "Lifetime"
        assert sa.payments_fixed_for(back[0]) is False

    def test_a_fixed_series_stays_fixed_after_a_round_trip(self):
        _doc, back = self._round_trip(
            [_goal(name="Fees", structure="Recurring", frequency="Annual",
                   end_mode="Occurrences", occurrences=4)])
        assert sa.payments_fixed_for(back[0]) is True

    def test_amount_frequency_and_inflation_come_back_intact(self):
        _doc, back = self._round_trip(
            [_goal(name="Fees", structure="Recurring", frequency="Quarterly",
                   end_mode="Occurrences", occurrences=8, amount=90_000,
                   inflation_percent=5.0, goal_category="education")])
        g = back[0]
        assert g["amount"] == 90_000
        assert g["frequency"] == "Quarterly"
        assert g["inflation_percent"] == pytest.approx(5.0)
        assert g["goal_category"] == "education"


class TestShippedSampleMatchesTheContract:
    def test_sample_file_is_valid(self):
        path = REPO / "crm_samples" / "sample_crm_goals_upload.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert list(doc.keys()) == ["goals"] and doc["goals"]
        for g in doc["goals"]:
            assert list(g.keys()) == CONTRACT_KEYS
            assert g["goal_negotiability"] in NEGOTIABILITY
            assert g["goal_type"] is None or g["goal_type"] in CATEGORIES
            assert g["frequency"] is None or g["frequency"] in FREQUENCIES
            assert isinstance(g["amount_per_occurrence"], int)
            assert isinstance(g["occurrences"], int) and g["occurrences"] >= 1
            assert (g["frequency"] is None) == (g["occurrences"] == 1)
            assert g["goal_status"] == "active"
        names = [g["goal_name"].lower() for g in doc["goals"]]
        assert len(names) == len(set(names)), "goal_name unique per plan"


class TestWeRefuseWhatWeCannotRepresent:
    def test_every_other_year_is_rejected_not_silently_monthlyfied(self):
        """Their enum has every_other_year; our engine has no 24-month step.
        Falling through would hit normalise_goal's "Monthly" default and
        over-fund the goal twelvefold, so the loader must refuse instead.
        """
        row = {
            "purpose_id": None, "goal_name": "Biennial", "goal_type": "other",
            "goal_negotiability": "negotiable", "goal_description": "",
            "amount_per_occurrence": 100_000, "occurrences": 10,
            "frequency": "every_other_year", "start_date": "2030-01-01",
            "inflation": 0.06, "goal_status": "active",
        }
        with pytest.raises(ValueError, match="every_other_year"):
            sa.form_state_from_inputs({"goals": [row]})

    def test_the_frequencies_we_do_support_still_load(self):
        for token in ("monthly", "quarterly", "half_yearly", "yearly"):
            row = {
                "purpose_id": None, "goal_name": "G", "goal_type": None,
                "goal_negotiability": "negotiable", "goal_description": "",
                "amount_per_occurrence": 1000, "occurrences": 5,
                "frequency": token, "start_date": "2030-01-01",
                "inflation": 0.06, "goal_status": "active",
            }
            _p, _s, goals, _o = sa.form_state_from_inputs({"goals": [row]})
            assert goals[0]["frequency"] in ("Monthly", "Quarterly",
                                             "Half-Yearly", "Annual")
