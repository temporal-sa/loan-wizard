"""Workflow integration tests (tasks 3.5 and 3.6).

T1-T4 run against a local dev server (`start_local`), which supports Workflow
Update. T5 runs against the time-skipping test server so the seven-day
inactivity path fires without real waiting. Activities are mocked by passing
alternate functions to the Worker, and the Pydantic data converter is registered
on the test client.
"""
import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import WorkflowFailureError, WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from activities import loan_activities
from shared.models import (
    LoanStep,
    ResolveReviewInput,
    SaveDraftInput,
    SubmitStepInput,
    WizardData,
)
from workflows.loan_application import LoanApplicationWorkflow, MAX_REMINDERS

TASK_QUEUE = "test-loan-applications"

ALL_ACTIVITIES = loan_activities.ALL

VALID_STEPS = [
    (LoanStep.applicant, {"full_name": "Ada Lovelace", "email": "ada@example.com", "date_of_birth": "1990-01-01"}),
    (LoanStep.loan_details, {"amount": 10000, "term_months": 24, "purpose": "home"}),
    (LoanStep.employment, {"employer_name": "Analytical Engines", "annual_income": 90000, "status": "employed"}),
    (LoanStep.review, {"consent_given": True}),
]


@pytest_asyncio.fixture
async def client():
    """A dev-server client with a running Worker for the real Activities."""
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        with ThreadPoolExecutor(max_workers=10) as executor:
            async with Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[LoanApplicationWorkflow],
                activities=ALL_ACTIVITIES,
                activity_executor=executor,
            ):
                yield env.client


