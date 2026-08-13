# activities/loan_activities.py
from temporalio import activity
from shared.models import WizardData, LoanDecision


def _score(application_id: str) -> int:
    h = 7
    for c in application_id:
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    return 580 + (h % 260)  # deterministic, so demonstrations are reproducible


@activity.defn
def reserve_underwriting_slot(application_id: str) -> None:
    ...  # mock: idempotent reservation keyed by application_id


@activity.defn
def release_underwriting_slot(application_id: str) -> None:
    ...  # mock: safe even if nothing was reserved (idempotent compensation)


@activity.defn
def pull_credit_report(application_id: str) -> int:
    return _score(application_id)  # idempotency key is application_id


@activity.defn
def run_decision_engine(application_id: str, data: WizardData, score: int) -> LoanDecision:
    # Numeric fields are normalized to numbers when the step is written
    income = (data.employment or {}).get("annual_income", 0) or 0
    amount = (data.loan_details or {}).get("amount", 0) or 0
    if score >= 720 and amount <= income:
        return LoanDecision(
            outcome="approved", reason="Strong credit and affordable amount",
            reference_id=application_id,
        )
    if score < 600:
        return LoanDecision(
            outcome="rejected", reason="Credit score below threshold",
            reference_id=application_id,
        )
    # Manual review: state the actual trigger so the underwriter (and the
    # applicant) sees why it was referred, not a circular "needs review".
    if score >= 720:
        # Credit was strong enough to auto-approve; the amount is what held it back.
        reason = f"Requested amount ${amount:,.0f} exceeds annual income ${income:,.0f}"
    else:
        reason = f"Credit score {score} is in the manual-review band (600–719)"
    return LoanDecision(
        outcome="manual_review", reason=reason, reference_id=application_id,
    )


@activity.defn
def notify_applicant(application_id: str, decision: LoanDecision) -> None:
    ...  # mock: idempotent on application_id plus outcome


@activity.defn
def send_reminder(application_id: str, email: str | None) -> None:
    ...  # mock: inactivity reminder


# The full set of Activities, so the Worker and the tests register the same list
# from one place instead of re-spelling it in each.
ALL = [
    reserve_underwriting_slot,
    release_underwriting_slot,
    pull_credit_report,
    run_decision_engine,
    notify_applicant,
    send_reminder,
]
