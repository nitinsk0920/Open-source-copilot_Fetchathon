"""Build GitHub issue-search queries. Pure functions, no I/O."""

import re
from datetime import date, timedelta
from typing import Sequence

DEFAULT_LABELS = ("good first issue",)
# Used when the first pass finds too little (recovery path, milestone 5).
WIDENED_LABELS = ("good first issue", "help wanted", "beginner", "easy", "first-timers-only")


def _quote(value: str) -> str:
    return f'"{value}"' if re.search(r"[^A-Za-z0-9_.+#-]", value) else value


def build_issue_query(
    language: str | None,
    labels: Sequence[str] = DEFAULT_LABELS,
    recency_days: int = 90,
    today: date | None = None,
    repo: str | None = None,
) -> str:
    """Open, unassigned issues (not PRs) in non-archived repos, recently updated.

    `label:a,b` is OR in GitHub's syntax, so several labels widen the search.
    """
    today = today or date.today()
    parts = ["is:issue", "is:open", "no:assignee", "archived:false"]
    if repo:
        parts.append(f"repo:{repo}")
    if language:
        parts.append(f"language:{_quote(language.strip().lower())}")
    labels = [lbl.strip() for lbl in labels if lbl.strip()] or list(DEFAULT_LABELS)
    parts.append("label:" + ",".join(_quote(lbl) for lbl in labels))
    parts.append(f"updated:>={(today - timedelta(days=recency_days)).isoformat()}")
    return " ".join(parts)
