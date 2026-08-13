// Thin typed wrapper over the five FastAPI routes. Each function maps one-to-one
// onto a Temporal primitive:
//
//   createApplication  -> start_workflow
//   getState           -> Query   (get_state)
//   saveDraft          -> Signal  (save_draft, fire-and-forget)
//   submitStep         -> Update  (submit_step, validated, synchronous)
//   submitApplication  -> Update  (submit_application)
//   resolveReview      -> Update  (resolve_review, underwriter finalizes)
//
// The interface never talks to Temporal directly; it only calls these REST
// endpoints through the `/api` proxy.

import type { WizardState } from "./types";

const JSON_HEADERS = { "Content-Type": "application/json" };

export async function createApplication(): Promise<string> {
  const res = await fetch("/api/applications", { method: "POST" });
  if (!res.ok) throw new Error(`Failed to start application (${res.status})`);
  const body = (await res.json()) as { application_id: string };
  return body.application_id;
}

export async function getState(id: string): Promise<WizardState> {
  const res = await fetch(`/api/applications/${id}`);
  if (!res.ok) throw new Error(`Failed to load application (${res.status})`);
  return (await res.json()) as WizardState;
}

// Best-effort autosave. No acknowledgment, no validation — deliberately not
// awaited by callers in a way that blocks typing.
export function saveDraft(
  id: string,
  step: string,
  partial: Record<string, unknown>,
): Promise<Response> {
  return fetch(`/api/applications/${id}/draft`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ step, partial }),
  });
}

export interface StepResult {
  ok: boolean;
  state?: WizardState;
  errors?: string[];
}

// Pull the validator's field errors out of the 422 body the API produced from
// the Update validator's ApplicationError.
async function readErrors(res: Response, fallback: string): Promise<string[]> {
  try {
    const body = await res.json();
    return body?.detail?.details?.[0]?.errors ?? [body?.detail?.error ?? fallback];
  } catch {
    return [fallback];
  }
}

export async function submitStep(
  id: string,
  step: string,
  payload: Record<string, unknown>,
): Promise<StepResult> {
  const res = await fetch(`/api/applications/${id}/steps`, {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify({ step, payload }),
  });
  if (res.status === 422) {
    return { ok: false, errors: await readErrors(res, "Validation failed") };
  }
  if (!res.ok) throw new Error(`Failed to submit step (${res.status})`);
  return { ok: true, state: (await res.json()) as WizardState };
}

export async function submitApplication(id: string): Promise<StepResult> {
  const res = await fetch(`/api/applications/${id}/submit`, { method: "POST" });
  if (res.status === 422) {
    return { ok: false, errors: await readErrors(res, "Submission failed") };
  }
  if (!res.ok) throw new Error(`Failed to submit application (${res.status})`);
  return { ok: true, state: (await res.json()) as WizardState };
}

// Underwriter action: resolve a manual_review application. `outcome` is
// "approved" or "rejected"; `note` becomes the final decision's reason.
export async function resolveReview(
  id: string,
  outcome: "approved" | "rejected",
  note: string,
): Promise<StepResult> {
  const res = await fetch(`/api/applications/${id}/review/resolve`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ outcome, note }),
  });
  if (res.status === 422) {
    return { ok: false, errors: await readErrors(res, "Resolution failed") };
  }
  if (!res.ok) throw new Error(`Failed to resolve review (${res.status})`);
  return { ok: true, state: (await res.json()) as WizardState };
}
