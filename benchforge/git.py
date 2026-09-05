import os
from .process import execute


def git(workspace, *args, timeout=20):
    env = os.environ.copy()
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "BenchForge",
        "GIT_AUTHOR_EMAIL": "fixture@benchforge.local",
        "GIT_COMMITTER_NAME": "BenchForge",
        "GIT_COMMITTER_EMAIL": "fixture@benchforge.local",
        "GIT_AUTHOR_DATE": "2024-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2024-01-01T00:00:00+00:00",
    })
    return execute(
        [
            "git",
            "-c", f"core.hooksPath={os.devnull}",
            "-c", "core.autocrlf=false",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=workspace,
        env=env,
        timeout=timeout,
    )
