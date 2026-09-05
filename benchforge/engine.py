import hashlib
import json
import os
import platform
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .adapters import configuration, generate, provider_status
from .envfile import load_env_file
from .git import git
from .patch import InvalidPatch, extract_patch, validate_patch
from .process import execute
from .schema import EvaluationSpec, ModelContext, PublicTask
from . import storage

PROJECT = Path(__file__).resolve().parent.parent
BENCHMARKS_DIR = PROJECT / "benchmarks"
DEFAULT_BENCHMARK_ID = "tiny-v0.1"
IMAGE = os.getenv("BENCHFORGE_IMAGE", "python:3.12-slim")
SUITE_ID = "python-starter-v0.1"


def _posix_relpath(value):
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts or path.parts[0] == "/":
        raise ValueError(f"Unsafe fixture path: {value}")
    posix = path.as_posix()
    if posix != str(value).replace("\\", "/"):
        raise ValueError(f"Unsafe fixture path: {value}")
    return posix


def load_fixture(path):
    raw = path.read_bytes()
    data = json.loads(raw)
    tests = data.get("tests") or {}
    required = ("id", "version", "task", "environment", "timeout_seconds")
    missing = [key for key in required if key not in data]
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        source = data.get("source")
        allowed = data.get("allowed_files") or ["calculator.py"]
        if not isinstance(source, str) or not allowed:
            missing.append("files")
        else:
            files = {allowed[0]: source}
            data = {**data, "files": files}
    if "issue" not in tests or "regression" not in tests:
        missing.append("tests.issue/regression")
    if "baseline" not in data:
        missing.append("baseline")
    commands = data.get("commands") or {}
    if not isinstance(commands.get("task"), list) or not isinstance(commands.get("regression"), list):
        missing.append("commands.task/regression")
    mocks = (data.get("mocks") or {}).get("fixed")
    if not isinstance(mocks, dict) or not mocks:
        missing.append("mocks.fixed")
    if missing:
        raise ValueError("Fixture is missing: " + ", ".join(missing))

    try:
        files = {_posix_relpath(name): content for name, content in data["files"].items()}
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    if not all(isinstance(content, str) for content in files.values()):
        raise ValueError("Fixture files must be text.")
    data["files"] = files
    allowed = tuple(
        _posix_relpath(name) for name in (data.get("allowed_files") or files)
    )
    if not allowed:
        raise ValueError("Fixture allowed_files must not be empty.")
    for name in allowed:
        if name not in files:
            raise ValueError(f"allowed_files entry is not in files: {name}")
    data["allowed_files"] = list(allowed)
    for name in mocks:
        if name not in allowed:
            raise ValueError("mocks.fixed may only include allowed files.")
        if not isinstance(mocks[name], str):
            raise ValueError("mocks.fixed values must be text.")
    data["_path"] = str(path)
    data["_hash"] = hashlib.sha256(raw).hexdigest()
    return data


def load_catalog(directory=BENCHMARKS_DIR):
    fixtures = {}
    hashes = {}
    for path in sorted(directory.glob("*.json")):
        data = load_fixture(path)
        if data["id"] in fixtures:
            raise ValueError(f"Duplicate benchmark id: {data['id']}")
        fixtures[data["id"]] = data
        hashes[data["id"]] = data["_hash"]
    if not fixtures:
        raise ValueError("No benchmark fixtures found.")
    return fixtures, hashes


FIXTURES, FIXTURE_HASHES = load_catalog()
BENCHMARK = FIXTURES[DEFAULT_BENCHMARK_ID]
BENCHMARK_HASH = FIXTURE_HASHES[DEFAULT_BENCHMARK_ID]
SPEC = EvaluationSpec(
    task_command=list(BENCHMARK["commands"]["task"]),
    regression_command=list(BENCHMARK["commands"]["regression"]),
    allowed_files=tuple(BENCHMARK["allowed_files"]),
)


def fixture_by_id(benchmark_id=None):
    key = benchmark_id or DEFAULT_BENCHMARK_ID
    fixture = FIXTURES.get(key)
    if fixture is None:
        raise KeyError(key)
    return fixture


