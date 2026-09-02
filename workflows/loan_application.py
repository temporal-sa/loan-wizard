# workflows/loan_application.py
import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from activities.loan_activities import (
        reserve_underwriting_slot, release_underwriting_slot, pull_credit_report,
        run_decision_engine, notify_applicant, send_reminder,
    )
    from shared.models import (
        WizardState, WizardData, LoanDecision, LoanStep,
        SubmitStepInput, SaveDraftInput, ResolveReviewInput,
    )
    from shared.wizard import STEP_ORDER, validate_step, next_step, coerce_step
    from shared.search_attributes import LOAN_STATUS, LOAN_USER_ID

ABANDON_AFTER = timedelta(days=7)
MAX_REMINDERS = 2
ACT_TIMEOUT = timedelta(seconds=30)


@workflow.defn
class LoanApplicationWorkflow:
    def __init__(self) -> None:
        # Initialize state here, not in run(): a validator or Signal handler can
        # run before run()'s body, so the state it reads must already exist.
        self._state = WizardState(
            application_id="",
            status="in_progress",
            current_step=LoanStep.applicant,
            completed_steps=[],
            data=WizardData(),
            updated_at=0.0,
        )
        self._submitted = False
        self._withdrawn = False
        self._withdraw_reason: str | None = None
        # An underwriter's resolution of a manual_review application. None until
        # a resolve_review Update lands; while None (and status is manual_review)
        # the Workflow stays alive, parked, waiting to be resolved.
        self._resolution: LoanDecision | None = None
        # The user id (applicant email) last mirrored to the LoanUserId Search
        # Attribute, so we only upsert when it first appears or changes.
        self._indexed_user_id: str | None = None

    def _apply_status(self, status: str) -> None:
        """Set the lifecycle status AND mirror it to the LoanStatus Search
        Attribute so reviewers can filter applications by status in the Temporal
        UI/CLI. The single writer of `self._state.status`, so the queryable state
        and the indexed attribute can never drift apart."""
        self._state.status = status
        workflow.upsert_search_attributes([LOAN_STATUS.value_set(status)])

    def _index_user_from_email(self) -> None:
        """Mirror the applicant's email to the LoanUserId Search Attribute once
        it is known, so a reviewer can find every application belonging to a
        person. Idempotent: only upserts when the email first appears or changes,
        and does nothing until the applicant step carries an email."""
        applicant = self._state.data.applicant or {}
        email = applicant.get("email")
        if email and email != self._indexed_user_id:
            self._indexed_user_id = email
            workflow.upsert_search_attributes([LOAN_USER_ID.value_set(email)])

    @workflow.run
    async def run(
        self,
        application_id: str,
        carried: WizardData | None = None,
        completed: list[LoanStep] | None = None,
        reminders: int = 0,
    ) -> LoanDecision:
        # Fill in identity and resumed state in place, not by replacing it: a
        # handler (signal-with-start, or a save_draft redelivered across
        # continue-as-new) can run before this body, so its newer write wins.
        self._state.application_id = application_id
        self._state.updated_at = workflow.now().timestamp()
        # Publish the current status as a Search Attribute from the first Task, so
        # a fresh application is filterable as in_progress immediately (and a run
        # resumed via continue-as-new re-affirms it). Search Attributes carry
        # across continue-as-new, so this is a cheap re-write, not a fix-up.
        self._apply_status(self._state.status)
        if completed:
            # Union with whatever a handler already recorded, in canonical order.
            resumed = set(self._state.completed_steps) | set(completed)
            self._state.completed_steps = [s for s in STEP_ORDER if s in resumed]
            self._state.current_step = next_step(self._state.completed_steps[-1]) or LoanStep.review
        if carried is not None:
            # Merge per field: keep a field a handler already set, fill in the
            # rest from carried, and on a shared field merge keys (handler wins).
            for field in WizardData.model_fields:
                incoming = getattr(carried, field)
                if incoming is None:
                    continue
                existing = getattr(self._state.data, field)
                setattr(
                    self._state.data, field,
                    incoming if existing is None else {**incoming, **existing},
                )
        # A resumed run may carry the applicant email — index it up front.
        self._index_user_from_email()

        # Resumable wait loop with inactivity reminders. The reminder count is
        # carried across continue-as-new (see below), so the abandonment budget
        # is spent over the application's whole lifetime rather than being reset
        # to zero on every history-driven continue-as-new.
        while not self._submitted and not self._withdrawn:
            try:
                # Wake on applicant action OR when history has grown large enough
                # that the SDK suggests continuing-as-new. Waking on the latter
                # means the history-size guard is evaluated on every autosave, not
                # only when the seven-day reminder timer fires — otherwise a burst
                # of best-effort save_draft signals could bloat history for days
                # before the next timeout got a chance to check.
                await workflow.wait_condition(
                    lambda: self._submitted
                    or self._withdrawn
                    or workflow.info().is_continue_as_new_suggested(),
                    timeout=ABANDON_AFTER,
                )
            except asyncio.TimeoutError:
                if reminders >= MAX_REMINDERS:
                    await workflow.wait_condition(workflow.all_handlers_finished)
                    decision = LoanDecision(
                        outcome="abandoned", reason="Inactive", reference_id=application_id,
                    )
                    self._apply_status("abandoned")
                    self._state.decision = decision
                    self._state.updated_at = workflow.now().timestamp()
                    return decision
                reminders += 1
                email = self._state.data.applicant.get("email") if self._state.data.applicant else None
                await workflow.execute_activity(
                    send_reminder, args=[application_id, email],
                    start_to_close_timeout=ACT_TIMEOUT,
                )
                continue

            # Woke without timing out: the applicant acted, or history is large.
            # If it was only the latter, continue-as-new to reset history and
            # carry the in-progress state forward. Drain handlers first so no
            # in-flight autosave is lost across the boundary.
            if (
                not self._submitted
                and not self._withdrawn
                and workflow.info().is_continue_as_new_suggested()
            ):
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(
                    args=[
                        application_id,
                        self._state.data,
                        self._state.completed_steps,
                        reminders,
                    ],
                )

        if self._withdrawn:
            await workflow.wait_condition(workflow.all_handlers_finished)
            decision = LoanDecision(
                outcome="withdrawn",
                reason=self._withdraw_reason or "Withdrawn by applicant",
                reference_id=application_id,
            )
            self._apply_status("withdrawn")
            self._state.decision = decision
            self._state.updated_at = workflow.now().timestamp()
            return decision

        # Submitted, so run the decisioning Saga.
        self._apply_status("processing")
        self._state.updated_at = workflow.now().timestamp()
        decision = await self._run_decisioning(application_id)

        # Notify the applicant of the decision the engine produced (approved,
        # rejected, or "under manual review"). This is a post-decision side
        # effect, kept OUTSIDE the Saga so a notification failure can never roll
        # back a decision that was already made.
        await workflow.execute_activity(
            notify_applicant, args=[application_id, decision],
            start_to_close_timeout=ACT_TIMEOUT,
        )

        # manual_review is not the end of the road: park the application in an
        # interim state and keep the Workflow running so it can still be
        # updated — an underwriter resolves it with a resolve_review Update (or
        # the applicant withdraws). This is the whole point of the pattern: a
        # long-lived Workflow that is continued and updated while it runs.
        if decision.outcome == "manual_review":
            self._apply_status("manual_review")
            self._state.decision = decision
            self._state.updated_at = workflow.now().timestamp()
            await workflow.wait_condition(
                lambda: self._resolution is not None or self._withdrawn
            )
            if self._withdrawn:
                decision = LoanDecision(
                    outcome="withdrawn",
                    reason=self._withdraw_reason or "Withdrawn by applicant",
                    reference_id=application_id,
                )
            else:
                decision = self._resolution
                # Notify the applicant of the underwriter's final decision.
                await workflow.execute_activity(
                    notify_applicant, args=[application_id, decision],
                    start_to_close_timeout=ACT_TIMEOUT,
                )
            # The review is over either way — release the underwriting slot.
            await workflow.execute_activity(
                release_underwriting_slot, application_id,
                start_to_close_timeout=ACT_TIMEOUT,
            )

        self._apply_status(decision.outcome)
        self._state.decision = decision
        self._state.updated_at = workflow.now().timestamp()

        await workflow.wait_condition(workflow.all_handlers_finished)
        return decision

    # Query: read the state on resume, and poll for the decision.
    @workflow.query
    def get_state(self) -> WizardState:
        return self._state

    # Signal: best-effort autosave, with no validation and no acknowledgment.
    @workflow.signal
    def save_draft(self, draft: SaveDraftInput) -> None:
        current = getattr(self._state.data, draft.step.value) or {}
        partial = coerce_step(draft.step, draft.partial)
        setattr(self._state.data, draft.step.value, {**current, **partial})
        self._index_user_from_email()
        self._state.updated_at = workflow.now().timestamp()

    # Signal: the applicant withdraws.
    @workflow.signal
    def withdraw(self, reason: str | None = None) -> None:
        self._withdrawn = True
        self._withdraw_reason = reason
        self._apply_status("withdrawn")
        self._state.updated_at = workflow.now().timestamp()

    # Update: submit a step, validated and synchronous.
    @workflow.update
    async def submit_step(self, inp: SubmitStepInput) -> WizardState:
        setattr(self._state.data, inp.step.value, coerce_step(inp.step, inp.payload))
        if inp.step not in self._state.completed_steps:
            self._state.completed_steps.append(inp.step)
        self._state.current_step = next_step(inp.step) or inp.step
        self._index_user_from_email()
        self._state.updated_at = workflow.now().timestamp()
        return self._state

    @submit_step.validator
    def _validate_submit_step(self, inp: SubmitStepInput) -> None:
        # Validators must be read-only: no Activities, no sleeps, no changes. Raise to reject.
        errors = validate_step(inp.step, inp.payload)
        if errors:
            raise ApplicationError(
                "Step validation failed", {"step": inp.step.value, "errors": errors},
                type="ValidationError",
            )

    # Update: final submission, validated and synchronous.
    @workflow.update
    async def submit_application(self) -> WizardState:
        self._submitted = True
        self._apply_status("submitted")
        self._state.updated_at = workflow.now().timestamp()
        return self._state

    @submit_application.validator
    def _validate_submit_application(self) -> None:
        missing = [s.value for s in STEP_ORDER if s not in self._state.completed_steps]
        if missing:
            raise ApplicationError(
                "Application incomplete", {"missing": missing},
                type="IncompleteApplication",
            )
        review = self._state.data.review or {}
        if review.get("consent_given") is not True:
            raise ApplicationError("Consent required", type="ConsentRequired")

    # Update: an underwriter resolves a manual_review application. Validated and
    # synchronous — only valid while the application is parked in manual_review.
    # The handler records the resolution; run()'s wait loop finalizes it (notify,
    # release the slot, complete), the same shape as submit_application → poll.
    @workflow.update
    async def resolve_review(self, inp: ResolveReviewInput) -> WizardState:
        self._resolution = LoanDecision(
            outcome=inp.outcome,
            reason=inp.note or f"Underwriter {inp.outcome} after manual review",
            reference_id=self._state.application_id,
        )
        self._state.updated_at = workflow.now().timestamp()
        return self._state

    @resolve_review.validator
    def _validate_resolve_review(self, inp: ResolveReviewInput) -> None:
        # Validators must be read-only: no Activities, no sleeps, no changes. Raise to reject.
        if self._state.status != "manual_review" or self._resolution is not None:
            raise ApplicationError(
                "Application is not awaiting manual review",
                {"status": self._state.status},
                type="NotUnderReview",
            )
        if inp.outcome not in ("approved", "rejected"):
            raise ApplicationError(
                "Resolution outcome must be 'approved' or 'rejected'",
                {"outcome": inp.outcome},
                type="InvalidResolution",
            )

    # Saga: reserve the underwriting slot, pull credit, and produce a decision.
    # Each forward step records a compensation first; on failure, compensate in
    # reverse. Notification is deliberately NOT here — the caller notifies after
    # the Saga so a notification failure can't roll back a made decision.
    async def _run_decisioning(self, application_id: str) -> LoanDecision:
        compensations: list[Callable[[], Awaitable[None]]] = []
        try:
            compensations.append(
                lambda: workflow.execute_activity(
                    release_underwriting_slot, application_id,
                    start_to_close_timeout=ACT_TIMEOUT,
                )
            )
            await workflow.execute_activity(
                reserve_underwriting_slot, application_id,
                start_to_close_timeout=ACT_TIMEOUT,
            )

            score = await workflow.execute_activity(
                pull_credit_report, application_id,
                start_to_close_timeout=ACT_TIMEOUT,
            )
            return await workflow.execute_activity(
                run_decision_engine, args=[application_id, self._state.data, score],
                start_to_close_timeout=ACT_TIMEOUT,
            )
        except Exception as e:
            workflow.logger.error(f"decisioning failed: {e}; running compensations")

            async def _compensate():
                for c in reversed(compensations):
                    try:
                        await c()
                    except Exception as ce:
                        workflow.logger.error(f"compensation failed: {ce}")

            # asyncio.shield runs the compensations even if the Workflow is cancelled.
            await asyncio.shield(asyncio.ensure_future(_compensate()))
            raise
