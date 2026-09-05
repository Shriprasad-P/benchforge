from pathlib import Path
import os


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path=None):
    """Load KEY=VALUE pairs without overriding variables already in the environment."""
    env_path = Path(path) if path else PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return

    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def first_env(*names, default=""):
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default
