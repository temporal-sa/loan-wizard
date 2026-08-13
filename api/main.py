# api/main.py
import uuid
from typing import Any
from fastapi import FastAPI, HTTPException
from temporalio.client import WorkflowHandle, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from api.temporal_client import get_client, wf_id, TASK_QUEUE
from workflows.loan_application import LoanApplicationWorkflow
from shared.models import SubmitStepInput, SaveDraftInput, ResolveReviewInput

app = FastAPI()


async def _handle(application_id: str) -> WorkflowHandle:
    client = await get_client()
    return client.get_workflow_handle(wf_id(application_id))


async def _execute_update(handle: WorkflowHandle, update, *args) -> Any:
    """Run a validated Workflow Update, mapping a rejection to HTTP 422.

    A rejected validator raises WorkflowUpdateFailedError; its ApplicationError
    (type and field details) is carried on `e.cause`, which the interface reads
    to show inline errors. Shared by every Update endpoint so the mapping lives
    in one place.
    """
    try:
        return await handle.execute_update(update, *args)
    except WorkflowUpdateFailedError as e:
        cause = e.cause
        details = cause.details if isinstance(cause, ApplicationError) else []
        raise HTTPException(
            status_code=422, detail={"error": str(cause), "details": details}
        ) from e


@app.post("/applications")
async def create_application():
    application_id = str(uuid.uuid4())
    client = await get_client()
    await client.start_workflow(
        LoanApplicationWorkflow.run, args=[application_id],
        id=wf_id(application_id), task_queue=TASK_QUEUE,
    )
    return {"application_id": application_id}


@app.get("/applications/{application_id}")
async def get_application(application_id: str):
    handle = await _handle(application_id)
    try:
        return await handle.query(LoanApplicationWorkflow.get_state)
    except RPCError as e:
        # An unknown application id surfaces as a 404, not an opaque 500.
        if e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail="Application not found") from e
        raise


@app.put("/applications/{application_id}/steps")
async def submit_step(application_id: str, body: SubmitStepInput):
    handle = await _handle(application_id)
    return await _execute_update(handle, LoanApplicationWorkflow.submit_step, body)


@app.post("/applications/{application_id}/draft", status_code=202)
async def save_draft(application_id: str, body: SaveDraftInput):
    handle = await _handle(application_id)
    await handle.signal(LoanApplicationWorkflow.save_draft, body)  # fire-and-forget


@app.post("/applications/{application_id}/submit")
async def submit_application(application_id: str):
    # status becomes "submitted"; the interface then polls GET for the decision.
    handle = await _handle(application_id)
    return await _execute_update(handle, LoanApplicationWorkflow.submit_application)


@app.post("/applications/{application_id}/review/resolve")
async def resolve_review(application_id: str, body: ResolveReviewInput):
    # An underwriter finalizes a manual_review application; the interface then
    # polls GET for the resolved (approved/rejected) decision.
    handle = await _handle(application_id)
    return await _execute_update(handle, LoanApplicationWorkflow.resolve_review, body)
