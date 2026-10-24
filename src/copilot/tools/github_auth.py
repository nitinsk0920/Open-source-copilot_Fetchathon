"""The single place GitHub credentials come from.

Hackathon: one shared PAT (a throwaway demo account, public_repo scope). A real product would
look up a per-user OAuth token here; change only this function and nothing else needs to move.
"""

import os

from copilot import config  # noqa: F401  (importing it loads .env)


def get_github_token(user_id: str | None = None) -> str | None:
    """Return the token to use for `user_id`, or None to make unauthenticated requests."""
    token = os.getenv("GITHUB_TOKEN", "").strip()
    return token or None