def spec_for(fixture):
    return EvaluationSpec(
        task_command=list(fixture["commands"]["task"]),
        regression_command=list(fixture["commands"]["regression"]),
        allowed_files=tuple(fixture["allowed_files"]),
    )

_PROBE = {"at": 0.0, "value": None}
PROBE_TTL_SECONDS = 10


def public_benchmark(fixture=None):
    data = fixture or BENCHMARK
    task = data["task"]
    return {
        "id": data["id"],
        "version": data["version"],
        "standard": data.get("standard") or "application",
        "family": data.get("family"),
        "summary": data.get("summary") or task.get("description"),
        "task": {
            "id": task["id"],
            "title": task["title"],
            "description": task["description"],
            "constraints": task.get("constraints") or [],
            "language": task.get("language"),
            "category": task.get("category"),
            "repository": task.get("repository"),
        },
        "expected": data.get("expected") or {},
        "allowed_files": list(data["allowed_files"]),
        "context_files": sorted(data["files"]),
        "environment": data["environment"],
        "timeout_seconds": data["timeout_seconds"],
        "demo": True,
    }


def public_catalog():
    order = {"platform": 0, "application": 1}
    items = sorted(
        FIXTURES.values(),
        key=lambda item: (
            order.get(item.get("standard"), 9),
            item["id"],
        ),
    )
    return {
        "id": SUITE_ID,
        "version": "1",
        "title": "Python starter suite",
        "description": (
            "Synthetic fixtures that set minimum application coding checks "
            "and platform patch-scope rules for this harness."
        ),
        "benchmarks": [public_benchmark(item) for item in items],
    }


def baseline_issue_matches(result, baseline=None):
    spec = baseline if baseline is not None else BENCHMARK["baseline"]
    stderr = result.get("stderr") or ""
    needles = spec.get("issue_stderr_must_contain") or []
    return (
        result.get("exit_code") == spec.get("issue_exit_code", 1)
        and not result.get("timeout")
        and all(needle in stderr for needle in needles)
    )


def classify_sandbox_failure(result, cancel_requested=False):
    if cancel_requested:
        return "CANCELLED"
    if result.get("spawn_error") or result.get("exit_code") in (125, 126, 127):
        return "INFRA_ERROR"
    return None


def probe_environment(force=False):
    load_env_file()
    now_ts = time.monotonic()
    if (
        not force
        and _PROBE["value"] is not None
        and now_ts - _PROBE["at"] < PROBE_TTL_SECONDS
    ):
        return _PROBE["value"]
    git_installed = bool(shutil.which("git"))
    docker_installed = bool(shutil.which("docker"))
    docker_daemon = False
    image_present = False
    image_id = None
    image_digests = []

    if docker_installed:
        info = execute(["docker", "info"], timeout=8)
        docker_daemon = info["exit_code"] == 0 and not info["spawn_error"]
        if docker_daemon:
            inspected = execute(["docker", "image", "inspect", IMAGE], timeout=8)
            if inspected["exit_code"] == 0 and not inspected["spawn_error"]:
                try:
                    payload = json.loads(inspected["stdout"])[0]
                    image_present = True
                    image_id = payload.get("Id")
                    image_digests = payload.get("RepoDigests") or []
                except (json.JSONDecodeError, IndexError, TypeError, AttributeError):
                    image_present = False

    providers = provider_status()
    result = {
        "git_installed": git_installed,
        "docker_installed": docker_installed,
        "docker_daemon": docker_daemon,
        "image": IMAGE,
        "image_present": image_present,
        "image_id": image_id,
        "image_digests": image_digests,
        "provider_configured": providers["any"],
        "providers": {
            "chat-completions": providers["chat-completions"],
            "anthropic-messages": providers["anthropic-messages"],
        },
        "ready": git_installed and docker_daemon and image_present,
    }
    _PROBE["at"] = now_ts
    _PROBE["value"] = result
    return result


