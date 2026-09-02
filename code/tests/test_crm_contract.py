"""The CRM goals contract (Punit's spec v2, 2026-08-31).

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

# The twelve keys, in the spec's order. Every goal carries all of them.
# v2 (2026-08-31): purpose_id dropped (the upload is ADD-only, so nothing in
# the file addresses an existing goal); lifetime and payments_fixed_at_start
# added as their own booleans, retiring the 500-occurrence sentinel.
CONTRACT_KEYS = [
    "goal_name", "goal_type", "goal_negotiability", "goal_description",
    "amount_per_occurrence", "occurrences", "lifetime",
    "payments_fixed_at_start", "frequency", "start_date", "inflation",
    "goal_status",
]
# Null is allowed only here: goal_type, goal_description, and the three
# recurring-only fields exactly when occurrences == 1.
NULLABLE_ALWAYS = {"goal_type", "goal_description"}
NULLABLE_WHEN_SINGLE = {"frequency", "lifetime", "payments_fixed_at_start"}
NEGOTIABILITY = {"non_negotiable", "semi_negotiable", "negotiable"}
# v2 removed every_other_year from their enum too, so the asymmetry is gone.
FREQUENCIES = {"monthly", "quarterly", "half_yearly", "yearly"}
CATEGORIES = {"education", "marriage", "home", "vehicle", "travel",
              "retirement", "healthcare", "emergency", "business", "other"}

TODAY = pd.Timestamp("2026-08-01")


def _goal(**kw):
    g = {"name": "Goal", "description": "", "type": "Non-Negotiable",
         "structure": "Lumpsum", "start_date_mode": "Fixed",
         "start_date": TODAY + pd.DateOffset(years=5), "amount": 1_000_000,
         "frequency": None, "end_mode": None, "occurrences": 1,
         "end_date": None, "inflation_percent": 6.0,
         "goal_category": None}
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


class TestLifetimeAndResolution:
    """v2: `lifetime` is its own boolean and `occurrences` never lies."""

    def test_lifetime_series_sets_the_flag_and_a_true_count(self):
        g = _upload([_goal(name="Income", structure="Recurring",
                           frequency="Monthly", end_mode="Lifetime",
                           occurrences=None)])["goals"][0]
        assert g["lifetime"] is True
        assert g["occurrences"] > 1
        assert g["occurrences"] != 500          # never a sentinel

    def test_a_bounded_series_is_not_lifetime(self):
        g = _upload([_goal(name="Bridge", structure="Recurring",
                           start_date_mode="At retirement", start_date=None,
                           frequency="Monthly", end_mode="Occurrences",
                           occurrences=240)],
                    retirement=pd.Timestamp("2035-06-01"))["goals"][0]
        assert g["lifetime"] is False
        assert g["occurrences"] == 240

    def test_policy_flag_rides_along_verbatim(self):
        doc = _upload([
            _goal(name="Income", structure="Recurring", frequency="Monthly",
                  end_mode="Lifetime", occurrences=None),
            _goal(name="Fees", structure="Recurring", frequency="Annual",
                  end_mode="Occurrences", occurrences=4),
        ])
        income, fees = doc["goals"]
        assert income["payments_fixed_at_start"] is False
        assert fees["payments_fixed_at_start"] is True

    def test_single_occurrence_nulls_the_three_recurring_fields(self):
        g = _upload([_goal()])["goals"][0]
        assert g["occurrences"] == 1
        for k in NULLABLE_WHEN_SINGLE:
            assert g[k] is None

    def test_nothing_else_is_ever_null(self):
        doc = _upload([
            _goal(name="One", goal_category="home"),
            _goal(name="Many", goal_category="education", structure="Recurring",
                  frequency="Monthly", end_mode="Occurrences", occurrences=12),
        ])
        for g in doc["goals"]:
            for k, v in g.items():
                if k in NULLABLE_ALWAYS:
                    continue
                if k in NULLABLE_WHEN_SINGLE and g["occurrences"] == 1:
                    continue
                assert v is not None, f"{k} must not be null"

    def test_at_retirement_start_is_resolved_to_a_concrete_date(self):
        g = _upload([_goal(name="Income", structure="Recurring",
                           start_date_mode="At retirement", start_date=None,
                           frequency="Monthly", end_mode="Lifetime",
                           occurrences=None)],
                    retirement=pd.Timestamp("2035-06-01"))["goals"][0]
        assert g["start_date"] == "2035-06-01"


class TestRoundTrip:
    """A CRM file must load back with its meaning intact."""

    def _round_trip(self, goals, retirement=None):
        doc = _upload(goals, retirement)
        _p, _s, back, _o = sa.form_state_from_inputs({"goals": doc["goals"]})
        return doc, back

    def test_income_still_inflates_after_a_crm_round_trip(self):
        """v1 keyed this off occurrences == 500, which Punit caught failing on
        a real plan (an income exported 397). v2 carries `lifetime`, so the
        classification survives whatever the count happens to be.
        """
        _doc, back = self._round_trip(
            [_goal(name="Income", structure="Recurring", frequency="Monthly",
                   end_mode="Lifetime", occurrences=None)],
            retirement=pd.Timestamp("2035-06-01"))
        assert back[0]["end_mode"] == "Lifetime"
        assert sa.payments_fixed_for(back[0]) is False

    def test_a_bounded_series_stays_contract_fixed(self):
        _doc, back = self._round_trip(
            [_goal(name="Fees", structure="Recurring", frequency="Annual",
                   end_mode="Occurrences", occurrences=4)])
        assert sa.payments_fixed_for(back[0]) is True

    def test_category_survives(self):
        _doc, back = self._round_trip([_goal(goal_category="education")])
        assert back[0]["goal_category"] == "education"


class TestOurOwnResolvedFileKeepsItsMeaning:
    """The same loss existed in OUR save format, independently of the CRM.

    build_inputs_json(resolved) replaces Lifetime with a concrete count and
    "At retirement" with a date - erasing both signals the income policy
    reads, so reloading a resolved export silently turned a retirement income
    into a contract-fixed series that stopped escalating. The file now carries
    `lifetime` explicitly.
    """

    def test_resolved_export_reloads_as_income(self):
        cfg = _config([_goal(name="Income", structure="Recurring",
                             start_date_mode="At retirement", start_date=None,
                             frequency="Monthly", end_mode="Lifetime",
                             occurrences=None)])
        doc = json.loads(sa.build_inputs_json(
            cfg, retirement_date=pd.Timestamp("2035-06-01")))
        assert doc["goals"][0]["lifetime"] is True
        _p, _s, back, _o = sa.form_state_from_inputs(doc)
        assert sa.payments_fixed_for(back[0]) is False


class TestWeRefuseWhatWeCannotRepresent:
    def test_an_unmappable_frequency_is_rejected_not_silently_monthlyfied(self):
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
