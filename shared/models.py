# shared/models.py
from enum import Enum
from pydantic import BaseModel


class LoanStep(str, Enum):
    applicant = "applicant"
    loan_details = "loan_details"
    employment = "employment"
    review = "review"


class ApplicationStatus(str, Enum):
    """The application's lifecycle status, which is also a decision `outcome`.

    A `str` Enum so it serializes to a plain string across the Temporal and JSON
    boundaries (like `LoanStep`) and compares equal to its value, and so every
    existing string comparison keeps working. The first three members are
    in-flight-only statuses; the rest are decision outcomes — the terminal ones
    (approved, rejected, withdrawn, abandoned) plus the interim manual_review,
    during which the Workflow keeps running until an underwriter resolves it.
    """
    in_progress = "in_progress"
    submitted = "submitted"
    processing = "processing"
    manual_review = "manual_review"
    approved = "approved"
    rejected = "rejected"
    withdrawn = "withdrawn"
    abandoned = "abandoned"


# Per-step fields (full_name/email, amount/term_months/purpose, etc.) are not
# modeled as Pydantic types here on purpose: step data is carried as plain dicts
# in WizardData (see below) so it crosses to the JavaScript interface as JSON,
# and shared.wizard.validate_step is the single source of truth for its shape.


class LoanDecision(BaseModel):
    # manual_review is an *interim* outcome: the Workflow keeps running and waits
    # for an underwriter to resolve it (see ResolveReviewInput). The final,
    # terminal outcomes are approved, rejected, withdrawn, and abandoned. Typed as
    # ApplicationStatus so an unknown outcome is rejected at construction.
    outcome: ApplicationStatus
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
    status: ApplicationStatus
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
    #
    # Deliberately a plain str, not ApplicationStatus: the resolve_review
    # validator is the single gatekeeper for what an underwriter may send, so it
    # rejects a bad value with a typed ApplicationError (mapped to HTTP 422) at
    # the Update boundary. Typing this as an Enum would instead reject unknown
    # values as a Pydantic error earlier, before that domain-specific validator.
    outcome: str
    note: str
