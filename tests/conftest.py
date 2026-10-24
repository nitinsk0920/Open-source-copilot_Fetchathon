import pytest


@pytest.fixture(autouse=True)
def _no_real_token(monkeypatch):
    """Tests must never pick up the developer's real token from .env."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-real")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("ALLOWED_TARGET_REPOS", "")


async def no_sleep(_seconds: float) -> None:
    return None
