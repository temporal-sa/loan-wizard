"""End-to-end REST test (task 5.4).

Drives the full flow through the FastAPI routes against a real dev server and a
running Worker. The client singleton in `api.temporal_client` is pointed at the
test environment's client, and the app is exercised in-process over ASGI, so no
network port is opened.
"""
import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import httpx
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import api.temporal_client as temporal_client
from activities import loan_activities
from api.main import app
from shared.models import LoanStep
from shared.temporal import register_search_attributes
from workflows.loan_application import LoanApplicationWorkflow

ALL_ACTIVITIES = loan_activities.ALL

VALID_STEPS = [
    (LoanStep.applicant, {"full_name": "Ada Lovelace", "email": "ada@example.com", "date_of_birth": "1990-01-01"}),
    (LoanStep.loan_details, {"amount": 10000, "term_months": 24, "purpose": "home"}),
    (LoanStep.employment, {"employer_name": "Analytical Engines", "annual_income": 90000, "status": "employed"}),
    (LoanStep.review, {"consent_given": True}),
]

# Terminal REST outcomes. manual_review is not here: it parks the Workflow until
# an underwriter resolves it via POST /review/resolve.
TERMINAL_DECISIONS = {"approved", "rejected"}


async def test_full_rest_flow_and_invalid_step_returns_422():
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        # Point the API's client singleton at the test server.
        temporal_client._client = env.client
        await register_search_attributes(env.client)
        try:
            with ThreadPoolExecutor(max_workers=10) as executor:
                async with Worker(
                    env.client,
                    task_queue=temporal_client.TASK_QUEUE,
                    workflows=[LoanApplicationWorkflow],
                    activities=ALL_ACTIVITIES,
                    activity_executor=executor,
                ):
                    transport = httpx.ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport, base_url="http://testserver"
                    ) as http:
                        # Start an application.
                        r = await http.post("/applications")
                        assert r.status_code == 200
                        app_id = r.json()["application_id"]

                        # An invalid step returns 422 with the validator's field errors.
                        r = await http.put(
                            f"/applications/{app_id}/steps",
                            json={
                                "step": "applicant",
                                "payload": {"full_name": "", "email": "bad", "date_of_birth": ""},
                            },
                        )
                        assert r.status_code == 422
                        details = r.json()["detail"]["details"]
                        assert details and details[0]["errors"], "expected field errors in the 422 body"

                        # Submit every step successfully.
                        for step, payload in VALID_STEPS:
                            r = await http.put(
                                f"/applications/{app_id}/steps",
                                json={"step": step.value, "payload": payload},
                            )
                            assert r.status_code == 200, r.text

                        # Final submission is accepted.
                        r = await http.post(f"/applications/{app_id}/submit")
                        assert r.status_code == 200
                        assert r.json()["status"] == "submitted"

                        # Poll the Query until the Saga settles on an outcome or
                        # parks in manual_review.
                        state = None
                        for _ in range(50):
                            r = await http.get(f"/applications/{app_id}")
                            assert r.status_code == 200
                            state = r.json()
                            if state["status"] in TERMINAL_DECISIONS | {"manual_review"}:
                                break
                            await asyncio.sleep(0.1)
                        assert state is not None
                        assert state["decision"]["reference_id"] == app_id

                        # If it parked in manual review, an underwriter resolves it
                        # through the REST endpoint, and it then reaches a terminal
                        # outcome carrying the underwriter's note as the reason.
                        if state["status"] == "manual_review":
                            r = await http.post(
                                f"/applications/{app_id}/review/resolve",
                                json={"outcome": "approved", "note": "Verified by ops"},
                            )
                            assert r.status_code == 200, r.text
                            for _ in range(50):
                                state = (await http.get(f"/applications/{app_id}")).json()
                                if state["status"] in TERMINAL_DECISIONS:
                                    break
                                await asyncio.sleep(0.1)
                            assert state["status"] == "approved"
                            assert state["decision"]["reason"] == "Verified by ops"

                        assert state["status"] in TERMINAL_DECISIONS
        finally:
            temporal_client._client = None


