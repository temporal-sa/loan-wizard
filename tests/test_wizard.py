"""Unit tests for the pure validation logic (task 1.3).

These exercise `validate_step` and `next_step` exhaustively per step. They need
no Temporal Cluster: the functions are pure, which is the whole point of keeping
validation in `shared/wizard.py`.
"""
import pytest

from shared.models import LoanStep
from shared.wizard import STEP_ORDER, coerce_step, next_step, validate_step


# --- Valid payloads: every step returns no errors --------------------------

VALID_PAYLOADS = {
    LoanStep.applicant: {
        "full_name": "Ada Lovelace",
        "email": "ada@example.com",
        "date_of_birth": "1990-01-01",
    },
    LoanStep.loan_details: {"amount": 10000.0, "term_months": 12, "purpose": "home"},
    LoanStep.employment: {
        "employer_name": "Analytical Engines",
        "annual_income": 50000.0,
        "status": "employed",
    },
    LoanStep.review: {"consent_given": True},
}


@pytest.mark.parametrize("step", list(LoanStep))
def test_valid_payload_has_no_errors(step):
    assert validate_step(step, VALID_PAYLOADS[step]) == []


# --- Applicant step --------------------------------------------------------

def test_applicant_missing_full_name():
    payload = {**VALID_PAYLOADS[LoanStep.applicant], "full_name": ""}
    assert "full_name is required" in validate_step(LoanStep.applicant, payload)


def test_applicant_invalid_email():
    payload = {**VALID_PAYLOADS[LoanStep.applicant], "email": "not-an-email"}
    assert "email is invalid" in validate_step(LoanStep.applicant, payload)


def test_applicant_missing_date_of_birth():
    payload = {**VALID_PAYLOADS[LoanStep.applicant], "date_of_birth": ""}
    assert "date_of_birth is required" in validate_step(LoanStep.applicant, payload)


def test_applicant_empty_payload_reports_every_error():
    errors = validate_step(LoanStep.applicant, {})
    assert set(errors) == {
        "full_name is required",
        "email is invalid",
        "date_of_birth is required",
    }


# --- Loan details step -----------------------------------------------------

def test_loan_details_amount_must_be_positive():
    payload = {**VALID_PAYLOADS[LoanStep.loan_details], "amount": 0}
    assert "amount must be positive" in validate_step(LoanStep.loan_details, payload)


def test_loan_details_term_below_minimum():
    payload = {**VALID_PAYLOADS[LoanStep.loan_details], "term_months": 5}
    assert "term_months must be at least 6" in validate_step(LoanStep.loan_details, payload)


def test_loan_details_term_at_minimum_is_valid():
    # Boundary: 6 months is the minimum accepted term.
    payload = {**VALID_PAYLOADS[LoanStep.loan_details], "term_months": 6}
    assert validate_step(LoanStep.loan_details, payload) == []


def test_loan_details_missing_fields_report_both_errors():
    errors = validate_step(LoanStep.loan_details, {})
    assert set(errors) == {"amount must be positive", "term_months must be at least 6"}


# --- Employment step -------------------------------------------------------

def test_employment_missing_employer_name():
    payload = {**VALID_PAYLOADS[LoanStep.employment], "employer_name": ""}
    assert "employer_name is required" in validate_step(LoanStep.employment, payload)


def test_employment_negative_income():
    payload = {**VALID_PAYLOADS[LoanStep.employment], "annual_income": -1}
    assert "annual_income must be zero or greater" in validate_step(LoanStep.employment, payload)


def test_employment_zero_income_is_valid():
    # Boundary: zero income is accepted (an unemployed applicant, for example).
    payload = {**VALID_PAYLOADS[LoanStep.employment], "annual_income": 0}
    assert validate_step(LoanStep.employment, payload) == []


def test_employment_missing_income_is_invalid():
    # A missing income is treated as invalid, not as zero.
    payload = {"employer_name": "Analytical Engines", "status": "employed"}
    assert "annual_income must be zero or greater" in validate_step(LoanStep.employment, payload)


# --- Review step -----------------------------------------------------------

def test_review_consent_false_is_rejected():
    assert validate_step(LoanStep.review, {"consent_given": False}) == ["consent is required"]


def test_review_consent_missing_is_rejected():
    assert validate_step(LoanStep.review, {}) == ["consent is required"]


# --- coerce_step -----------------------------------------------------------

def test_coerce_step_casts_string_numbers():
    # The values downstream arithmetic depends on (amount, term_months,
    # annual_income) are cast to numbers when they arrive as strings.
    loan = coerce_step(LoanStep.loan_details, {"amount": "10000", "term_months": "24", "purpose": "home"})
    assert loan == {"amount": 10000.0, "term_months": 24, "purpose": "home"}
    assert isinstance(loan["amount"], float) and isinstance(loan["term_months"], int)

    emp = coerce_step(LoanStep.employment, {"annual_income": "50000", "employer_name": "Acme"})
    assert emp["annual_income"] == 50000.0 and isinstance(emp["annual_income"], float)


def test_coerce_step_handles_partial_payloads():
    # Autosave drafts are partial: only present numeric keys are touched.
    assert coerce_step(LoanStep.loan_details, {"amount": "500"}) == {"amount": 500.0}
    assert coerce_step(LoanStep.loan_details, {"purpose": "auto"}) == {"purpose": "auto"}


def test_coerce_step_leaves_blank_and_unparseable_as_is():
    # Blank/garbage values are left for validate_step to reject, not forced to 0.
    assert coerce_step(LoanStep.loan_details, {"amount": ""}) == {"amount": ""}
    assert coerce_step(LoanStep.employment, {"annual_income": "n/a"}) == {"annual_income": "n/a"}


def test_coerce_step_accepts_dotted_term_months():
    # "24.0" must not crash int(); it routes through float first.
    assert coerce_step(LoanStep.loan_details, {"term_months": "24.0"}) == {"term_months": 24}


def test_coerce_step_is_noop_for_steps_without_numeric_fields():
    payload = {"full_name": "Ada", "email": "ada@example.com"}
    assert coerce_step(LoanStep.applicant, payload) == payload


def test_coerce_step_does_not_mutate_input():
    original = {"amount": "10000"}
    coerce_step(LoanStep.loan_details, original)
    assert original == {"amount": "10000"}  # a copy is returned, input untouched


# --- next_step -------------------------------------------------------------

@pytest.mark.parametrize(
    "step, expected",
    [
        (LoanStep.applicant, LoanStep.loan_details),
        (LoanStep.loan_details, LoanStep.employment),
        (LoanStep.employment, LoanStep.review),
    ],
)
def test_next_step_advances(step, expected):
    assert next_step(step) == expected


def test_next_step_at_last_step_is_none():
    assert next_step(LoanStep.review) is None


def test_step_order_covers_every_step_once():
    assert STEP_ORDER == list(LoanStep)
