import csv
import io
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import storage
from .adapters import PROVIDER_ADAPTERS
from .envfile import load_env_file
from .engine import (
    FIXTURES,
    fixture_by_id,
    new_record,
    probe_environment,
    public_benchmark,
    public_catalog,
    request_cancel,
    run_evaluation,
)
from .schema import RunRequest

STATIC = Path(__file__).parent / "static"
POOL = None
SUBMISSION_LOCK = threading.Lock()
MAX_PENDING = 30


@asynccontextmanager
async def lifespan(app):
    global POOL
    load_env_file()
    storage.initialize()
    storage.recover_interrupted()
    POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="benchforge")
    yield
    POOL.shutdown(wait=True)


app = FastAPI(
    title="BenchForge",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"]
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin:
            expected = urlparse(str(request.base_url))
            actual = urlparse(origin)
            if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
                return Response("Cross-origin request rejected.", status_code=403)

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
        "form-action 'self'"
    )
    return response


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health(refresh: bool = Query(default=False)):
    return probe_environment(force=refresh)


@app.get("/api/benchmarks")
def benchmarks():
    return public_catalog()


@app.get("/api/benchmarks/{benchmark_id}")
def benchmark_detail(benchmark_id: str):
    try:
        return public_benchmark(fixture_by_id(benchmark_id))
    except KeyError:
        raise HTTPException(404, "Benchmark not found.") from None


@app.get("/api/benchmark")
def benchmark():
    return public_catalog()


@app.get("/api/runs")
def runs():
    # Keep collection responses small; command output lives in the detail API.
    return [
        {
            key: value
            for key, value in run.items()
            if key not in ("commands", "events", "patch", "model_config")
        }
        for run in storage.list_runs()
    ]


@app.post("/api/runs", status_code=202)
def create_runs(body: RunRequest):
    return queue_runs(body)


def queue_runs(body, parent_run_id=None):
    health = probe_environment(force=True)
    if not health["ready"]:
        raise HTTPException(
            503,
            "Git, Docker daemon, and the sandbox image must be available.",
        )
    if body.adapter in PROVIDER_ADAPTERS:
        providers = health.get("providers") or {}
        chat = body.adapter in {"chat-completions", "openai-compatible"}
        configured = (
            providers.get("chat-completions")
            if chat
            else providers.get(body.adapter)
        )
        if not configured:
            raise HTTPException(
                400,
                "That provider is not configured. Set PROVIDER_MODEL or ANTHROPIC_MODEL.",
            )
    if body.benchmark_id not in FIXTURES:
        raise HTTPException(400, "Unknown benchmark_id.")

    with SUBMISSION_LOCK:
        pending = sum(
            run["status"] in storage.ACTIVE
            for run in storage.list_runs()
        )
        if pending + body.trials > MAX_PENDING:
            raise HTTPException(429, "Queue is full. Wait for current runs.")

        experiment = uuid.uuid4().hex
        try:
            records = [
                new_record(
                    body.adapter,
                    experiment,
                    trial + 1,
                    parent_run_id=parent_run_id,
                    benchmark_id=body.benchmark_id,
                )
                for trial in range(body.trials)
            ]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

        for record in records:
            storage.save(record)
            POOL.submit(run_evaluation, record["id"])

    return {"run_ids": [record["id"] for record in records]}


def find_run(run_id):
    record = storage.get(run_id)
    if record is None:
        raise HTTPException(404, "Run not found.")
    return record


@app.post("/api/runs/{run_id}/cancel", status_code=202)
def cancel_run(run_id: str):
    record = find_run(run_id)
    if record["status"] not in storage.ACTIVE:
        raise HTTPException(409, "Only in-progress runs can be cancelled.")
    updated = request_cancel(run_id)
    return {
        "id": run_id,
        "status": updated["status"],
        "cancel_requested": True,
    }


@app.post("/api/runs/{run_id}/retry", status_code=202)
def retry_run(run_id: str):
    original = find_run(run_id)
    if original["status"] in storage.ACTIVE:
        raise HTTPException(409, "Cannot retry an in-progress run.")
    return queue_runs(
        RunRequest(
            adapter=original["adapter"],
            trials=1,
            benchmark_id=original.get("benchmark_id") or "tiny-v0.1",
        ),
        parent_run_id=original["id"],
    )


@app.delete("/api/runs/{run_id}", status_code=204)
def delete_run(run_id: str):
    record = find_run(run_id)
    if record["status"] in storage.ACTIVE:
        raise HTTPException(409, "Cannot delete an in-progress run. Cancel it first.")
    storage.delete(run_id)
    shutil.rmtree(storage.ARTIFACTS / run_id, ignore_errors=True)
    return Response(status_code=204)


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    return find_run(run_id)


@app.get("/api/runs/{run_id}/artifacts/{name}")
def artifact(run_id: str, name: str):
    find_run(run_id)
    if name not in {"result.json", "patch.diff", "model-response.txt"}:
        raise HTTPException(404, "Artifact not found.")

    path = storage.ARTIFACTS / run_id / name
    if not path.is_file():
        raise HTTPException(404, "Artifact is not available yet.")

    return FileResponse(path, filename=f"{run_id[:8]}-{name}")


def csv_safe(value):
    # Prevent spreadsheet formula injection in exported text.
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


@app.get("/api/export.csv")
def export_csv():
    fields = [
        "id", "experiment_id", "trial", "created_at", "benchmark_id",
        "task_id", "adapter", "model", "demo", "status", "resolved",
        "duration", "model_latency", "cost", "lines_added", "lines_removed",
        "parent_run_id",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for record in storage.list_runs():
        writer.writerow({
            key: csv_safe(record.get(key))
            for key in fields
        })
    return Response(
        stream.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="benchforge-runs.csv"'
        },
    )