async def _start(client, application_id):
    return await client.start_workflow(
        LoanApplicationWorkflow.run,
        args=[application_id],
        id=f"loan-application-{application_id}-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )


async def _wait_for_status(handle, statuses, *, tries=50):
    """Poll get_state until the Workflow reports one of `statuses`."""
    state = None
    for _ in range(tries):
        state = await handle.query(LoanApplicationWorkflow.get_state)
        if state.status in statuses:
            return state
        await asyncio.sleep(0.1)
    raise AssertionError(f"status never reached {statuses}; last was {state and state.status}")


async def _drive_to_decision(handle):
    """Submit every step, submit the application, and resolve manual_review.

    The Saga can park an application in manual_review — now a non-terminal state
    an underwriter finishes. Resolving it (approved) lets the Workflow complete,
    which the happy-path and replay tests depend on.
    """
    for step, payload in VALID_STEPS:
        await handle.execute_update(
            LoanApplicationWorkflow.submit_step,
            SubmitStepInput(step=step, payload=payload),
        )
    await handle.execute_update(LoanApplicationWorkflow.submit_application)
    state = await _wait_for_status(
        handle, {"approved", "rejected", "manual_review"}
    )
    if state.status == "manual_review":
        await handle.execute_update(
            LoanApplicationWorkflow.resolve_review,
            ResolveReviewInput(outcome="approved", note="Docs verified"),
        )


# --- T2: full flow reaches a decision --------------------------------------

async def test_full_flow_reaches_a_decision(client):
    handle = await _start(client, "app-t2")
    await _drive_to_decision(handle)

    decision = await handle.result()
    # manual_review is resolved inside _drive_to_decision, so the terminal
    # outcome is always approved or rejected.
    assert decision.outcome in {"approved", "rejected"}
    assert decision.reference_id == "app-t2"

    final = await handle.query(LoanApplicationWorkflow.get_state)
    assert final.status == decision.outcome
    assert set(final.completed_steps) == set(LoanStep)


# --- T1: invalid step rejected; state unchanged ----------------------------

async def test_invalid_step_is_rejected_and_state_unchanged(client):
    handle = await _start(client, "app-t1")
    with pytest.raises(WorkflowUpdateFailedError) as exc:
        await handle.execute_update(
            LoanApplicationWorkflow.submit_step,
            SubmitStepInput(
                step=LoanStep.applicant,
                payload={"full_name": "Ada", "email": "not-an-email", "date_of_birth": "1990-01-01"},
            ),
        )
    assert getattr(exc.value.cause, "type", None) == "ValidationError"

    state = await handle.query(LoanApplicationWorkflow.get_state)
    assert state.current_step == LoanStep.applicant
    assert state.completed_steps == []
    assert state.data.applicant is None


# --- T3: submit before complete is rejected --------------------------------

async def test_submit_application_before_complete_is_rejected(client):
    handle = await _start(client, "app-t3")
    with pytest.raises(WorkflowUpdateFailedError) as exc:
        await handle.execute_update(LoanApplicationWorkflow.submit_application)
    assert getattr(exc.value.cause, "type", None) == "IncompleteApplication"


# --- T4: withdraw mid-wizard ends as withdrawn -----------------------------

async def test_withdraw_ends_as_withdrawn(client):
    handle = await _start(client, "app-t4")
    step, payload = VALID_STEPS[0]
    await handle.execute_update(
        LoanApplicationWorkflow.submit_step,
        SubmitStepInput(step=step, payload=payload),
    )
    await handle.signal(LoanApplicationWorkflow.withdraw, "Found a better rate")

    decision = await handle.result()
    assert decision.outcome == "withdrawn"
    # The applicant's withdrawal reason is carried onto the decision.
    assert decision.reason == "Found a better rate"

    # The terminal decision is also visible through the Query, not just the
    # Workflow's return value, so a client polling get_state sees the reason.
    state = await handle.query(LoanApplicationWorkflow.get_state)
    assert state.status == "withdrawn"
    assert state.decision is not None and state.decision.outcome == "withdrawn"


# --- T5: inactivity leads to reminders then abandoned (time-skipping) -------

async def test_inactivity_sends_reminders_then_abandons():
    reminders: list[str] = []

    @activity.defn(name="send_reminder")
    def counting_send_reminder(application_id: str, email: str | None) -> None:
        reminders.append(application_id)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        with ThreadPoolExecutor(max_workers=4) as executor:
            async with Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[LoanApplicationWorkflow],
                activities=[counting_send_reminder],
                activity_executor=executor,
            ):
                handle = await env.client.start_workflow(
                    LoanApplicationWorkflow.run,
                    args=["app-t5"],
                    id=f"loan-application-app-t5-{uuid.uuid4()}",
                    task_queue=TASK_QUEUE,
                )
                decision = await handle.result()
                # The abandoned decision is also queryable, not just returned.
                state = await handle.query(LoanApplicationWorkflow.get_state)

    assert decision.outcome == "abandoned"
    assert len(reminders) == 2  # MAX_REMINDERS, then abandon on the next timeout
    assert state.status == "abandoned"
    assert state.decision is not None and state.decision.outcome == "abandoned"


# --- T6: decision engine failure triggers Saga compensation ----------------

async def test_decision_engine_failure_runs_compensation():
    released: list[str] = []

    @activity.defn(name="reserve_underwriting_slot")
    def reserve(application_id: str) -> None:
        ...  # forward step succeeds, so its compensation must run on failure

    @activity.defn(name="release_underwriting_slot")
    def release(application_id: str) -> None:
        released.append(application_id)

    @activity.defn(name="pull_credit_report")
    def pull(application_id: str) -> int:
        return 700

    @activity.defn(name="run_decision_engine")
    def failing_decision(application_id: str, data: WizardData, score: int) -> None:
        # non_retryable so the Workflow fails at once instead of retrying forever.
        raise ApplicationError(
            "decision engine unavailable", type="DecisionEngineDown", non_retryable=True,
        )

    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        with ThreadPoolExecutor(max_workers=10) as executor:
            async with Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[LoanApplicationWorkflow],
                activities=[reserve, release, pull, failing_decision],
                activity_executor=executor,
            ):
                handle = await env.client.start_workflow(
                    LoanApplicationWorkflow.run,
                    args=["app-t6"],
                    id=f"loan-application-app-t6-{uuid.uuid4()}",
                    task_queue=TASK_QUEUE,
                )
                for step, payload in VALID_STEPS:
                    await handle.execute_update(
                        LoanApplicationWorkflow.submit_step,
                        SubmitStepInput(step=step, payload=payload),
                    )
                await handle.execute_update(LoanApplicationWorkflow.submit_application)

                # The decisioning failure propagates out of the Workflow after the
                # compensation runs.
                with pytest.raises(WorkflowFailureError):
                    await handle.result()

    assert released == ["app-t6"]  # release_underwriting_slot compensated the reservation


