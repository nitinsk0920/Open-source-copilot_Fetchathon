"""Live verification of one candidate issue. The checks are pure functions over GitHub JSON.

Thresholds are deliberately lenient (the user wants small beginner-friendly repos to survive).
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from copilot.models import Issue, Verification
from copilot.tools.http import ApiError, RateLimitError

logger = logging.getLogger(__name__)

MAX_REPO_STALE_DAYS = 180  # no push for this long -> the project looks dead
CLAIM_ACTIVE_DAYS = 60  # a claim comment newer than this is treated as an active claim

_CLAIM_PATTERNS = [re.compile(p, re.I) for p in (
    r"\b(?:can|could|may|might) i (?:please )?(?:work on|take|pick (?:this|it|that)?\s*up|try|tackle|have|be assigned|get assigned|contribute)",
    r"\bi(?:'d| would| 'd) (?:like|love|want) to (?:work on|take|pick up|try|tackle|contribute|fix|solve|do)",
    r"\bi(?:'ll| will| can| could) (?:take|work on|pick up|tackle|handle|fix|submit|open a pr|do this|give it a try)",
    r"\bi(?:'m| am) (?:currently |already )?(?:working on|taking|on it|going to (?:work|take|fix))",
    r"\bplease (?:assign|allocate)\b|\bassign(?:ed)? (?:this )?(?:issue )?(?:to )?me\b|\bcan you assign\b",
    r"\b(?:working on this|taking this|take this|claiming this|i claim|dibs)\b",
)]
_CLA = re.compile(r"\bCLA\b|contributor licen[sc]e agreement", re.I)
_DCO = re.compile(r"\bDCO\b|signed-off-by|sign[- ]?off", re.I)
_ASK_FIRST = re.compile(r"(?:open|file|create) an? issue (?:first|before)|discuss .{0,40}before|comment .{0,30}(?:before|to claim)|ask .{0,30}before", re.I)
_TESTS = re.compile(r"\b(?:run|running) (?:the )?tests?\b|\btests? (?:must|should) pass\b|\bpytest\b|\bnpm test\b", re.I)


def _days_since(iso: str | None, now: datetime) -> int | None:
    if not iso:
        return None
    try:
        return max((now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days, 0)
    except ValueError:
        return None


def ago(days: int | None) -> str:
    if days is None:
        return "recently"
    return "today" if days <= 0 else "1 day ago" if days == 1 else f"{days} days ago"


def check_repo(repo: dict, now: datetime) -> tuple[str | None, int | None]:
    """(drop reason or None, days since last push)."""
    days = _days_since(repo.get("pushed_at"), now)
    if repo.get("archived"):
        return "repository is archived (read-only)", days
    if repo.get("disabled"):
        return "repository is disabled", days
    if repo.get("has_issues") is False:
        return "repository has issues turned off", days
    if days is not None and days > MAX_REPO_STALE_DAYS:
        return f"repository looks inactive (last push {days} days ago)", days
    return None, days


def check_issue(issue: dict) -> str | None:
    if issue.get("state") != "open":
        return "issue is already closed"
    if issue.get("locked"):
        return "issue is locked (comments are disabled)"
    assignees = [a["login"] for a in issue.get("assignees") or [] if a.get("login")]
    if assignees:
        return f"already assigned to @{assignees[0]}"
    if issue.get("pull_request"):
        return "this is a pull request, not an issue"
    return None


def find_claim(comments: list[dict], now: datetime) -> tuple[str, int] | None:
    """Most recent comment that looks like 'I'll take this' by a human: (login, days ago)."""
    for c in sorted(comments, key=lambda c: c.get("created_at", ""), reverse=True):
        user = c.get("user") or {}
        if user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]"):
            continue
        body = c.get("body") or ""
        if any(p.search(body) for p in _CLAIM_PATTERNS):
            return user.get("login", "someone"), _days_since(c.get("created_at"), now) or 0
    return None


def find_open_pr(timeline: list[dict]) -> int | None:
    """Number of an open PR that references this issue, from cross-reference events."""
    for event in timeline:
        if event.get("event") != "cross-referenced":
            continue
        src = (event.get("source") or {}).get("issue") or {}
        if "pull_request" in src and src.get("state") == "open":
            return src.get("number")
    return None


_PLACEHOLDER = r"<[^<>`\n]+>|\{[^{}`\n]+\}"
_BRANCH_RULE = re.compile(r"branch[^`\n]{0,60}`([^`\n]+)`", re.I)


def parse_branch_pattern(text: str) -> str:
    """A branch naming rule with an issue-number placeholder, e.g. 'create a branch named `fix/issue-<number>`'.

    Returns the pattern as written ("fix/issue-<number>"), or "" if the repo states no such rule.
    """
    for m in _BRANCH_RULE.finditer(text):
        pattern = m.group(1).strip()
        if re.search(_PLACEHOLDER, pattern) and re.search(r"number|issue|id|num|\bn\b", pattern, re.I):
            return pattern
    return ""


