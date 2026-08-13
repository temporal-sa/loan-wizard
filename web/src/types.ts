// Mirrors shared/models.py (WizardState) as it crosses the FastAPI boundary as
// JSON. The interface holds no durable state of its own — this is only the
// shape of what the Query returns.

export type LoanStep = "applicant" | "loan_details" | "employment" | "review";

export interface LoanDecision {
  outcome: string; // approved | rejected | manual_review | withdrawn | abandoned
  reason: string;
  reference_id: string;
}

export interface WizardData {
  applicant?: Record<string, unknown> | null;
  loan_details?: Record<string, unknown> | null;
  employment?: Record<string, unknown> | null;
  review?: Record<string, unknown> | null;
}

export interface WizardState {
  application_id: string;
  status: string; // in_progress | submitted | processing | <terminal outcome>
  current_step: LoanStep;
  completed_steps: LoanStep[];
  data: WizardData;
  decision: LoanDecision | null;
  updated_at: number;
}

export const STEP_ORDER: LoanStep[] = [
  "applicant",
  "loan_details",
  "employment",
  "review",
];

export const STEP_LABELS: Record<LoanStep, string> = {
  applicant: "Applicant",
  loan_details: "Loan details",
  employment: "Employment",
  review: "Review",
};

// A workflow whose status is one of these has reached a terminal decision and
// will not change again, so polling can stop. `manual_review` is deliberately
// NOT here: the Workflow stays running, parked, until an underwriter resolves
// it, so the interface keeps polling for the final outcome.
export const TERMINAL_STATUSES = new Set([
  "approved",
  "rejected",
  "withdrawn",
  "abandoned",
]);
