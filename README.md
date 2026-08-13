# Resumable Loan Application Wizard — Temporal Reference

Reference implementation for the Validated Pattern *Orchestrate a Resumable Loan
Application Wizard Using Temporal Signals and Queries*. It models a multi-step
loan application as one long-lived, durable Temporal Workflow that validates each
step, resumes wherever the applicant left off, and reaches a decision through a
small Saga.

The same code runs against a local dev server or Temporal Cloud — the only
difference is environment variables (see [Point at Temporal Cloud](#point-at-temporal-cloud)).

See `docs/` for the design guide, the build plan, and the implementation task
list.

## Requirements

- Python 3.10 or later (the reference build pins Python 3.13)
- [uv](https://docs.astral.sh/uv/)
- The Temporal CLI, for the local development server (`temporal server start-dev`)
- Node.js 18 or later, for the React interface only

## Setup

```bash
uv sync
```

## Layout

The repository root is the source root — run the Worker and API from here so the
flat top-level imports (`shared`, `activities`, `workflows`, `api`) resolve.

```
loan-wizard/
├── shared/        # Pydantic models and pure validation logic
├── activities/    # mocked, idempotent Activities
├── workflows/     # LoanApplicationWorkflow
├── api/           # FastAPI client and routes
├── worker.py      # Temporal Worker entry point
├── web/           # React (Vite) wizard interface
├── tests/         # unit, integration, and replay tests
└── docs/          # design guide, plan, and task list
```

## Run it locally

Four processes: the Cluster, the Worker, the API, and (optionally) the interface.
Use four terminals. Everything below runs with the defaults — no environment
variables needed.

```bash
# 1. Start the local dev server (Web UI at http://localhost:8233).
temporal server start-dev

# 2. Start the Worker (from the repo root).
uv run python worker.py

# 3. Start the API (from the repo root).
uv run uvicorn api.main:app --reload --port 8000

# 4. Start the interface (needs Node; proxies /api to http://127.0.0.1:8000).
cd web && npm install && npm run dev   # http://localhost:5173
```

Then open http://localhost:5173, start an application, fill a couple of steps,
refresh the page, and watch it resume exactly where you left off.

> The interface expects the API on port 8000 and serves on 5173. If another
> process already holds either port, free it first (or run the API on another
> port and update the `proxy` target in `web/vite.config.ts`).

## Drive the primitives from the CLI

You can exercise the Workflow directly, without the interface, to see each
primitive in the Web UI at http://localhost:8233.

```bash
# Start an application Workflow.
temporal workflow start \
  --task-queue loan-applications \
  --type LoanApplicationWorkflow \
  --workflow-id loan-application-demo \
  --input '"demo"'

# Update: submit and validate a step (returns the new state synchronously).
temporal workflow update execute \
  --workflow-id loan-application-demo \
  --name submit_step \
  --input '{"step":"applicant","payload":{"full_name":"Ada","email":"ada@example.com","date_of_birth":"1990-01-01"}}'

# Query: read the current state (used for resume and for polling the decision).
temporal workflow query --workflow-id loan-application-demo --name get_state

# Signal: fire-and-forget autosave.
temporal workflow signal \
  --workflow-id loan-application-demo \
  --name save_draft \
  --input '{"step":"loan_details","partial":{"amount":10000}}'

# Signal: withdraw (ends the application as "withdrawn").
temporal workflow signal --workflow-id loan-application-demo --name withdraw
```

## Point at Temporal Cloud

No code changes — only environment variables. Set them for both the Worker
(step 2) and the API (step 3); everything else stays the same.

```bash
export TEMPORAL_ADDRESS="your-namespace.your-account.tmprl.cloud:7233"
export TEMPORAL_NAMESPACE="your-namespace.your-account"
export TEMPORAL_API_KEY="$(cat /path/to/your.key)"   # implies TLS
```

- `TEMPORAL_API_KEY` turns on TLS automatically and authenticates with the key.
- For a self-hosted cluster that terminates TLS without an API key, set
  `TEMPORAL_TLS=true` instead.
- Unset, the connection defaults to `localhost:7233`, namespace `default`, no TLS
  — the local dev server.

The Pydantic data converter is registered on both the Worker and the API client,
so payloads are encoded identically in every environment.

## Test

```bash
uv run pytest
```

The suite covers the pure validation logic, Activity idempotency, the full
Workflow lifecycle (submit, resume, withdraw, inactivity, Saga compensation), the
REST flow, and a replay test against a recorded history under
`tests/histories/`. Regenerate that history — only when the Workflow's command
sequence changes on purpose — with:

```bash
uv run python tests/histories/generate.py
```
