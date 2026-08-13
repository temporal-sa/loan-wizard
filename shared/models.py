# shared/models.py
from enum import Enum
from pydantic import BaseModel


class LoanStep(str, Enum):
    applicant = "applicant"
    loan_details = "loan_details"
    employment = "employment"
    review = "review"


# Per-step fields (full_name/email, amount/term_months/purpose, etc.) are not
# modeled as Pydantic types here on purpose: step data is carried as plain dicts
# in WizardData (see below) so it crosses to the JavaScript interface as JSON,
# and shared.wizard.validate_step is the single source of truth for its shape.


class LoanDecision(BaseModel):
    # manual_review is an *interim* outcome: the Workflow keeps running and waits
    # for an underwriter to resolve it (see ResolveReviewInput). The final,
    # terminal outcomes are approved, rejected, withdrawn, and abandoned.
    outcome: str  # approved, rejected, manual_review, withdrawn, or abandoned
    reason: str
    reference_id: str


# Step data is stored as plain dicts in WizardState, because it crosses to a
# JavaScript interface as JSON for the front-end. validate_step() is the source of truth
class WizardData(BaseModel):
    applicant: dict | None = None
    loan_details: dict | None = None
    employment: dict | None = None
    review: dict | None = None


class WizardState(BaseModel):
    application_id: str
    status: str
    current_step: LoanStep
    completed_steps: list[LoanStep]
    data: WizardData
    decision: LoanDecision | None = None
    updated_at: float


# Message inputs
class SubmitStepInput(BaseModel):
    step: LoanStep
    payload: dict


class SaveDraftInput(BaseModel):
    step: LoanStep
    partial: dict


class ResolveReviewInput(BaseModel):
    # An underwriter's resolution of a manual_review application. `outcome` must
    # be "approved" or "rejected"; `note` records the underwriter's reasoning and
    # becomes the final decision's reason.
    outcome: str
    note: str
