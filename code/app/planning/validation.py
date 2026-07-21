"""Server-side input validation for the financial-planning engine (LP-015 C1, D-P202-6).

``validate_plan_config(config)`` is called at the engine entrypoint
(``engine.find_retirement_date``) so EVERY caller — the C2 HTTP boundary and any
direct/test caller — is guarded against malformed input *before* any simulation
runs. The v3 engine had no server-side validation: negative amounts "succeeded",
``lifetime <= age`` returned an infeasible result rather than an error, and a
monthly non-replenishing recurring goal with a long horizon fanned out into
hundreds of chain DataFrames (the perf cliff).

This is the engine-level (plain-dict) validation layer. C2 adds a second
Pydantic/HTTP-request layer on top; the two are complementary, not redundant.

Raises ``PlanValidationError`` (a ``ValueError`` subclass) listing every problem
found, so the caller can surface all input errors at once.
"""

import pandas as pd

# Span cap (D-P208-1, replaces D-P202-7 occurrence count cap):
# Non-replenishing recurring goals may not span more than 4 years from first
# to last occurrence.  Span = first-to-last occurrence gap:
#   Occurrences mode  → (occurrences - 1) * freq_months
#   Fixed-date mode   → calendar month diff start → end (same arithmetic as
#                       engine._resolve_recurring_occurrences)
#   Lifetime mode     → unconditional violation (unbounded by construction)
# Implied per-frequency maxima: 49 Monthly / 17 Quarterly / 9 Half-Yearly /
# 5 Annual.  Replenishing recurring goals are payout rows only and stay uncapped.
MAX_NONREPLENISHING_SPAN_MONTHS = 48

_VALID_FREQUENCIES = {"Monthly", "Quarterly", "Half-Yearly", "Annual"}
_FREQ_TO_MONTHS = {"Annual": 12, "Quarterly": 3, "Half-Yearly": 6, "Monthly": 1}


class PlanValidationError(ValueError):
    """Raised when a plan config fails server-side validation.

    ``errors`` carries the full list of human-readable problems found.
    """

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _is_replenishing(goal):
    return str(goal.get("nature", "")).lower() == "replenishing"


def _nonreplenishing_span_months(goal):
    """First-to-last occurrence span in months for a NON-replenishing recurring goal.

    Mirrors ``engine._resolve_recurring_occurrences`` for the relevant ``end_mode``
    branches but is self-contained (validation must not depend on the engine, to
    avoid an import cycle).

    Returns (span_months, is_lifetime) where is_lifetime=True signals an
    unconditional violation (Lifetime end_mode on a non-replenishing goal).
    When the frequency is invalid, returns (0, False) so the existing frequency
    error fires instead of a spurious span error.
    """
    if goal.get("structure") != "Recurring":
        return 0, False

    end_mode = goal.get("end_mode") or "Occurrences"
    freq_months = _FREQ_TO_MONTHS.get(goal.get("frequency"))

    if end_mode == "Lifetime":
        # Unbounded by construction — unconditional violation.
        return MAX_NONREPLENISHING_SPAN_MONTHS + 1, True

    if freq_months is None:
        # Invalid frequency — let the frequency-error path handle it.
        return 0, False

    if end_mode == "Occurrences":
        occ = int(goal.get("occurrences", 1) or 1)
        # span = (occ - 1) * freq_months  (first occurrence is at offset 0)
        return max(0, (occ - 1) * freq_months), False

    if end_mode == "Fixed date":
        start = goal.get("start_date")
        end = goal.get("end_date")
        if start is None or end is None:
            return 0, False
        start = pd.Timestamp(start)
        end = pd.Timestamp(end)
        if end < start:
            return 0, False
        months_span = (end.year - start.year) * 12 + (end.month - start.month)
        return months_span, False

    return 0, False


