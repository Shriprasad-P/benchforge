import os
import signal
import subprocess
import threading
import time

CAPTURE_LIMIT = 128 * 1024


def execute(args, cwd=None, env=None, timeout=30):
    """No shell. Capture bounded output while continuously draining pipes."""
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            list(args),
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=(os.name != "nt"),
        )
    except OSError as exc:
        return {
            "command": list(args),
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "timeout": False,
            "duration": round(time.monotonic() - started, 3),
            "spawn_error": True,
            "output_truncated": False,
        }

    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}

    def drain(pipe, key):
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    break
                remaining = CAPTURE_LIMIT - len(buffers[key])
                if remaining > 0:
                    buffers[key].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated[key] = True
        finally:
            pipe.close()

    readers = [
        threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True),
    ]
    for thread in readers:
        thread.start()

    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    # Docker containers are additionally removed by the sandbox lifecycle.
    for thread in readers:
        thread.join(timeout=2)

    return {
        "command": list(args),
        "exit_code": process.returncode,
        "stdout": bytes(buffers["stdout"]).decode("utf-8", errors="replace"),
        "stderr": bytes(buffers["stderr"]).decode("utf-8", errors="replace"),
        "timeout": timed_out,
        "duration": round(time.monotonic() - started, 3),
        "spawn_error": False,
        "output_truncated": any(truncated.values()),
    }
