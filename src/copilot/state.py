"""Per-user session state: in memory, mirrored to a JSON file so a restart doesn't lose the conversation."""

import logging
import os
from pathlib import Path

from pydantic import BaseModel, Field

from copilot.config import ROOT
from copilot.logic.intent import UserProfile
from copilot.logic.ranking import RankedIssue
from copilot.models import Dropped, StepInfo

logger = logging.getLogger(__name__)

DEFAULT_PATH = ROOT / ".state" / "sessions.json"

# idle -> presented -> awaiting_approval -> executing -> done | partial (some step failed, `retry` repeats it)
# then, after the user pushed their fix: done -> pr_review (branch checked) -> executing -> pr_done
STAGES = ("idle", "presented", "awaiting_approval", "executing", "partial", "done", "pr_review", "pr_done")


class Session(BaseModel):
    profile: UserProfile = Field(default_factory=UserProfile)
    stage: str = "idle"
    ranked: list[RankedIssue] = Field(default_factory=list)
    dropped: list[Dropped] = Field(default_factory=list)
    considered: int = 0
    picked: int | None = None  # 1-based index into `ranked`
    comment: str = ""
    branch: str = ""
    approval_id: str = ""  # set only when the user types approve
    fork_full_name: str = ""
    steps: list[StepInfo] = Field(default_factory=list)
    dry_run: bool = False
    plan_path: str = ""
    pr_title: str = ""  # the pull request being reviewed or opened
    pr_note: str = ""  # the user's own description ("edit pr: ..."), empty = use the repo's template
    pr_check: str = ""  # what the read-only branch check found, shown in the review
    pr_url: str = ""

    def picked_issue(self) -> RankedIssue | None:
        return self.ranked[self.picked - 1] if self.picked and 1 <= self.picked <= len(self.ranked) else None


class _File(BaseModel):
    sessions: dict[str, Session] = Field(default_factory=dict)


class SessionStore:
    def __init__(self, path: Path | None = DEFAULT_PATH):
        self._path = path
        self._data = _File()
        if path and path.exists():
            try:
                self._data = _File.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning("could not read %s, starting with empty sessions", path)

    def get(self, user: str) -> Session:
        return self._data.sessions.setdefault(user, Session())

    def save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(self._data.model_dump_json(), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            logger.warning("could not persist sessions to %s", self._path)
