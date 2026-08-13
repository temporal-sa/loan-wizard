import { useCallback, useEffect, useRef, useState } from "react";
import {
  getState,
  resolveReview,
  saveDraft,
  submitApplication,
  submitStep,
} from "./api";
import {
  STEP_LABELS,
  STEP_ORDER,
  TERMINAL_STATUSES,
  type LoanStep,
  type WizardState,
} from "./types";

// --- Field metadata -------------------------------------------------------
// Drives generic rendering and payload coercion. `validate_step` in
// shared/wizard.py remains the single source of truth for validation; these
// fields only shape the inputs.

type FieldType = "text" | "email" | "date" | "tel" | "number" | "select";

interface Field {
  name: string;
  label: string;
  type: FieldType;
  options?: string[];
  optional?: boolean;
}

const STEP_FIELDS: Record<Exclude<LoanStep, "review">, Field[]> = {
  applicant: [
    { name: "full_name", label: "Full name", type: "text" },
    { name: "email", label: "Email", type: "email" },
    { name: "date_of_birth", label: "Date of birth", type: "date" },
    { name: "phone", label: "Phone", type: "tel", optional: true },
  ],
  loan_details: [
    { name: "amount", label: "Loan amount ($)", type: "number" },
    { name: "term_months", label: "Term (months)", type: "number" },
    {
      name: "purpose",
      label: "Purpose",
      type: "select",
      options: ["home", "auto", "personal", "education"],
    },
  ],
  employment: [
    { name: "employer_name", label: "Employer name", type: "text" },
    { name: "annual_income", label: "Annual income ($)", type: "number" },
    {
      name: "status",
      label: "Employment status",
      type: "select",
      options: ["employed", "self_employed", "unemployed", "retired"],
    },
  ],
};

const NUMBER_FIELDS = new Set(["amount", "term_months", "annual_income"]);

type FormValues = Record<string, string | boolean>;
type Form = Record<LoanStep, FormValues>;

// Seed editable form values from the Query's data, so a resume restores what
// was previously entered (committed steps and best-effort drafts alike).
function formFromState(state: WizardState): Form {
  const seed = (step: LoanStep): FormValues => {
    const data = (state.data[step] ?? {}) as Record<string, unknown>;
    const out: FormValues = {};
    for (const [k, v] of Object.entries(data)) {
      out[k] = typeof v === "boolean" ? v : v == null ? "" : String(v);
    }
    return out;
  };
  return {
    applicant: seed("applicant"),
    loan_details: seed("loan_details"),
    employment: seed("employment"),
    review: seed("review"),
  };
}

// Coerce a step's form values into the payload the Workflow expects.
function payloadFor(values: FormValues): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(values)) {
    if (NUMBER_FIELDS.has(k)) {
      out[k] = v === "" || v == null ? null : Number(v);
    } else {
      out[k] = v;
    }
  }
  return out;
}

const AUTOSAVE_DELAY_MS = 800;
const POLL_INTERVAL_MS = 1500;
// Consecutive poll failures tolerated before giving up and surfacing the error.
const MAX_POLL_FAILURES = 5;

