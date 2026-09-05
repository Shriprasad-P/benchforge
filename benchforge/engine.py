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
from .adapters import configuration, generate
from .git import git
from .patch import InvalidPatch, extract_patch, validate_patch
from .process import execute
from .schema import EvaluationSpec, ModelContext, PublicTask
from . import storage

PROJECT = Path(__file__).resolve().parent.parent
BENCHMARK_PATH = PROJECT / "benchmarks" / "tiny-v0.1.json"
BENCHMARK_BYTES = BENCHMARK_PATH.read_bytes()
BENCHMARK = json.loads(BENCHMARK_BYTES)
BENCHMARK_HASH = hashlib.sha256(BENCHMARK_BYTES).hexdigest()
IMAGE = os.getenv("BENCHFORGE_IMAGE", "python:3.12-slim")

REGRESSION_TEST = """import unittest
from calculator import average

class RegressionTests(unittest.TestCase):
    def test_integer_values(self):
        self.assertEqual(average([2, 4, 6]), 4)

    def test_single_value(self):
        self.assertEqual(average([9]), 9)

    def test_negative_values(self):
        self.assertEqual(average([-6, -2]), -4)

    def test_fractional_values(self):
        self.assertAlmostEqual(average([1.5, 2.5]), 2.0)
"""

TASK_TEST = """import unittest
from calculator import average

class IssueTests(unittest.TestCase):
    def test_empty_values_raise_explicit_error(self):
        with self.assertRaises(ValueError) as caught:
            average([])
        self.assertEqual(str(caught.exception), "values must not be empty")
"""

SPEC = EvaluationSpec(
    task_command=["python", "-B", "-m", "unittest", "-v", "tests.test_issue"],
    regression_command=[
        "python", "-B", "-m", "unittest", "-v", "tests.test_regression"
    ],
)


def now():
    return datetime.now(timezone.utc).isoformat()


class Halt(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def new_record(adapter, experiment_id, trial):
    config = configuration(adapter)
    return {
        "id": uuid.uuid4().hex,
        "experiment_id": experiment_id,
        "trial": trial,
        "created_at": now(),
        "finished_at": None,
        "benchmark_id": BENCHMARK["id"],
        "benchmark_hash": BENCHMARK_HASH,
        "task_id": BENCHMARK["task"]["id"],
        "repository": BENCHMARK["task"]["repository"],
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
        "environment": {
            "requested_image": IMAGE,
            "network": "none",
            "memory": "256m",
            "cpus": "1",
            "pids": 64,
            "command_timeout": BENCHMARK["timeout_seconds"],
            "controller_platform": platform.platform(),
            "controller_python": platform.python_version(),
            "evaluator_version": __version__,
        },
    }


def run_evaluation(run_id):
    record = storage.get(run_id)
    started = time.monotonic()
    artifact_dir = storage.ARTIFACTS / run_id
    workspace = artifact_dir / "workspace"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)

    def persist():
        storage.save(record)

    def event(phase, message):
        record["status"] = phase
        record["events"].append({
            "at": now(), "phase": phase, "message": message
        })
        persist()

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
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--user", "65534:65534",
            "--memory", "256m",
            "--memory-swap", "256m",
            "--cpus", "1",
            "--pids-limit", "64",
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
        try:
            result = track(
                label,
                execute(args, timeout=BENCHMARK["timeout_seconds"]),
            )
        finally:
            track(
                "container cleanup",
                execute(["docker", "rm", "-f", name], timeout=15),
            )

        if result["spawn_error"] or result["exit_code"] in (125, 126, 127):
            raise Halt("INFRA_ERROR", f"Docker could not execute {label}.")
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

        # This is a trusted bundled fixture, not downloaded repository code.
        (workspace / "calculator.py").write_text(
            BENCHMARK["source"], encoding="utf-8"
        )
        (workspace / "tests").mkdir()
        (workspace / "tests" / "__init__.py").write_text("")
        (workspace / "tests" / "test_regression.py").write_text(
            REGRESSION_TEST, encoding="utf-8"
        )
        (workspace / "tests" / "test_issue.py").write_text(
            TASK_TEST, encoding="utf-8"
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
        regression = sandbox("baseline regression", SPEC.regression_command)
        issue = sandbox("baseline issue", SPEC.task_command)

        # An arbitrary nonzero exit is not sufficient baseline evidence.
        expected_issue_failure = (
            issue["exit_code"] == 1
            and not issue["timeout"]
            and "ZeroDivisionError" in issue["stderr"]
            and "Ran 1 test" in issue["stderr"]
        )
        if not successful(regression) or not expected_issue_failure:
            record["baseline_status"] = "INVALID"
            raise Halt(
                "BASELINE_INVALID",
                "Baseline did not match the fixture's expected conditions.",
            )
        record["baseline_status"] = "VALID"

        event("MODEL", "Sending only the public issue and allowed source context.")
        task_data = BENCHMARK["task"]
        public_task = PublicTask(
            id=task_data["id"],
            title=task_data["title"],
            description=task_data["description"],
            constraints=task_data["constraints"],
        )
        context = ModelContext(files={"calculator.py": BENCHMARK["source"]})

        try:
            model_output = generate(record["model_config"], public_task, context)
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
            validate_patch(patch, SPEC.allowed_files)
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

        if untracked or any(name not in SPEC.allowed_files for name in names):
            record["patch_status"] = "INVALID"
            raise Halt("PATCH_INVALID", "Final workspace contains forbidden changes.")

        source_file = workspace / "calculator.py"
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
        issue = sandbox("task tests", SPEC.task_command)
        regression = sandbox("regression tests", SPEC.regression_command)
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