def validate_plan_config(config):
    """Validate a plan config dict. Raises ``PlanValidationError`` on any problem.

    Checks (D-P202-6, D-P208-1):
      - ``current_corpus`` present and >= 0.
      - ``target_lifetime > current_age`` (a positive remaining horizon).
      - Each investment stream: ``amount >= 0``; Fixed end_mode requires an
        ``end_date >= start_date``.
      - Each goal: ``amount >= 0``; recurring goals need a valid ``frequency``;
        ``occurrences >= 1`` where the end_mode requires it; Fixed-date recurring
        needs ``end_date >= start_date``; non-replenishing recurring first-to-last
        span must not exceed ``MAX_NONREPLENISHING_SPAN_MONTHS`` (D-P208-1).
      - Each one-time investment: ``amount >= 0``.
    """
    errors = []

    if not isinstance(config, dict):
        raise PlanValidationError(["config must be a dict"])

    # --- Personal & corpus -------------------------------------------------
    corpus = config.get("current_corpus")
    if corpus is None:
        errors.append("current_corpus is required")
    else:
        try:
            if float(corpus) < 0:
                errors.append("current_corpus must be >= 0")
        except (TypeError, ValueError):
            errors.append("current_corpus must be a number")

    current_age = config.get("current_age", 30)
    target_lifetime = config.get("target_lifetime", 90)
    try:
        if float(target_lifetime) <= float(current_age):
            errors.append("target_lifetime must be greater than current_age")
    except (TypeError, ValueError):
        errors.append("current_age and target_lifetime must be numbers")

    # --- Investment streams ------------------------------------------------
    streams = config.get("investment_streams", []) or []
    for i, stream in enumerate(streams):
        label = stream.get("name") or f"investment stream #{i + 1}"
        amount = stream.get("amount")
        try:
            if amount is None or float(amount) < 0:
                errors.append(f"{label}: amount must be >= 0")
        except (TypeError, ValueError):
            errors.append(f"{label}: amount must be a number")
        if stream.get("end_date_mode", "Fixed") == "Fixed":
            start = stream.get("start_date")
            end = stream.get("end_date")
            if end is None:
                errors.append(f"{label}: Fixed end_date_mode requires an end_date")
            elif start is not None and pd.Timestamp(end) < pd.Timestamp(start):
                errors.append(f"{label}: end_date must be on or after start_date")

    # --- Goals -------------------------------------------------------------
    goals = config.get("goals", []) or []
    for i, goal in enumerate(goals):
        label = goal.get("name") or f"goal #{i + 1}"
        amount = goal.get("amount")
        try:
            if amount is None or float(amount) < 0:
                errors.append(f"{label}: amount must be >= 0")
        except (TypeError, ValueError):
            errors.append(f"{label}: amount must be a number")

        structure = goal.get("structure", "Lumpsum")
        if structure == "Recurring":
            frequency = goal.get("frequency")
            if frequency not in _VALID_FREQUENCIES:
                errors.append(
                    f"{label}: recurring goal needs a valid frequency "
                    f"({', '.join(sorted(_VALID_FREQUENCIES))})"
                )
            end_mode = goal.get("end_mode") or "Occurrences"
            if end_mode == "Occurrences":
                occ = goal.get("occurrences")
                try:
                    if occ is None or int(occ) < 1:
                        errors.append(f"{label}: occurrences must be >= 1")
                except (TypeError, ValueError):
                    errors.append(f"{label}: occurrences must be an integer")
            elif end_mode == "Fixed date":
                start = goal.get("start_date")
                end = goal.get("end_date")
                if end is None:
                    errors.append(f"{label}: Fixed-date end_mode requires an end_date")
                elif start is not None and pd.Timestamp(end) < pd.Timestamp(start):
                    errors.append(f"{label}: end_date must be on or after start_date")

            # Span cap (D-P208-1) — non-replenishing recurring only.
            if not _is_replenishing(goal):
                span_months, is_lifetime = _nonreplenishing_span_months(goal)
                if is_lifetime:
                    errors.append(
                        f"{label}: non-replenishing recurring goal with Lifetime end_mode "
                        f"spans more than 4 years; shorten it or model it as a Replenishing goal."
                    )
                elif span_months > MAX_NONREPLENISHING_SPAN_MONTHS:
                    errors.append(
                        f"{label}: non-replenishing recurring goal spans {span_months} months "
                        f"— more than 4 years; shorten it or model it as a Replenishing goal."
                    )

    # --- One-time investments ---------------------------------------------
    one_time = config.get("one_time_investments", []) or []
    for i, w in enumerate(one_time):
        label = w.get("name") or f"one-time investment #{i + 1}"
        amount = w.get("amount")
        try:
            if amount is not None and float(amount) < 0:
                errors.append(f"{label}: amount must be >= 0")
        except (TypeError, ValueError):
            errors.append(f"{label}: amount must be a number")

    if errors:
        raise PlanValidationError(errors)
