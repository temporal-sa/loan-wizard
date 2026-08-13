"""Tests for the mocked Activities (task 2.2).

`run_decision_engine` takes `score` as a parameter, so its three branches are
tested directly without going through `pull_credit_report`. The credit score is
deterministic, and the side-effecting Activities are idempotent no-ops, so
calling them more than once is safe — which is what lets Temporal retry them.
"""
from temporalio.testing import ActivityEnvironment

from shared.models import LoanDecision, WizardData
from activities.loan_activities import (
    _score,
    pull_credit_report,
    reserve_underwriting_slot,
    release_underwriting_slot,
    run_decision_engine,
    notify_applicant,
    send_reminder,
)


# --- Deterministic credit score -------------------------------------------

def test_score_is_deterministic():
    assert _score("app-123") == _score("app-123")


def test_score_varies_by_application_id():
    assert _score("app-123") != _score("app-456")


def test_score_stays_in_expected_range():
    for app_id in ("a", "app-123", "97f3c0de-0000-4000-8000-000000000000"):
        assert 580 <= _score(app_id) <= 839


def test_pull_credit_report_returns_the_score():
    env = ActivityEnvironment()
    assert env.run(pull_credit_report, "app-123") == _score("app-123")


# --- Decision engine branches ---------------------------------------------

AFFORDABLE = WizardData(employment={"annual_income": 100000}, loan_details={"amount": 10000})
UNAFFORDABLE = WizardData(employment={"annual_income": 40000}, loan_details={"amount": 250000})


def _decide(data: WizardData, score: int) -> LoanDecision:
    return ActivityEnvironment().run(run_decision_engine, "app-1", data, score)


def test_decision_approved_on_strong_score_and_affordable_amount():
    decision = _decide(AFFORDABLE, 750)
    assert decision.outcome == "approved"
    assert decision.reference_id == "app-1"


def test_decision_rejected_on_low_score():
    assert _decide(AFFORDABLE, 550).outcome == "rejected"


def test_decision_manual_review_on_mid_score():
    # 600 <= score < 720 falls through to manual review.
    assert _decide(AFFORDABLE, 650).outcome == "manual_review"


def test_decision_manual_review_when_strong_score_but_unaffordable():
    # Strong score but amount exceeds income, so it is not auto-approved.
    assert _decide(UNAFFORDABLE, 800).outcome == "manual_review"


def test_decision_engine_tolerates_missing_or_blank_amounts():
    # Numeric fields are normalized upstream by coerce_step, but a missing or
    # blank field can still reach the engine; `or 0` must keep it from crashing
    # the `amount <= income` comparison.
    blank = WizardData(employment={"annual_income": ""}, loan_details={})
    assert _decide(blank, 650).outcome == "manual_review"


# --- Side-effecting Activities are idempotent no-op mocks -------------------

def test_side_effecting_activities_are_idempotent():
    env = ActivityEnvironment()
    decision = LoanDecision(outcome="approved", reason="ok", reference_id="app-1")
    # Calling each twice must be safe — the property Temporal relies on to retry.
    for _ in range(2):
        assert env.run(reserve_underwriting_slot, "app-1") is None
        assert env.run(release_underwriting_slot, "app-1") is None
        assert env.run(notify_applicant, "app-1", decision) is None
        assert env.run(send_reminder, "app-1", "ada@example.com") is None
        assert env.run(send_reminder, "app-1", None) is None