export function Wizard({ applicationId }: { applicationId: string }) {
  const [state, setState] = useState<WizardState | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [viewStep, setViewStep] = useState<LoanStep | null>(null);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const formRef = useRef<Form | null>(null);
  formRef.current = form;
  const autosaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Restore state on mount (and whenever the application id changes).
  useEffect(() => {
    let cancelled = false;
    setState(null);
    setForm(null);
    setViewStep(null);
    getState(applicationId)
      .then((s) => {
        if (cancelled) return;
        setState(s);
        setForm(formFromState(s));
        setViewStep(s.current_step);
      })
      .catch((e) => !cancelled && setLoadError(e.message));
    return () => {
      cancelled = true;
    };
  }, [applicationId]);

  // While the decision is being computed — or while it sits in manual_review
  // waiting for an underwriter — poll the Query until it settles on a terminal
  // outcome. manual_review keeps polling so the underwriter's resolution shows up.
  const polling =
    state != null &&
    !TERMINAL_STATUSES.has(state.status) &&
    (state.status === "submitted" ||
      state.status === "processing" ||
      state.status === "manual_review");

  useEffect(() => {
    if (!polling) return;
    let cancelled = false;
    let failures = 0;
    const timer = setInterval(async () => {
      try {
        const s = await getState(applicationId);
        if (cancelled) return;
        failures = 0; // recovered
        setState(s);
      } catch (err) {
        if (cancelled) return;
        failures += 1;
        // A blip while the decision computes (API restart, dropped connection,
        // a transient 5xx) is expected — keep polling. But don't spin forever:
        // after repeated failures, surface the error and stop.
        console.warn(
          `Polling ${applicationId} failed (${failures}/${MAX_POLL_FAILURES})`,
          err,
        );
        if (failures >= MAX_POLL_FAILURES) {
          clearInterval(timer);
          setLoadError(err instanceof Error ? err.message : String(err));
        }
      }
    }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [polling, applicationId]);

  // Best-effort autosave via the draft Signal — one Signal per pause in typing,
  // never one per keystroke, to keep Signal volume low.
  const scheduleAutosave = useCallback(
    (step: LoanStep) => {
      if (autosaveTimer.current) clearTimeout(autosaveTimer.current);
      autosaveTimer.current = setTimeout(() => {
        const values = formRef.current?.[step];
        if (values) void saveDraft(applicationId, step, values as Record<string, unknown>);
      }, AUTOSAVE_DELAY_MS);
    },
    [applicationId],
  );

  useEffect(
    () => () => {
      if (autosaveTimer.current) clearTimeout(autosaveTimer.current);
    },
    [],
  );

  const onField = (step: LoanStep, name: string, value: string | boolean) => {
    setForm((prev) =>
      prev ? { ...prev, [step]: { ...prev[step], [name]: value } } : prev,
    );
    scheduleAutosave(step);
  };

  const commitStep = async (step: LoanStep) => {
    if (!form) return;
    setBusy(true);
    setErrors([]);
    try {
      const result = await submitStep(applicationId, step, payloadFor(form[step]));
      if (!result.ok) {
        setErrors(result.errors ?? ["Validation failed"]);
        return;
      }
      if (result.state) {
        setState(result.state);
        setViewStep(result.state.current_step);
      }
    } catch (e) {
      setErrors([e instanceof Error ? e.message : String(e)]);
    } finally {
      setBusy(false);
    }
  };

  const finalSubmit = async () => {
    if (!form) return;
    setBusy(true);
    setErrors([]);
    try {
      // Commit the review step (records consent) before the final submission.
      const committed = await submitStep(
        applicationId,
        "review",
        payloadFor(form.review),
      );
      if (!committed.ok) {
        setErrors(committed.errors ?? ["Consent is required"]);
        return;
      }
      const submitted = await submitApplication(applicationId);
      if (!submitted.ok) {
        setErrors(submitted.errors ?? ["Submission failed"]);
        return;
      }
      if (submitted.state) setState(submitted.state);
    } catch (e) {
      setErrors([e instanceof Error ? e.message : String(e)]);
    } finally {
      setBusy(false);
    }
  };

  // --- Render ---------------------------------------------------------------

  if (loadError) {
    return <div className="card error-text">Could not load application: {loadError}</div>;
  }
  if (!state || !form || !viewStep) {
    return <div className="card">Loading…</div>;
  }

  // Parked in manual review: the Workflow is still running. Show the interim
  // reason and an underwriter panel that resolves it via the resolve_review
  // Update — the running application is updated in place, not restarted.
  if (state.status === "manual_review") {
    return <UnderReview state={state} applicationId={applicationId} />;
  }

  if (TERMINAL_STATUSES.has(state.status) || state.decision) {
    return <Decision state={state} />;
  }

  if (polling) {
    return (
      <div className="card processing">
        <div className="spinner" aria-hidden />
        <p>Reviewing your application… this only takes a moment.</p>
      </div>
    );
  }

  return (
    <div className="wizard">
      <ProgressBar
        current={state.current_step}
        completed={state.completed_steps}
        viewing={viewStep}
        onSelect={setViewStep}
      />
      <div className="card">
        <StepForm
          step={viewStep}
          form={form}
          errors={errors}
          busy={busy}
          isCurrent={viewStep === state.current_step}
          onField={onField}
          onCommit={commitStep}
          onFinalSubmit={finalSubmit}
          state={state}
        />
      </div>
      <p className="app-id">Application {state.application_id}</p>
    </div>
  );
}

// --- Sub-components --------------------------------------------------------

function ProgressBar({
  current,
  completed,
  viewing,
  onSelect,
}: {
  current: LoanStep;
  completed: LoanStep[];
  viewing: LoanStep;
  onSelect: (s: LoanStep) => void;
}) {
  const done = new Set(completed);
  return (
    <ol className="progress">
      {STEP_ORDER.map((step, i) => {
        const isDone = done.has(step);
        const isCurrent = step === current;
        const selectable = isDone || isCurrent;
        const classes = [
          "progress-step",
          isDone ? "done" : "",
          isCurrent ? "current" : "",
          step === viewing ? "viewing" : "",
        ]
          .filter(Boolean)
          .join(" ");
        return (
          <li key={step} className={classes}>
            <button
              className="progress-dot"
              disabled={!selectable}
              onClick={() => selectable && onSelect(step)}
              title={STEP_LABELS[step]}
            >
              {isDone ? "✓" : i + 1}
            </button>
            <span className="progress-label">{STEP_LABELS[step]}</span>
          </li>
        );
      })}
    </ol>
  );
}

function StepForm({
  step,
  form,
  errors,
  busy,
  isCurrent,
  onField,
  onCommit,
  onFinalSubmit,
  state,
}: {
  step: LoanStep;
  form: Form;
  errors: string[];
  busy: boolean;
  isCurrent: boolean;
  onField: (step: LoanStep, name: string, value: string | boolean) => void;
  onCommit: (step: LoanStep) => void;
  onFinalSubmit: () => void;
  state: WizardState;
}) {
  if (step === "review") {
    return (
      <ReviewStep
        form={form}
        errors={errors}
        busy={busy}
        onField={onField}
        onFinalSubmit={onFinalSubmit}
        state={state}
      />
    );
  }

  const fields = STEP_FIELDS[step];
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onCommit(step);
      }}
    >
      <h2>{STEP_LABELS[step]}</h2>
      {fields.map((f) => (
        <label key={f.name} className="field">
          <span className="field-label">
            {f.label}
            {f.optional && <em> (optional)</em>}
          </span>
          {f.type === "select" ? (
            <select
              value={(form[step][f.name] as string) ?? ""}
              onChange={(e) => onField(step, f.name, e.target.value)}
            >
              <option value="" disabled>
                Select…
              </option>
              {f.options!.map((o) => (
                <option key={o} value={o}>
                  {o.replace("_", " ")}
                </option>
              ))}
            </select>
          ) : (
            <input
              type={f.type}
              value={(form[step][f.name] as string) ?? ""}
              onChange={(e) => onField(step, f.name, e.target.value)}
            />
          )}
        </label>
      ))}
      <FieldErrors errors={errors} />
      <div className="actions">
        <button type="submit" className="primary" disabled={busy}>
          {busy ? "Saving…" : isCurrent ? "Continue" : "Save changes"}
        </button>
      </div>
    </form>
  );
}