# --- T5b: a resumed run honors its carried reminder budget ------------------

async def test_carried_reminder_count_is_honored_after_continue_as_new():
    """Regression guard for the reminder-budget-survives-CAN fix.

    A run resumed via continue-as-new is just `run()` invoked with a non-zero
    `reminders` argument. Starting with the budget already spent
    (`reminders == MAX_REMINDERS`) must abandon on the very next inactivity
    timeout without sending any further reminders — proving the budget is spent
    over the application's whole life, not reset to zero on each CAN. Before the
    fix, the carried count was dropped and the run would send MAX_REMINDERS fresh
    reminders all over again.
    """
    reminders_sent: list[str] = []

    @activity.defn(name="send_reminder")
    def counting_send_reminder(application_id: str, email: str | None) -> None:
        reminders_sent.append(application_id)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        with ThreadPoolExecutor(max_workers=4) as executor:
            async with Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[LoanApplicationWorkflow],
                activities=[counting_send_reminder],
                activity_executor=executor,
            ):
                handle = await env.client.start_workflow(
                    LoanApplicationWorkflow.run,
                    # application_id, carried, completed, reminders(=budget spent)
                    args=["app-t5b", None, None, MAX_REMINDERS],
                    id=f"loan-application-app-t5b-{uuid.uuid4()}",
                    task_queue=TASK_QUEUE,
                )
                decision = await handle.result()

    assert decision.outcome == "abandoned"
    assert reminders_sent == []  # budget already spent: abandons, no new reminders


# --- T7: busy autosave path continues-as-new before the reminder tick --------

def _continued_as_new(history) -> bool:
    return any(
        e.HasField("workflow_execution_continued_as_new_event_attributes")
        for e in history.events
    )


async def test_busy_autosave_continues_as_new_without_waiting_for_timeout():
    """Regression guard for the continue-as-new cadence fix.

    A burst of best-effort `save_draft` signals must trigger continue-as-new on
    its own, without the seven-day reminder timer firing. The default history
    threshold (~10k events) is lowered on the dev server so a modest number of
    autosaves crosses it. Before the fix, the history-size check only ran after a
    reminder timeout, so this burst would have grown history unbounded for days.
    """
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter,
        dev_server_extra_args=[
            "--dynamic-config-value",
            "limit.historyCount.suggestContinueAsNew=50",
        ],
    ) as env:
        with ThreadPoolExecutor(max_workers=4) as executor:
            async with Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[LoanApplicationWorkflow],
                activities=ALL_ACTIVITIES,
                activity_executor=executor,
            ):
                handle = await env.client.start_workflow(
                    LoanApplicationWorkflow.run,
                    args=["app-t7"],
                    id=f"loan-application-app-t7-{uuid.uuid4()}",
                    task_queue=TASK_QUEUE,
                )
                first_run_id = handle.first_execution_run_id
                first_run = env.client.get_workflow_handle(
                    handle.id, run_id=first_run_id
                )

                # Autosave repeatedly. No timer is skipped and none fires; the
                # only thing growing is history, which should trigger CAN.
                for i in range(60):
                    await handle.signal(
                        LoanApplicationWorkflow.save_draft,
                        SaveDraftInput(
                            step=LoanStep.applicant, partial={"full_name": f"Ada {i}"}
                        ),
                    )

                # The first run must end in a continue-as-new.
                history = None
                for _ in range(50):
                    history = await first_run.fetch_history()
                    if _continued_as_new(history):
                        break
                    await asyncio.sleep(0.1)
                assert history is not None and _continued_as_new(history), (
                    "expected the autosave burst to continue-as-new the first run"
                )

                # State carried across the boundary: the latest run still knows
                # the application and holds the drafted data.
                state = await handle.query(LoanApplicationWorkflow.get_state)
                assert state.application_id == "app-t7"
                assert state.data.applicant is not None
                assert state.data.applicant["full_name"].startswith("Ada ")

                # The first run's history — which exercises the continue-as-new
                # branch of the wait loop — must replay deterministically.
                await Replayer(
                    workflows=[LoanApplicationWorkflow],
                    data_converter=pydantic_data_converter,
                ).replay_workflow(history)