def contributing_notes(text: str) -> list[str]:
    notes = []
    if _CLA.search(text):
        notes.append("Requires signing a Contributor License Agreement (CLA)")
    if _DCO.search(text):
        notes.append("Commits must be signed off (use `git commit -s`)")
    if _ASK_FIRST.search(text):
        notes.append("Asks you to discuss or claim an issue before starting")
    if _TESTS.search(text):
        notes.append("Run the tests before opening a PR")
    return notes


def pick_contributing(paths: list[str]) -> str:
    """The contributing guide among repo files. Prefers CONTRIBUTING.md over translated copies (CONTRIBUTING.es.md)."""
    guides = [p for p in paths if p.rsplit("/", 1)[-1].lower().startswith("contributing")]
    plain = [p for p in guides if re.fullmatch(r"contributing(\.(md|rst|txt|markdown))?", p.rsplit("/", 1)[-1].lower())]
    return (plain or guides or [""])[0]


def _names(entries: list[dict]) -> list[str]:
    return [e["name"] for e in entries if isinstance(e, dict) and "name" in e]


@dataclass
class Outcome:
    verification: Verification | None = None
    drop_reason: str | None = None  # set when the issue must be dropped
    unchecked: bool = False  # True if a rate limit stopped us before we could decide


async def verify_issue(gh, issue: Issue, now: datetime | None = None) -> Outcome:
    """Run every live check for one issue. `gh` is a GitHubClient (duck-typed for tests)."""
    now = now or datetime.now(timezone.utc)
    try:
        repo_json, issue_json = await asyncio.gather(gh.get_repo(issue.repo), gh.get_issue(issue.repo, issue.number))
        reason, pushed_days = check_repo(repo_json, now)
        reason = reason or check_issue(issue_json)
        if reason:
            return Outcome(drop_reason=reason)

        comments, timeline, root = await asyncio.gather(
            gh.get_issue_comments(issue.repo, issue.number),
            gh.get_issue_timeline(issue.repo, issue.number),
            gh.list_dir(issue.repo),
        )
        notes: list[str] = []
        claim = find_claim(comments, now)
        if claim and claim[1] <= CLAIM_ACTIVE_DAYS:
            return Outcome(drop_reason=f"already claimed: @{claim[0]} asked to work on it {ago(claim[1])}")
        if claim:
            notes.append(f"@{claim[0]} asked to work on it {ago(claim[1])}; it may have been abandoned")
        pr = find_open_pr(timeline)
        if pr:
            return Outcome(drop_reason=f"open pull request #{pr} already references this issue")

        root_names = _names(root)
        dot_github = _names(await gh.list_dir(issue.repo, ".github")) if ".github" in root_names else []
        contributing = pick_contributing([*root_names, *(f".github/{n}" for n in dot_github)])
        template = next(
            (p for p in [*root_names, *(f".github/{n}" for n in dot_github)]
             if p.rsplit("/", 1)[-1].lower().startswith("pull_request_template")), "")
        template_text = (await gh.get_file_text(issue.repo, template) or "")[:1500] if template else ""
        guide_notes: list[str] = []
        branch_pattern = ""
        if contributing:
            guide = await gh.get_file_text(issue.repo, contributing) or ""
            guide_notes, branch_pattern = contributing_notes(guide), parse_branch_pattern(guide)
            if branch_pattern:
                guide_notes.append(f"Branches must be named `{branch_pattern}`")

        checks = [f"repo active (last push {ago(pushed_days)})" if pushed_days is not None else "repo active",
                  "issue open and unassigned",
                  "no claim comments" if not claim else "only an old claim comment",
                  "no open PR referencing it"]
        checks.append(f"{contributing} found" if contributing else "no CONTRIBUTING file")
        return Outcome(verification=Verification(
            checks=checks, stars=repo_json.get("stargazers_count", 0), pushed_days_ago=pushed_days,
            default_branch=repo_json.get("default_branch", "main"), root_files=root_names[:60],
            has_contributing=bool(contributing), contributing_path=contributing, contributing_notes=guide_notes,
            branch_pattern=branch_pattern, pr_template=template, pr_template_text=template_text, notes=notes,
        ))
    except RateLimitError:
        return Outcome(unchecked=True)
    except ApiError as exc:
        if exc.status in (404, 410):
            return Outcome(drop_reason="repository or issue no longer exists (or is private)")
        logger.warning("could not verify %s#%s: %s", issue.repo, issue.number, exc)
        return Outcome(drop_reason=f"could not be verified right now ({exc.message[:80]})")