function ReviewStep({
  form,
  errors,
  busy,
  onField,
  onFinalSubmit,
  state,
}: {
  form: Form;
  errors: string[];
  busy: boolean;
  onField: (step: LoanStep, name: string, value: string | boolean) => void;
  onFinalSubmit: () => void;
  state: WizardState;
}) {
  const consent = form.review.consent_given === true;
  const rows: [string, unknown][] = [
    ["Name", state.data.applicant?.["full_name"]],
    ["Email", state.data.applicant?.["email"]],
    ["Amount", state.data.loan_details?.["amount"]],
    ["Term (months)", state.data.loan_details?.["term_months"]],
    ["Purpose", state.data.loan_details?.["purpose"]],
    ["Employer", state.data.employment?.["employer_name"]],
    ["Annual income", state.data.employment?.["annual_income"]],
  ];
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onFinalSubmit();
      }}
    >
      <h2>Review &amp; submit</h2>
      <dl className="summary">
        {rows.map(([label, value]) => (
          <div key={label} className="summary-row">
            <dt>{label}</dt>
            <dd>{value == null || value === "" ? "—" : String(value)}</dd>
          </div>
        ))}
      </dl>
      <label className="field checkbox">
        <input
          type="checkbox"
          checked={consent}
          onChange={(e) => onField("review", "consent_given", e.target.checked)}
        />
        <span>I confirm the information above is accurate and consent to a credit check.</span>
      </label>
      <FieldErrors errors={errors} />
      <div className="actions">
        <button type="submit" className="primary" disabled={busy || !consent}>
          {busy ? "Submitting…" : "Submit application"}
        </button>
      </div>
    </form>
  );
}

