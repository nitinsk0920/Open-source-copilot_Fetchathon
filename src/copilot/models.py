"""Message models exchanged between agents."""

from pydantic import BaseModel
from uagents import Model


class IssueCandidate(Model):
    repo: str  # "owner/name"
    number: int
    title: str
    url: str
    labels: list[str] = []
    comments: int = 0
    created_at: str = ""
    updated_at: str = ""
    language: str = ""  # language the search was run for
    body_excerpt: str = ""


class Issue(BaseModel):
    """Domain copy of IssueCandidate. uagents' Model is pydantic-v1 style and can't be nested in v2 models
    (ranking, sessions), so results are converted once, where they arrive from Discovery."""

    repo: str
    number: int
    title: str
    url: str
    labels: list[str] = []
    comments: int = 0
    created_at: str = ""
    updated_at: str = ""
    language: str = ""
    body_excerpt: str = ""

    @classmethod
    def from_candidate(cls, c: IssueCandidate) -> "Issue":
        return cls(**c.dict())


class Verification(BaseModel):
    """What the Analyst checked live for one issue (v2: nested in RankedIssue and persisted in sessions)."""

    checks: list[str] = []  # human-readable passed checks, shown to the user
    stars: int = 0
    pushed_days_ago: int | None = None
    default_branch: str = "main"
    root_files: list[str] = []
    has_contributing: bool = False
    contributing_path: str = ""
    contributing_notes: list[str] = []
    branch_pattern: str = ""  # branch naming rule from CONTRIBUTING, e.g. "fix/issue-<number>"
    pr_template: str = ""  # path, empty if none
    pr_template_text: str = ""  # first part of the template, used to prefill the pull request description
    notes: list[str] = []  # soft warnings (e.g. an old claim comment)


class Dropped(BaseModel):
    repo: str
    number: int
    title: str
    url: str = ""
    reason: str
    stage: str  # "language" | "verification"


class StepInfo(BaseModel):
    name: str  # "fork" | "comment" | "branch"
    status: str  # "ok" | "failed" | "skipped" | "simulated"
    detail: str = ""
    url: str = ""


class SearchRequest(Model):
    request_id: str
    languages: list[str]
    labels: list[str] = []  # empty -> default beginner labels
    max_results: int = 20
    recency_days: int = 90
    widen: bool = False  # recovery pass: more labels, longer recency window


class AnalyzeRequest(Model):
    request_id: str
    languages: list[str]
    level: str = "unknown"
    time_budget_hours: float = 0.0  # 0 = unknown
    interests: list[str] = []
    candidates: list[IssueCandidate]


class AnalyzeResponse(Model):
    """ranked_json / dropped_json hold the v2 domain models (RankedIssue / Dropped lists) as JSON."""

    request_id: str
    ok: bool
    ranked_json: str = "[]"
    dropped_json: str = "[]"
    considered: int = 0  # candidates received
    verified: int = 0  # candidates that passed every live check
    ai_ranked: bool = False
    warnings: list[str] = []
    error: str = ""


class StepResult(Model):
    name: str
    status: str
    detail: str = ""
    url: str = ""


class ActionRequest(Model):
    request_id: str
    approval_id: str  # minted by the Orchestrator only after the user's explicit approval
    repo: str
    issue_number: int
    default_branch: str = "main"
    comment_text: str
    branch_name: str
    # fork / comment / branch, then later pr_check (read-only) and pr. A retry sends only the failed ones.
    steps: list[str] = ["fork", "comment", "branch"]
    fork_full_name: str = ""  # known from an earlier attempt
    user_id: str = ""
    pr_title: str = ""  # for the "pr" step
    pr_body: str = ""


class ActionResponse(Model):
    request_id: str
    ok: bool
    dry_run: bool = False
    steps: list[StepResult] = []
    fork_full_name: str = ""
    error: str = ""



class SearchResponse(Model):
    request_id: str
    ok: bool
    candidates: list[IssueCandidate] = []
    total_found: int = 0  # candidates before the per-repo cap / truncation
    queries: list[str] = []
    warnings: list[str] = []
    error: str = ""
