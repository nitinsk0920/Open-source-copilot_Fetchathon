from datetime import datetime, timedelta, timezone

from copilot.logic.verification import (check_issue, check_repo, contributing_notes, find_claim, find_open_pr,
                                        verify_issue)
from copilot.models import Issue
from copilot.tools.http import ApiError, RateLimitError

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def comment(body: str, days_ago: int = 1, login: str = "bob", user_type: str = "User") -> dict:
    return {"body": body, "created_at": (NOW - timedelta(days=days_ago)).isoformat(),
            "user": {"login": login, "type": user_type}}


def test_repo_checks():
    assert check_repo({"archived": True}, NOW)[0] == "repository is archived (read-only)"
    assert "inactive" in check_repo({"pushed_at": "2025-01-01T00:00:00Z"}, NOW)[0]
    assert check_repo({"has_issues": False}, NOW)[0] == "repository has issues turned off"
    reason, days = check_repo({"pushed_at": "2026-10-20T00:00:00Z", "stargazers_count": 0}, NOW)
    assert reason is None and days == 4  # no star threshold: small repos survive


def test_issue_checks():
    assert check_issue({"state": "open", "assignees": []}) is None
    assert check_issue({"state": "closed"}) == "issue is already closed"
    assert check_issue({"state": "open", "locked": True}).startswith("issue is locked")
    assert check_issue({"state": "open", "assignees": [{"login": "amy"}]}) == "already assigned to @amy"


def test_claim_detection():
    for body in ["Can I work on this?", "I'd like to take this one", "I'll take it", "assign me please",
                 "I am working on this", "Hi, could I pick this up?", "Please assign this to me"]:
        assert find_claim([comment(body)], NOW) is not None, body
    for body in ["Thanks for reporting!", "This is caused by the parser.", "I can reproduce this on Windows",
                 "You can't work around it", "see #12"]:
        assert find_claim([comment(body)], NOW) is None, body


def test_claim_ignores_bots_and_reports_latest_claimer():
    assert find_claim([comment("I'll take this", user_type="Bot")], NOW) is None
    assert find_claim([comment("I'll take this", login="stale[bot]")], NOW) is None
    login, days = find_claim([comment("can I work on this", 5, "old"), comment("I'm on it", 2, "new")], NOW)
    assert (login, days) == ("new", 2)


def test_open_pr_detection():
    open_pr = {"event": "cross-referenced", "source": {"issue": {"number": 9, "state": "open", "pull_request": {}}}}
    closed_pr = {"event": "cross-referenced", "source": {"issue": {"number": 8, "state": "closed", "pull_request": {}}}}
    mention = {"event": "cross-referenced", "source": {"issue": {"number": 7, "state": "open"}}}
    assert find_open_pr([closed_pr, mention, {"event": "labeled"}]) is None
    assert find_open_pr([closed_pr, open_pr]) == 9


def test_contributing_notes():
    notes = contributing_notes("Sign the CLA. All commits need a Signed-off-by line. Please open an issue first. Run the tests.")
    assert len(notes) == 4
    assert contributing_notes("Be nice.") == []


class FakeGH:
    def __init__(self, repo=None, issue=None, comments=None, timeline=None, root=None, error=None, files=None):
        self.repo = {"pushed_at": "2026-10-20T00:00:00Z", "stargazers_count": 3, "default_branch": "main", **(repo or {})}
        self.issue = {"state": "open", "assignees": [], **(issue or {})}
        self.comments, self.timeline, self.error = comments or [], timeline or [], error
        self.root = root if root is not None else [{"name": "README.md"}, {"name": "CONTRIBUTING.md"}, {"name": "package.json"}]
        self.files = files if files is not None else {"CONTRIBUTING.md": "Please sign off your commits (DCO)."}

    async def get_repo(self, repo):
        if self.error:
            raise self.error
        return self.repo

    async def get_issue(self, repo, n):
        return self.issue

    async def get_issue_comments(self, repo, n):
        return self.comments

    async def get_issue_timeline(self, repo, n):
        return self.timeline

    async def list_dir(self, repo, path=""):
        return self.root if path == "" else []

    async def get_file_text(self, repo, path):
        return self.files.get(path)


ISSUE = Issue(repo="a/b", number=1, title="t", url="u")


async def test_verify_ok_collects_repo_facts():
    out = await verify_issue(FakeGH(), ISSUE, NOW)
    v = out.verification
    assert out.drop_reason is None and v.stars == 3 and v.default_branch == "main"
    assert v.has_contributing and v.contributing_path == "CONTRIBUTING.md"
    assert any("signed off" in n for n in v.contributing_notes) and "package.json" in v.root_files
    assert "no claim comments" in v.checks


async def test_verify_drops_with_reasons():
    claimed = await verify_issue(FakeGH(comments=[comment("Can I work on this?", 2, "bob")]), ISSUE, NOW)
    assert claimed.drop_reason == "already claimed: @bob asked to work on it 2 days ago"
    same_day = await verify_issue(FakeGH(comments=[comment("I'm on it", 0, "amy")]), ISSUE, NOW)
    assert same_day.drop_reason == "already claimed: @amy asked to work on it today"
    pr = {"event": "cross-referenced", "source": {"issue": {"number": 9, "state": "open", "pull_request": {}}}}
    assert "open pull request #9" in (await verify_issue(FakeGH(timeline=[pr]), ISSUE, NOW)).drop_reason
    assert "archived" in (await verify_issue(FakeGH(repo={"archived": True}), ISSUE, NOW)).drop_reason
    assert "closed" in (await verify_issue(FakeGH(issue={"state": "closed"}), ISSUE, NOW)).drop_reason


async def test_old_claim_is_kept_with_a_heads_up():
    out = await verify_issue(FakeGH(comments=[comment("I'll take this", 100, "bob")]), ISSUE, NOW)
    assert out.drop_reason is None and "may have been abandoned" in out.verification.notes[0]


async def test_verify_handles_errors_without_crashing():
    gone = await verify_issue(FakeGH(error=ApiError(404, "Not Found")), ISSUE, NOW)
    assert "no longer exists" in gone.drop_reason
    flaky = await verify_issue(FakeGH(error=ApiError(502, "Bad Gateway")), ISSUE, NOW)
    assert "could not be verified" in flaky.drop_reason
    limited = await verify_issue(FakeGH(error=RateLimitError(403, "slow", 60)), ISSUE, NOW)
    assert limited.unchecked and limited.drop_reason is None


async def test_pr_template_and_dot_github_contributing():
    class GH(FakeGH):
        async def list_dir(self, repo, path=""):
            return [{"name": ".github"}] if path == "" else [{"name": "CONTRIBUTING.md"}, {"name": "PULL_REQUEST_TEMPLATE.md"}]

    out = await verify_issue(GH(files={".github/CONTRIBUTING.md": "Sign the CLA"}), ISSUE, NOW)
    assert out.verification.contributing_path == ".github/CONTRIBUTING.md"
    assert out.verification.pr_template == ".github/PULL_REQUEST_TEMPLATE.md"
    assert out.verification.contributing_notes == ["Requires signing a Contributor License Agreement (CLA)"]