async def test_save_draft_is_accepted_as_fire_and_forget():
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        temporal_client._client = env.client
        await register_search_attributes(env.client)
        try:
            with ThreadPoolExecutor(max_workers=10) as executor:
                async with Worker(
                    env.client,
                    task_queue=temporal_client.TASK_QUEUE,
                    workflows=[LoanApplicationWorkflow],
                    activities=ALL_ACTIVITIES,
                    activity_executor=executor,
                ):
                    transport = httpx.ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport, base_url="http://testserver"
                    ) as http:
                        app_id = (await http.post("/applications")).json()["application_id"]

                        # Autosave returns 202 and does not block on validation.
                        r = await http.post(
                            f"/applications/{app_id}/draft",
                            json={"step": "applicant", "partial": {"full_name": "Ada"}},
                        )
                        assert r.status_code == 202

                        # The draft is visible on the next Query.
                        state = None
                        for _ in range(50):
                            state = (await http.get(f"/applications/{app_id}")).json()
                            if state["data"]["applicant"]:
                                break
                            await asyncio.sleep(0.1)
                        assert state["data"]["applicant"] == {"full_name": "Ada"}
        finally:
            temporal_client._client = None


@asynccontextmanager
async def _rest_env():
    """Yield (client, http): a dev-server client and an httpx client wired to the
    FastAPI app in-process. Used by the tests below that need a known workflow id.
    """
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        temporal_client._client = env.client
        await register_search_attributes(env.client)
        try:
            with ThreadPoolExecutor(max_workers=10) as executor:
                async with Worker(
                    env.client,
                    task_queue=temporal_client.TASK_QUEUE,
                    workflows=[LoanApplicationWorkflow],
                    activities=ALL_ACTIVITIES,
                    activity_executor=executor,
                ):
                    transport = httpx.ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport, base_url="http://testserver"
                    ) as http:
                        yield env.client, http
        finally:
            temporal_client._client = None


async def test_unknown_application_returns_404():
    # An unknown application id maps to 404, not an opaque 500.
    async with _rest_env() as (_, http):
        r = await http.get(f"/applications/{uuid.uuid4()}")
        assert r.status_code == 404


async def test_manual_review_resolves_through_rest():
    # Deterministic coverage of POST /review/resolve: a known id that scores into
    # the manual-review band, driven to resolution through the REST endpoints.
    async with _rest_env() as (client, http):
        app_id = "app-t10-x"  # scores into the manual-review band
        await client.start_workflow(
            LoanApplicationWorkflow.run, args=[app_id],
            id=temporal_client.wf_id(app_id), task_queue=temporal_client.TASK_QUEUE,
        )
        for step, payload in VALID_STEPS:
            r = await http.put(
                f"/applications/{app_id}/steps",
                json={"step": step.value, "payload": payload},
            )
            assert r.status_code == 200, r.text
        assert (await http.post(f"/applications/{app_id}/submit")).status_code == 200

        # Poll until it parks in manual review.
        state = None
        for _ in range(50):
            state = (await http.get(f"/applications/{app_id}")).json()
            if state["status"] == "manual_review":
                break
            await asyncio.sleep(0.1)
        assert state is not None and state["status"] == "manual_review"

        # An underwriter resolves it through REST; it reaches a terminal outcome
        # carrying the underwriter's note as the reason.
        r = await http.post(
            f"/applications/{app_id}/review/resolve",
            json={"outcome": "rejected", "note": "Underwriter declined"},
        )
        assert r.status_code == 200, r.text
        for _ in range(50):
            state = (await http.get(f"/applications/{app_id}")).json()
            if state["status"] in TERMINAL_DECISIONS:
                break
            await asyncio.sleep(0.1)
        assert state["status"] == "rejected"
        assert state["decision"]["reason"] == "Underwriter declined"