def request_cancel(run_id):
    record = storage.get(run_id)
    if record is None:
        return None
    if record["status"] not in storage.ACTIVE:
        return record

    record["cancel_requested"] = True
    record["status"] = "CANCELLED"
    record["finished_at"] = now()
    record["errors"].append("Run cancelled.")
    record["events"].append({
        "at": now(),
        "phase": "CANCELLED",
        "message": "Run cancelled.",
    })
    storage.save(record)
    listed = execute(
        ["docker", "ps", "-aq", "--filter", f"name=benchforge-{run_id[:12]}"],
        timeout=10,
    )
    identifiers = listed["stdout"].split()
    if identifiers:
        execute(["docker", "rm", "-f", *identifiers], timeout=20)
    return record


def now():
    return datetime.now(timezone.utc).isoformat()


class Halt(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def new_record(adapter, experiment_id, trial, parent_run_id=None, benchmark_id=None):
    fixture = fixture_by_id(benchmark_id)
    config = configuration(adapter)
    env = fixture["environment"]
    return {
        "id": uuid.uuid4().hex,
        "experiment_id": experiment_id,
        "trial": trial,
        "created_at": now(),
        "finished_at": None,
        "benchmark_id": fixture["id"],
        "benchmark_hash": fixture["_hash"],
        "task_id": fixture["task"]["id"],
        "repository": fixture["task"]["repository"],
        "base_commit": None,
        "model": config["model"],
        "model_config": config,
        "adapter": adapter,
        "demo": config["demo"],
        "status": "QUEUED",
        "baseline_status": "NOT_RUN",
        "model_status": "NOT_RUN",
        "patch_status": "NOT_RUN",
        "test_status": "NOT_RUN",
        "resolved": False,
        "duration": None,
        "model_latency": None,
        "token_usage": {},
        "cost": None,
        "files_changed": [],
        "lines_added": 0,
        "lines_removed": 0,
        "patch": "",
        "events": [{"at": now(), "phase": "QUEUED", "message": "Run queued."}],
        "commands": [],
        "errors": [],
        "cancel_requested": False,
        "container_name": None,
        "parent_run_id": parent_run_id,
        "environment": {
            "requested_image": IMAGE,
            "network": env.get("network", "none"),
            "memory": env.get("memory", "256m"),
            "cpus": str(env.get("cpus", "1")),
            "pids": env.get("pids", 64),
            "command_timeout": fixture["timeout_seconds"],
            "controller_platform": platform.platform(),
            "controller_python": platform.python_version(),
            "evaluator_version": __version__,
        },
    }


def run_evaluation(run_id):
    record = storage.get(run_id)
    fixture = fixture_by_id(record["benchmark_id"])
    spec = spec_for(fixture)
    started = time.monotonic()
    artifact_dir = storage.ARTIFACTS / run_id
    workspace = artifact_dir / "workspace"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    limits = record["environment"]

    def persist():
        storage.save(record)

    def raise_if_cancelled():
        latest = storage.get(run_id) or record
        if latest.get("cancel_requested"):
            record["cancel_requested"] = True
            raise Halt("CANCELLED", "Run cancelled.")

    def event(phase, message):
        record["status"] = phase
        record["events"].append({
            "at": now(), "phase": phase, "message": message
        })
        persist()
        if phase in storage.ACTIVE:
            raise_if_cancelled()

    def track(label, result):
        result["label"] = label
        record["commands"].append(result)
        persist()
        return result

    def checked_git(*args):
        result = track("git", git(workspace, *args))
        if result["exit_code"] != 0:
            raise Halt("INFRA_ERROR", "Git preparation or diff inspection failed.")
        return result

    def sandbox(label, command):
        name = f"benchforge-{run_id[:12]}-{uuid.uuid4().hex[:8]}"
        args = [
            "docker", "run", "--rm",
            "--name", name,
            "--network", limits.get("network", "none"),
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--user", "65534:65534",
            "--memory", str(limits.get("memory", "256m")),
            "--memory-swap", str(limits.get("memory", "256m")),
            "--cpus", str(limits.get("cpus", "1")),
            "--pids-limit", str(limits.get("pids", 64)),
            "--ulimit", "nofile=256:256",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
            "--mount", f"type=bind,source={workspace},target=/repo,readonly",
            "--workdir", "/repo",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PYTHONNOUSERSITE=1",
            "--env", "HOME=/tmp",
            "--env", "PYTHONHASHSEED=0",
            "--env", "TZ=UTC",
            "--entrypoint", "python",
            record["environment"]["image_id"],
            *command[1:],
        ]
        record["container_name"] = name
        persist()
        try:
            result = track(
                label,
                execute(args, timeout=limits.get("command_timeout", 30)),
            )
        finally:
            track(
                "container cleanup",
                execute(["docker", "rm", "-f", name], timeout=15),
            )
            record["container_name"] = None
            persist()

        failure = classify_sandbox_failure(
            result, cancel_requested=bool(
                record.get("cancel_requested")
                or (storage.get(run_id) or {}).get("cancel_requested")
            ),
        )
        if failure == "CANCELLED":
            raise Halt("CANCELLED", "Run cancelled.")
        if failure == "INFRA_ERROR":
            raise Halt("INFRA_ERROR", f"Docker could not execute {label}.")
        raise_if_cancelled()
        return result

    def successful(result):
        return result["exit_code"] == 0 and not result["timeout"]

    try:
        event("PREPARING", "Preparing clean fixture and isolated execution.")
        if not shutil.which("git") or not shutil.which("docker"):
            raise Halt("INFRA_ERROR", "Git and Docker are required.")

        inspected = track(
            "inspect image",
            execute(["docker", "image", "inspect", IMAGE], timeout=20),
        )
        if inspected["exit_code"] != 0:
            raise Halt(
                "INFRA_ERROR",
                f"Docker image unavailable. Start Docker and pull {IMAGE}.",
            )

        image = json.loads(inspected["stdout"])[0]
        record["environment"]["image_id"] = image["Id"]
        record["environment"]["image_digests"] = image.get("RepoDigests", [])
        record["environment"]["image_os"] = image.get("Os")
        record["environment"]["image_architecture"] = image.get("Architecture")

        for rel, content in fixture["files"].items():
            dest = workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        (workspace / "tests").mkdir()
        (workspace / "tests" / "__init__.py").write_text("")
        (workspace / "tests" / "test_regression.py").write_text(
            fixture["tests"]["regression"], encoding="utf-8"
        )
        (workspace / "tests" / "test_issue.py").write_text(
            fixture["tests"]["issue"], encoding="utf-8"
        )
        (workspace / ".gitignore").write_text("__pycache__/\n*.pyc\n")

        # Keep files readable by the unprivileged container user.
        workspace.chmod(0o755)
        for path in workspace.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)

        checked_git("init", "-q")
        checked_git("add", ".")
        checked_git("commit", "-qm", "BenchForge deterministic fixture")
        record["base_commit"] = checked_git(
            "rev-parse", "HEAD"
        )["stdout"].strip()

        if checked_git("status", "--porcelain")["stdout"].strip():
            raise Halt("INFRA_ERROR", "Initial workspace is not clean.")

        event("BASELINE", "Checking passing regressions and the expected issue failure.")
        regression = sandbox("baseline regression", spec.regression_command)
        issue = sandbox("baseline issue", spec.task_command)

        # An arbitrary nonzero exit is not sufficient baseline evidence.
        if not successful(regression) or not baseline_issue_matches(issue, fixture["baseline"]):
            record["baseline_status"] = "INVALID"
            raise Halt(
                "BASELINE_INVALID",
                "Baseline did not match the fixture's expected conditions.",
            )
        record["baseline_status"] = "VALID"

        event("MODEL", "Sending only the public issue and allowed source context.")
        task_data = fixture["task"]
        public_task = PublicTask(
            id=task_data["id"],
            title=task_data["title"],
            description=task_data["description"],
            constraints=task_data["constraints"],
        )
        context = ModelContext(files=dict(fixture["files"]))

        try:
            model_output = generate(
                record["model_config"],
                public_task,
                context,
                mock_fixed=(fixture.get("mocks") or {}).get("fixed"),
            )
        except Exception as exc:
            record["model_status"] = "FAILED"
            # Adapter errors are controlled; never persist request headers.
            message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            raise Halt("MODEL_FAILED", f"Model generation failed: {message}") from None

        record["model_status"] = "SUCCEEDED"
        record["model_latency"] = model_output["latency"]
        record["token_usage"] = model_output["usage"]
        record["returned_model"] = model_output["returned_model"]
        (artifact_dir / "model-response.txt").write_text(
            model_output["text"], encoding="utf-8"
        )

        event("PATCH", "Validating patch syntax, file scope, and applicability.")
        try:
            patch = extract_patch(model_output["text"])
            validate_patch(patch, spec.allowed_files)
        except InvalidPatch as exc:
            record["patch_status"] = "INVALID"
            raise Halt("PATCH_INVALID", str(exc)) from None

        (artifact_dir / "patch.diff").write_text(patch, encoding="utf-8")
        if patch:
            result = track(
                "patch check",
                git(
                    workspace, "apply", "--check", "--whitespace=nowarn",
                    str(artifact_dir / "patch.diff"),
                ),
            )
            if result["exit_code"] != 0:
                record["patch_status"] = "APPLY_FAILED"
                raise Halt("PATCH_APPLY_FAILED", "git apply --check rejected the patch.")

            result = track(
                "apply patch",
                git(
                    workspace, "apply", "--whitespace=nowarn",
                    str(artifact_dir / "patch.diff"),
                ),
            )
            if result["exit_code"] != 0:
                record["patch_status"] = "APPLY_FAILED"
                raise Halt("PATCH_APPLY_FAILED", "git apply failed.")

            record["patch_status"] = "APPLIED"
        else:
            record["patch_status"] = "EMPTY"

        names = checked_git("diff", "--name-only")["stdout"].splitlines()
        untracked = checked_git(
            "ls-files", "--others", "--exclude-standard"
        )["stdout"].splitlines()

        if untracked or any(name not in spec.allowed_files for name in names):
            record["patch_status"] = "INVALID"
            raise Halt("PATCH_INVALID", "Final workspace contains forbidden changes.")

        for name in spec.allowed_files:
            source_file = workspace / name
            if source_file.is_symlink() or not source_file.is_file():
                raise Halt("PATCH_INVALID", "Source must remain a regular file.")

        record["patch"] = checked_git("diff", "--no-ext-diff", "--binary")["stdout"]
        record["files_changed"] = names
        numstat = checked_git("diff", "--numstat")["stdout"]
        for line in numstat.splitlines():
            added, removed, _ = line.split("\t", 2)
            if added.isdigit() and removed.isdigit():
                record["lines_added"] += int(added)
                record["lines_removed"] += int(removed)

        (artifact_dir / "patch.diff").write_text(record["patch"], encoding="utf-8")

        event("EVALUATING", "Running task checks and regressions in fresh containers.")
        issue = sandbox("task tests", spec.task_command)
        regression = sandbox("regression tests", spec.regression_command)
        record["task_tests_passed"] = successful(issue)
        record["regression_tests_passed"] = successful(regression)
        record["resolved"] = successful(issue) and successful(regression)
        record["test_status"] = "PASSED" if record["resolved"] else "FAILED"

        if record["resolved"]:
            event("TASK_RESOLVED", "All required task and regression checks passed.")
        else:
            event("TEST_FAILED", "The candidate did not pass all required checks.")

    except Halt as exc:
        record["errors"].append(str(exc))
        event(exc.status, str(exc))
    except Exception as exc:
        record["errors"].append(f"Internal infrastructure failure: {type(exc).__name__}")
        event("INFRA_ERROR", "Unexpected infrastructure failure.")
    finally:
        record["finished_at"] = now()
        record["duration"] = round(time.monotonic() - started, 3)
        persist()
        (artifact_dir / "result.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        # Workspace is disposable. Patch and run metadata remain.
        shutil.rmtree(workspace, ignore_errors=True)
