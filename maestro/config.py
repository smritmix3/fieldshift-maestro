"""Settings: reads a local `.env` (if present) into the environment. On Hugging Face, secrets are
already environment variables, so nothing else is needed."""
from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env(path: Path = ENV_PATH) -> None:
    """Load KEY=VALUE lines into os.environ without overriding real environment variables."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def get_int(name: str, default: int) -> int:
    try:
        return int(get(name) or default)
    except ValueError:
        return default


def mask(secret: str) -> str:
    """Safe display of a key: never print more than the last 4 characters."""
    return "(not set)" if not secret else f"…{secret[-4:]}" if len(secret) > 8 else "****"


load_env()
