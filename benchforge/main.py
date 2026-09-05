import csv
import io
import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import storage
from .engine import BENCHMARK, IMAGE, new_record, run_evaluation
from .schema import RunRequest

STATIC = Path(__file__).parent / "static"
POOL = None
SUBMISSION_LOCK = threading.Lock()
MAX_PENDING = 30


@asynccontextmanager
async def lifespan(app):
    global POOL
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
def health():
    return {
        "git_installed": bool(shutil.which("git")),
        "docker_installed": bool(shutil.which("docker")),
        "image": IMAGE,
        "provider_configured": bool(os.getenv("OPENAI_MODEL")),
        "note": "Executable detection only; daemon and image are checked per run.",
    }


@app.get("/api/benchmark")
def benchmark():
    return {
        "id": BENCHMARK["id"],
        "version": BENCHMARK["version"],
        "task": BENCHMARK["task"],
        "environment": BENCHMARK["environment"],
        "timeout_seconds": BENCHMARK["timeout_seconds"],
        "demo": True,
    }


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
                new_record(body.adapter, experiment, trial + 1)
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
