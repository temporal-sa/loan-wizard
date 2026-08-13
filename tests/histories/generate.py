"""Regenerate the recorded replay fixture used by ``tests/test_replay.py``.

Run from anywhere:  uv run python tests/histories/generate.py

Drives a completed happy-path application against a local dev server with the
real (mocked) Activities and the Pydantic data converter, then writes the run's
Event History to ``loan_application_completed.json`` beside this file.

Re-run this only when the Workflow's command sequence changes on purpose — the
committed history is a fixed determinism baseline, so regenerating it after an
accidental change would mask exactly the regression the replay test guards.
"""
import asyncio
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Make the source root (the repo root) importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from activities import loan_activities
from shared.models import LoanStep, ResolveReviewInput, SubmitStepInput
from workflows.loan_application import LoanApplicationWorkflow

TASK_QUEUE = "gen-history"
OUT = Path(__file__).resolve().parent / "loan_application_completed.json"

ALL_ACTIVITIES = loan_activities.ALL

VALID_STEPS = [
    (LoanStep.applicant, {"full_name": "Ada Lovelace", "email": "ada@example.com", "date_of_birth": "1990-01-01"}),
    (LoanStep.loan_details, {"amount": 10000, "term_months": 24, "purpose": "home"}),
    (LoanStep.employment, {"employer_name": "Analytical Engines", "annual_income": 90000, "status": "employed"}),
    (LoanStep.review, {"consent_given": True}),
]


async def main() -> None:
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
                handle = await env.client.start_workflow(
                    LoanApplicationWorkflow.run,
                    args=["recorded-history-app"],
                    id=f"loan-application-recorded-{uuid.uuid4()}",
                    task_queue=TASK_QUEUE,
                )
                for step, payload in VALID_STEPS:
                    await handle.execute_update(
                        LoanApplicationWorkflow.submit_step,
                        SubmitStepInput(step=step, payload=payload),
                    )
                await handle.execute_update(LoanApplicationWorkflow.submit_application)

                # `recorded-history-app` scores into the manual-review band, which
                # now parks the Workflow instead of ending it. Resolve it as an
                # underwriter would so the recorded history is a *completed* run
                # that exercises the manual_review → resolve_review → finish path.
                for _ in range(50):
                    state = await handle.query(LoanApplicationWorkflow.get_state)
                    if state.status == "manual_review":
                        await handle.execute_update(
                            LoanApplicationWorkflow.resolve_review,
                            ResolveReviewInput(outcome="approved", note="Docs verified"),
                        )
                        break
                    if state.status in ("approved", "rejected"):
                        break
                    await asyncio.sleep(0.1)

                decision = await handle.result()

                history = await handle.fetch_history()
                OUT.write_text(history.to_json())
                print(f"decision={decision.outcome}; wrote {len(history.events)} events to {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
