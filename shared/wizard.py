# shared/wizard.py
from .models import LoanStep

STEP_ORDER = [LoanStep.applicant, LoanStep.loan_details, LoanStep.employment, LoanStep.review]


def next_step(step: LoanStep) -> LoanStep | None:
    i = STEP_ORDER.index(step)
    return STEP_ORDER[i + 1] if i + 1 < len(STEP_ORDER) else None


def _as_int(value) -> int:
    return int(float(value))  # via float so "24" and "24.0" both parse


# Numeric fields per step, with the caster to normalize them to. Step data
# crosses the boundary as plain dicts and can arrive as strings (raw form
# inputs, autosave drafts, CLI payloads). Coercing once, where the data is
# written, keeps stored state numeric so no downstream consumer has to re-parse.
NUMERIC_FIELDS: dict[LoanStep, dict[str, object]] = {
    LoanStep.loan_details: {"amount": float, "term_months": _as_int},
    LoanStep.employment: {"annual_income": float},
}


def coerce_step(step: LoanStep, payload: dict) -> dict:
    """Return a copy of `payload` with this step's numeric fields cast to numbers.

    Only keys actually present are touched, so partial payloads (autosave drafts)
    are handled. A blank or unparseable value is left as-is for `validate_step`
    to reject — this normalizes types, it does not validate.
    """
    casters = NUMERIC_FIELDS.get(step)
    if not casters:
        return payload
    out = dict(payload)
    for name, cast in casters.items():
        if name not in out:
            continue
        try:
            out[name] = cast(out[name])
        except (TypeError, ValueError):
            pass  # leave blank/unparseable values for validation to catch
    return out


def validate_step(step: LoanStep, payload: dict) -> list[str]:
    """Return a list of field errors. An empty list means the step is valid."""
    errors: list[str] = []
    if step is LoanStep.applicant:
        if not payload.get("full_name"):
            errors.append("full_name is required")
        if "@" not in str(payload.get("email", "")):
            errors.append("email is invalid")
        if not payload.get("date_of_birth"):
            errors.append("date_of_birth is required")
    elif step is LoanStep.loan_details:
        if not float(payload.get("amount") or 0) > 0:
            errors.append("amount must be positive")
        if not int(payload.get("term_months") or 0) >= 6:
            errors.append("term_months must be at least 6")
    elif step is LoanStep.employment:
        if not payload.get("employer_name"):
            errors.append("employer_name is required")
        income = payload.get("annual_income")
        if float(income if income is not None else -1) < 0:
            errors.append("annual_income must be zero or greater")
    elif step is LoanStep.review:
        if payload.get("consent_given") is not True:
            errors.append("consent is required")
    return errors
