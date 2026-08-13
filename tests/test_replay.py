"""Replay test against a recorded history (task 7.1, T7).

Unlike the inline replay checks in ``test_loan_application.py`` — which record a
history live and immediately replay it against the *same* code — this test
replays a history committed under ``tests/histories/``. That recorded history is
a fixed baseline: replaying today's Workflow code against it catches any change
that would break determinism for applications already in flight (a reordered
Activity, an added timer, a removed branch). It needs no Cluster and no Worker.

Regenerate the fixture only when the Workflow's command sequence changes on
purpose, with ``tests/histories/generate.py``.
"""
import uuid
from pathlib import Path

from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

from workflows.loan_application import LoanApplicationWorkflow

HISTORY = Path(__file__).parent / "histories" / "loan_application_completed.json"


async def test_recorded_history_replays_without_nondeterminism():
    history_json = HISTORY.read_text()
    await Replayer(
        workflows=[LoanApplicationWorkflow],
        data_converter=pydantic_data_converter,
    ).replay_workflow(WorkflowHistory.from_json(str(uuid.uuid4()), history_json))