function FieldErrors({ errors }: { errors: string[] }) {
  if (errors.length === 0) return null;
  return (
    <ul className="errors">
      {errors.map((e, i) => (
        <li key={i}>{e}</li>
      ))}
    </ul>
  );
}

// Interim state: the application is parked in manual review and the Workflow is
// still running. The applicant sees why it was referred; a staff underwriter can
// resolve it here, which sends the resolve_review Update to the live Workflow.
// The surrounding Wizard keeps polling, so the resolved outcome appears on its own.
function UnderReview({
  state,
  applicationId,
}: {
  state: WizardState;
  applicationId: string;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [submitted, setSubmitted] = useState(false);

  const resolve = async (outcome: "approved" | "rejected") => {
    setBusy(true);
    setErrors([]);
    try {
      const result = await resolveReview(applicationId, outcome, note.trim());
      if (!result.ok) {
        setErrors(result.errors ?? ["Resolution failed"]);
        return;
      }
      // The Workflow finalizes asynchronously; the Wizard's poll will pick up
      // the terminal outcome and replace this view.
      setSubmitted(true);
    } catch (e) {
      setErrors([e instanceof Error ? e.message : String(e)]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card decision decision-manual_review">
      <h2>Under manual review</h2>
      {state.decision?.reason && (
        <p className="decision-reason">{state.decision.reason}</p>
      )}
      <p>
        An underwriter is reviewing your application. This application stays open —
        no need to start over.
      </p>
      {state.decision?.reference_id && (
        <p className="app-id">Reference {state.decision.reference_id}</p>
      )}

      <div className="underwriter-panel">
        <h3>Underwriter review · staff</h3>
        {submitted ? (
          <p className="decision-reason">Resolution recorded — finalizing…</p>
        ) : (
          <>
            <label className="field">
              <span className="field-label">Note (reason for the decision)</span>
              <textarea
                value={note}
                onChange={(e) => setNote(e.target.value)}
                rows={3}
                placeholder="e.g. Verified income documents; approving."
              />
            </label>
            <FieldErrors errors={errors} />
            <div className="actions">
              <button
                type="button"
                className="primary"
                disabled={busy}
                onClick={() => resolve("approved")}
              >
                {busy ? "Working…" : "Approve"}
              </button>
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => resolve("rejected")}
              >
                {busy ? "Working…" : "Reject"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function Decision({ state }: { state: WizardState }) {
  const outcome = state.decision?.outcome ?? state.status;
  const reason = state.decision?.reason;
  const ref = state.decision?.reference_id;
  const label: Record<string, string> = {
    approved: "Approved 🎉",
    rejected: "Not approved",
    manual_review: "Under manual review",
    withdrawn: "Withdrawn",
    abandoned: "Abandoned",
  };
  return (
    <div className={`card decision decision-${outcome}`}>
      <h2>{label[outcome] ?? outcome}</h2>
      {reason && <p className="decision-reason">{reason}</p>}
      {ref && <p className="app-id">Reference {ref}</p>}
    </div>
  );
}