# --- T8: a completed application's history replays deterministically ---------

async def test_completed_workflow_history_replays(client):
    """Replay guard against hidden non-determinism on the happy path."""
    handle = await _start(client, "app-t8")
    await _drive_to_decision(handle)
    await handle.result()

    history = await handle.fetch_history()
    await Replayer(
        workflows=[LoanApplicationWorkflow],
        data_converter=pydantic_data_converter,
    ).replay_workflow(history)


# --- T9: resume merges carried state with an early handler's write -----------

async def test_resume_merges_without_clobbering_early_handler(client):
    """Regression guard for the merge-don't-clobber-on-resume fix.

    Signal-with-start delivers `save_draft` in the same first Workflow Task, so
    the handler runs before `run()`'s body (verified: an early write survives).
    The workflow is started as if resumed from continue-as-new — `carried` data
    plus a `completed` step — while the start signal writes to the same and to a
    different field. Every write must survive: the handler wins on shared keys,
    carried fills in the rest. Before the fix, `run()` replaced `data` wholesale
    and the handler's write was lost.
    """
    handle = await client.start_workflow(
        LoanApplicationWorkflow.run,
        args=[
            "app-t9",
            WizardData(
                applicant={"full_name": "Carried Name", "email": "carried@example.com"},
                employment={"employer_name": "Acme"},
            ),
            [LoanStep.applicant],
            0,
        ],
        id=f"loan-application-app-t9-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
        start_signal="save_draft",
        start_signal_args=[
            SaveDraftInput(
                step=LoanStep.applicant,
                partial={"full_name": "Fresh Name", "phone": "555"},
            )
        ],
    )
    state = await handle.query(LoanApplicationWorkflow.get_state)

    # Shared field: handler wins on the shared key, carried keeps its own, and
    # the handler's extra key survives.
    assert state.data.applicant == {
        "full_name": "Fresh Name",       # handler wins over carried
        "email": "carried@example.com",  # carried key preserved
        "phone": "555",                  # handler-only key preserved
    }
    # Carried-only field is untouched.
    assert state.data.employment == {"employer_name": "Acme"}
    # Completed step and derived current step come through the resume.
    assert state.completed_steps == [LoanStep.applicant]
    assert state.current_step == LoanStep.loan_details

    await handle.signal(LoanApplicationWorkflow.withdraw)
    assert (await handle.result()).outcome == "withdrawn"


# --- T10b: string numeric inputs are coerced and never crash decisioning -----

