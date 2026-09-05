# BenchForge

A local-first coding evaluation starter with a responsive web dashboard.

## Requirements

- Python 3.11+ (3.13 recommended; 3.14 needs current pydantic wheels)
- Git
- Docker Engine or Docker Desktop, running
- An explicitly pulled Python sandbox image

## Start

```bash
python -m venv .venv
source .venv/bin/activate
# Windows: .venv\Scripts\activate

pip install -r requirements.txt
docker pull python:3.12-slim
uvicorn benchforge.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000

Use one Uvicorn worker. This starter uses an in-process job queue, not a
distributed scheduler. Do not use --reload while evaluations are running.

## Included

- Responsive dashboard, run history, comparison bars, run-detail drawer
- Patch, command, stdout, stderr, and timeline inspection
- SQLite run persistence
- JSON artifacts and CSV export
- Repeated trials
- Two explicit smoke-test mock adapters
- Optional HTTP providers: chat completions (OpenAI-compatible APIs) and Anthropic Messages
- Runtime health for Git, Docker daemon, and sandbox image, with a manual refresh
- Cancel, retry, and delete completed runs
- Five bundled Python fixtures covering application contracts and patch-scope rules
- Baseline validation, patch validation, task tests, regression tests
- Centralized subprocess and Git utilities
- Docker execution with network disabled, read-only mounts, non-root user,
  dropped capabilities, memory/CPU/PID limits, and command timeouts

## Important scope

The bundled suite is synthetic. Mock adapter results are infrastructure smoke
tests and must not be reported as model-quality evidence.

This starter supports patch-only Python tasks. It does not implement GitHub
mining, arbitrary remote repositories, autonomous tool-using agents,
authentication, distributed workers, or multi-tenant hosting.

The app binds to localhost by default. Do not expose it publicly.

## Optional providers

Copy `.env.example` to `.env` in the project directory, or export variables in
your shell. The server loads `.env` on startup and does not override variables
already in the environment. Restart after changes.

Chat completions (`adapter: chat-completions`) uses POST `/chat/completions`.
Point `PROVIDER_BASE_URL` at any compatible host:

```bash
export PROVIDER_BASE_URL="https://api.openai.com/v1"
export PROVIDER_API_KEY="your-key"
export PROVIDER_MODEL="your-model-id"
```

`OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL` remain aliases.
Azure OpenAI may put `api-version` in the URL query and set `PROVIDER_AUTH=api-key`.
Local Ollama typically needs no key (`PROVIDER_AUTH=none`).

Anthropic (`adapter: anthropic-messages`) uses POST `/v1/messages`:

```bash
export ANTHROPIC_API_KEY="your-key"
export ANTHROPIC_MODEL="your-model-id"
```

Providers that need a different protocol still need an adapter extension.

Only the public issue and selected source file go to the provider. Hidden
tests and evaluator metadata are not included in the request.

Provider calls may transmit repository contents to a third party. Obtain
permission before extending this to private repositories.

API keys are never included in run configuration or container environments.
Do not put secrets in the API base URL.

Costs are unknown/null: this starter does not invent pricing.
A provider model identifier is not necessarily an immutable model version.

## Starter suite

These fixtures are the local standard the dashboard and harness are built to run:

| ID | Standard | What it checks |
| --- | --- | --- |
| `scope-v0.1` | Platform | Patch allowlist: fix `registry.py`, leave `util.py` alone |
| `tiny-v0.1` | Application | Replace a crash with an explicit error contract |
| `bounds-v0.1` | Application | Inclusive range / off-by-one without breaking neighbors |
| `merge-v0.1` | Application | Nested mapping merge without mutating inputs |
| `query-v0.1` | Application | `+` and percent-decoding in form-urlencoded queries |

Hidden tests, source, and mock patches are never returned by `GET /api/benchmarks`.

## Reproducibility

Use an image digest rather than a moving tag:

```bash
docker image inspect python:3.12-slim --format '{{index .RepoDigests 0}}'
export BENCHFORGE_IMAGE="python@sha256:..."
```

Run records include the actual local Docker image ID, image digests,
Git base commit, fixture definition hash, model configuration, evaluator
version, platform, commands, patch, and outcome.

Exact model response replay is not guaranteed. The image captures the Python
runtime; the bundled fixture needs no third-party repository dependencies.

The image must already exist locally. Evaluation does not silently pull images.

## Security and evaluation limitations

Repository Python code only executes inside Docker. Host subprocesses are
restricted to Git operations, Docker management, and Docker execution.

Docker shares the host kernel and is not a complete boundary for actively
hostile code. Use a dedicated disposable VM or a stronger sandbox before
evaluating adversarial repositories.

The Docker socket is used by the trusted controller and is never mounted into
a task container.

Agent-style direct filesystem access is not implemented. The patch-only
adapter sees bundled source context. Only allowlisted files may be changed;
`scope-v0.1` exists to keep that rule honest.

Hidden tests are supplied for evaluation, not to the model. However, Python
code under test shares an interpreter with the test framework and can attempt
to tamper with its observations. The starter is intended for cooperative
patch evaluation, not tamper-proof competitive scoring.

No host-directory disk quota is provided. Container writes are restricted to
a bounded tmpfs; process output capture is bounded. Stronger disk accounting,
evaluator integrity, and secret/exfiltration controls need additional work.

There is no host-execution fallback when Docker is unavailable.

## Tests

```bash
python -m unittest discover -s tests -v
```

These test schema-independent patch/process helpers and provider payload
parsing. For an end-to-end smoke test, run both mock adapters from the
dashboard against each bundled fixture:

- Mock / fixed should resolve the selected fixture
- Mock / unchanged should fail its task-specific test

A Docker outage should produce INFRA_ERROR, not a model failure.

## Layout

benchforge/
  main.py          HTTP API and queue
  engine.py        evaluation orchestration
  adapters.py      public-context-only adapters
  process.py       bounded subprocess capture and timeout handling
  git.py           centralized Git operations
  patch.py         patch extraction and allowlist validation
  storage.py       SQLite persistence
  schema.py        public and hidden data types
  static/          dashboard
benchmarks/
  tiny-v0.1.json     error handling
  bounds-v0.1.json   inclusive range / off-by-one
  merge-v0.1.json    nested dict contract
  query-v0.1.json    URL-encoded query protocol
  scope-v0.1.json    allowlisted patch, read-only helper
tests/
  test_core.py

## API

GET  /api/health
GET  /api/benchmarks
GET  /api/benchmarks/{id}
GET  /api/benchmark          (alias of /api/benchmarks)
GET  /api/runs
POST /api/runs
POST /api/runs/{id}/cancel
POST /api/runs/{id}/retry
DELETE /api/runs/{id}
GET  /api/runs/{id}
GET  /api/runs/{id}/artifacts/{name}
GET  /api/export.csv

POST /api/runs:

```json
{
  "adapter": "mock-fixed",
  "benchmark_id": "tiny-v0.1",
  "trials": 2
}
```

Allowed adapters: mock-fixed, mock-unchanged, chat-completions,
openai-compatible (alias), anthropic-messages.

POST /api/runs returns 503 if Git, the Docker daemon, or the sandbox image
is unavailable. Provider adapters return 400 until their model env is set.

Generated artifacts:
- result.json
- patch.diff
- model-response.txt

## Next extension points

1. Add mined or multi-language tasks on top of the bundled Python suite.
2. Add trusted image builders and dependency lock validation.
3. Add independent evaluator services and stronger isolation.
4. Implement agent tool permissions and complete trajectories.
5. Replace the local queue with durable workers.
6. Add mined-task validation and statistically sound paired reports.
