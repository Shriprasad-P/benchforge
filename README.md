# BenchForge

A local-first coding evaluation starter with a responsive web dashboard.

## Requirements

- Python 3.11+
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
- Optional OpenAI-compatible patch-generation adapter
- Baseline validation, patch validation, task tests, regression tests
- Centralized subprocess and Git utilities
- Docker execution with network disabled, read-only mounts, non-root user,
  dropped capabilities, memory/CPU/PID limits, and command timeouts

## Important scope

The bundled benchmark is a small synthetic fixture, not a real-world
leaderboard. Mock adapter results are infrastructure smoke tests and must not
be reported as model-quality evidence.

This starter intentionally supports one patch-only Python task. It does not
implement GitHub mining, arbitrary remote repositories, autonomous tool-using
agents, authentication, distributed workers, or multi-tenant hosting.

The app binds to localhost by default. Do not expose it publicly.

## Optional provider

```bash
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_API_KEY="your-key"
export OPENAI_MODEL="your-model-id"
```

Restart the server after changing configuration.

The adapter uses POST /chat/completions. Providers that require a different API,
special reasoning parameters, or nonstandard token limits need an adapter
extension.

Only the public issue and selected source file go to the provider. Hidden
tests and evaluator metadata are not included in the request.

Provider calls may transmit repository contents to a third party. Obtain
permission before extending this to private repositories.

API keys are never included in run configuration or container environments.
Do not put secrets in the API base URL.

Costs are unknown/null: this starter does not invent pricing.
A provider model identifier is not necessarily an immutable model version.

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
adapter sees an explicit allowlist of source context. Only calculator.py may
be changed in this fixture.

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

These test schema-independent patch/process helpers. For an end-to-end smoke
test, run both mock adapters from the dashboard:

- Mock / fixed should resolve the fixture
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
  tiny-v0.1.json    declarative fixture definition
tests/
  test_core.py

## API

GET  /api/health
GET  /api/benchmark
GET  /api/runs
POST /api/runs
GET  /api/runs/{id}
GET  /api/runs/{id}/artifacts/{name}
GET  /api/export.csv

POST /api/runs:

```json
{
  "adapter": "mock-fixed",
  "trials": 2
}
```

Allowed adapters: mock-fixed, mock-unchanged, openai-compatible.

Generated artifacts:
- result.json
- patch.diff
- model-response.txt

## Next extension points

1. Introduce a versioned multi-repository benchmark schema.
2. Add trusted image builders and dependency lock validation.
3. Add independent evaluator services and stronger isolation.
4. Implement agent tool permissions and complete trajectories.
5. Replace the local queue with durable workers.
6. Add mined-task validation and statistically sound paired reports.