async def test_string_numeric_inputs_are_coerced_and_reach_a_decision(client):
    """Regression guard for the `'<=' not supported between int and str` crash.

    Numeric step fields submitted as strings (raw form inputs, CLI payloads)
    must be coerced when the step is stored, so the decision engine sees numbers,
    not strings, and never raises TypeError on `amount <= income`.
    """
    handle = await _start(client, "app-t11")
    string_steps = [
        (LoanStep.applicant, {"full_name": "Ada", "email": "ada@example.com", "date_of_birth": "1990-01-01"}),
        (LoanStep.loan_details, {"amount": "10000", "term_months": "24", "purpose": "home"}),
        (LoanStep.employment, {"employer_name": "Acme", "annual_income": "90000", "status": "employed"}),
        (LoanStep.review, {"consent_given": True}),
    ]
    for step, payload in string_steps:
        await handle.execute_update(
            LoanApplicationWorkflow.submit_step,
            SubmitStepInput(step=step, payload=payload),
        )

    # Stored state holds numbers, not the strings that were submitted.
    state = await handle.query(LoanApplicationWorkflow.get_state)
    assert state.data.loan_details["amount"] == 10000.0
    assert state.data.loan_details["term_months"] == 24
    assert state.data.employment["annual_income"] == 90000.0

    await handle.execute_update(LoanApplicationWorkflow.submit_application)
    state = await _wait_for_status(handle, {"approved", "rejected", "manual_review"})
    if state.status == "manual_review":
        await handle.execute_update(
            LoanApplicationWorkflow.resolve_review,
            ResolveReviewInput(outcome="approved", note="Docs verified"),
        )
    # The crux: decisioning ran to a real outcome instead of raising TypeError.
    assert (await handle.result()).outcome in {"approved", "rejected"}


# --- T10: manual_review stays open until an underwriter resolves it ----------

async def _submit_all(handle):
    for step, payload in VALID_STEPS:
        await handle.execute_update(
            LoanApplicationWorkflow.submit_step,
            SubmitStepInput(step=step, payload=payload),
        )
    await handle.execute_update(LoanApplicationWorkflow.submit_application)


async def test_manual_review_stays_open_then_resolves(client):
    """The application parks in manual_review — a non-terminal, resumable state —
    and only an underwriter's resolve_review Update completes the Workflow.

    `app-t10` scores into the manual-review band with the real decision engine,
    so no Activity mock is needed.
    """
    handle = await _start(client, "app-t10")
    await _submit_all(handle)

    # It parks in manual_review with an interim, queryable decision.
    state = await _wait_for_status(handle, {"manual_review"})
    assert state.decision is not None and state.decision.outcome == "manual_review"
    assert state.decision.reason  # a real reason, not empty

    # The Workflow is still running: no result yet.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(handle.result()), timeout=1.0)

    # The underwriter resolves it; the Workflow then completes with that outcome,
    # and the note becomes the final decision's reason.
    await handle.execute_update(
        LoanApplicationWorkflow.resolve_review,
        ResolveReviewInput(outcome="rejected", note="Insufficient collateral"),
    )
    decision = await handle.result()
    assert decision.outcome == "rejected"
    assert decision.reason == "Insufficient collateral"

    final = await handle.query(LoanApplicationWorkflow.get_state)
    assert final.status == "rejected"
    assert final.decision.reason == "Insufficient collateral"


async def test_resolve_review_rejected_before_manual_review(client):
    """resolve_review is only valid while parked in manual_review."""
    handle = await _start(client, "app-t10b")
    with pytest.raises(WorkflowUpdateFailedError) as exc:
        await handle.execute_update(
            LoanApplicationWorkflow.resolve_review,
            ResolveReviewInput(outcome="approved", note="too early"),
        )
    assert getattr(exc.value.cause, "type", None) == "NotUnderReview"

    # Clean up: withdraw so the Workflow completes instead of lingering.
    await handle.signal(LoanApplicationWorkflow.withdraw)
    await handle.result()


async def test_resolve_review_rejects_invalid_outcome(client):
    """An underwriter can only approve or reject, not send an arbitrary outcome."""
    handle = await _start(client, "app-t10-x")  # scores into the manual-review band
    await _submit_all(handle)
    await _wait_for_status(handle, {"manual_review"})

    with pytest.raises(WorkflowUpdateFailedError) as exc:
        await handle.execute_update(
            LoanApplicationWorkflow.resolve_review,
            ResolveReviewInput(outcome="maybe", note="unsure"),
        )
    assert getattr(exc.value.cause, "type", None) == "InvalidResolution"

    # The application is still resolvable after a rejected attempt.
    await handle.execute_update(
        LoanApplicationWorkflow.resolve_review,
        ResolveReviewInput(outcome="approved", note="ok"),
    )
    assert (await handle.result()).outcome == "approved"
