"""Environment-driven config. Values from .env are never logged."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from uagents.crypto import Identity

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

PROFILES_DIR = ROOT / "agent_profiles"


def require_env(name: str) -> str:
    """Return a required env var, failing with a message that names the variable only."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var {name}. Set it in .env (see .env.example).")
    return value


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def demo_mode() -> bool:
    return os.getenv("DEMO_MODE", "true").strip().lower() not in {"false", "0", "no", "off"}


def allowed_repos() -> set[str]:
    return {r.strip().lower() for r in os.getenv("ALLOWED_TARGET_REPOS", "").split(",") if r.strip()}


def live_writes_allowed(repo: str) -> bool:
    """In demo mode only allowlisted repos get real writes; everything else is a dry run."""
    return (not demo_mode()) or repo.lower() in allowed_repos()


def connectivity(port: int) -> dict:
    """Agent(...) kwargs: Agentverse mailbox normally; plain localhost endpoint when LOCAL_ONLY=true (dev/e2e tests)."""
    if os.getenv("LOCAL_ONLY", "").strip().lower() in {"1", "true", "yes"}:
        return {"mailbox": False, "endpoint": [f"http://127.0.0.1:{port}/submit"]}
    return {"mailbox": True}


def agent_address(seed_var: str, address_var: str = "") -> str:
    """Address of another agent: an explicit override if set, else derived from its seed."""
    explicit = os.getenv(address_var, "").strip() if address_var else ""
    return explicit or Identity.from_seed(require_env(seed_var), 0).address


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
