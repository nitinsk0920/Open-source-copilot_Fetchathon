import json

import httpx
import pytest
import respx

from copilot.agents.action import execute, validate
from copilot.cards import pr_review_message, pr_review_payload
from copilot.formatting import format_pr_review
from copilot.logic.commands import parse_command
from copilot.logic.ranking import RankedIssue
from copilot.logic.review import pr_body, pr_ref, pr_title
from copilot.logic.verification import verify_issue
from copilot.models import ActionRequest, Issue, Verification
from copilot.tools.github_client import API_URL, GitHubClient
from tests.conftest import no_sleep
from tests.test_verification import NOW, FakeGH

REPO, FORK = "owner/proj", "demo/proj"
BRANCH = "fix/issue-1"
HEAD = f"demo:{BRANCH}"


def req(steps, **kw) -> ActionRequest:
    base = dict(request_id="r1", approval_id="appr1", repo=REPO, issue_number=1, default_branch="main",
                comment_text="c", branch_name=BRANCH, steps=steps, fork_full_name=FORK,
                pr_title="Add input validation", pr_body="Fixes #1")
    return ActionRequest(**{**base, **kw})


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("ALLOWED_TARGET_REPOS", REPO)


def gh() -> GitHubClient:
    return GitHubClient(sleep=no_sleep, retries=1)


def issue(n: int = 1) -> Issue:
    return Issue(repo=REPO, number=n, title="Add input validation to divide() in calc.py", url="https://x/1")


# ---- Action: pr_check (read-only) ----

@respx.mock
async def test_pr_check_reports_commits_and_files_without_needing_approval(live):
    respx.get(f"{API_URL}/repos/{REPO}/pulls").mock(return_value=httpx.Response(200, json=[]))
    respx.get(url__regex=r".*/compare/.*").mock(return_value=httpx.Response(200, json={
        "ahead_by": 2, "files": [{"filename": "calc.py"}, {"filename": "test_calc.py"}]}))
    resp = await execute(gh(), req(["pr_check"], approval_id=""))
    step = resp.steps[0]
    assert resp.ok and step.status == "ok" and "2 commit(s) ahead of `main`" in step.detail
    assert "calc.py, test_calc.py" in step.detail
    assert all(c.request.method == "GET" for c in respx.calls)  # nothing written


@respx.mock
async def test_pr_check_fails_clearly_when_the_branch_has_no_commits_yet(live):
    respx.get(f"{API_URL}/repos/{REPO}/pulls").mock(return_value=httpx.Response(200, json=[]))
    respx.get(url__regex=r".*/compare/.*").mock(return_value=httpx.Response(200, json={"ahead_by": 0, "files": []}))
    resp = await execute(gh(), req(["pr_check"], approval_id=""))
    assert not resp.ok and "no commits of its own yet" in resp.steps[0].detail


@respx.mock
async def test_pr_check_branch_missing_and_no_fork(live):
    respx.get(f"{API_URL}/repos/{REPO}/pulls").mock(return_value=httpx.Response(200, json=[]))
    respx.get(url__regex=r".*/compare/.*").mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    assert "can't find branch" in (await execute(gh(), req(["pr_check"], approval_id=""))).steps[0].detail
    no_fork = await execute(gh(), req(["pr_check"], approval_id="", fork_full_name=""))
    assert not no_fork.ok and "no fork is known" in no_fork.steps[0].detail


@respx.mock
async def test_pr_check_sees_an_already_open_pr(live):
    respx.get(f"{API_URL}/repos/{REPO}/pulls").mock(return_value=httpx.Response(
        200, json=[{"number": 7, "html_url": "https://github.com/owner/proj/pull/7"}]))
    resp = await execute(gh(), req(["pr_check"], approval_id=""))
    assert resp.ok and resp.steps[0].status == "exists" and resp.steps[0].url.endswith("/pull/7")


# ---- Action: pr ----

@respx.mock
async def test_pr_is_opened_from_the_fork_branch_with_the_approved_text(live):
    respx.get(f"{API_URL}/repos/{REPO}/pulls").mock(return_value=httpx.Response(200, json=[]))
    create = respx.post(f"{API_URL}/repos/{REPO}/pulls").mock(return_value=httpx.Response(
        201, json={"number": 9, "html_url": "https://github.com/owner/proj/pull/9"}))
    resp = await execute(gh(), req(["pr"]))
    assert resp.ok and resp.steps[0].status == "ok" and resp.steps[0].url.endswith("/pull/9")
    sent = json.loads(create.calls.last.request.content)
    assert sent == {"title": "Add input validation", "head": HEAD, "base": "main", "body": "Fixes #1"}


@respx.mock
async def test_pr_without_approval_is_refused_and_nothing_is_sent(live):
    resp = await execute(gh(), req(["pr"], approval_id=""))
    assert not resp.ok and not resp.steps and "approval" in resp.error and len(respx.calls) == 0


@respx.mock
async def test_pr_is_never_opened_twice(live):
    respx.get(f"{API_URL}/repos/{REPO}/pulls").mock(return_value=httpx.Response(
        200, json=[{"number": 7, "html_url": "https://github.com/owner/proj/pull/7"}]))
    create = respx.post(f"{API_URL}/repos/{REPO}/pulls").mock(return_value=httpx.Response(201, json={}))
    resp = await execute(gh(), req(["pr"]))
    assert resp.ok and resp.steps[0].status == "exists" and create.call_count == 0


@respx.mock
async def test_pr_with_no_commits_gets_a_friendly_failure(live):
    respx.get(f"{API_URL}/repos/{REPO}/pulls").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API_URL}/repos/{REPO}/pulls").mock(return_value=httpx.Response(
        422, json={"message": "Validation Failed: No commits between main and demo:fix/issue-1"}))
    resp = await execute(gh(), req(["pr"]))
    assert not resp.ok and "no commits of its own yet" in resp.steps[0].detail


@respx.mock
async def test_pr_is_a_dry_run_on_non_allowlisted_repos():
    resp = await execute(gh(), req(["pr_check", "pr"]))  # ALLOWED_TARGET_REPOS is empty
    assert resp.ok and resp.dry_run and [s.status for s in resp.steps] == ["simulated", "simulated"]
    assert "would open the pull request" in resp.steps[1].detail and len(respx.calls) == 0


def test_pr_request_validation():
    assert validate(req(["pr"], pr_title="  ")) is not None
    assert validate(req(["pr"], pr_body="x" * 7000)) is not None
    assert validate(req(["pr"], fork_full_name="not a fork")) == "invalid fork name"
    assert validate(req(["pr_check"], approval_id="")) is None  # read-only: no approval needed
    assert validate(req(["pr"], approval_id="")) is not None


# ---- title and description ----

def test_pr_description_prefers_the_users_text_then_the_repo_template():
    template = Verification(pr_template_text="## What does this change?\n\n## How did you test it?\n\nFixes #\n")
    filled = pr_body(issue(1), template)
    assert filled.rstrip().endswith("Fixes #1") and "## What does this change?" in filled
    assert pr_body(issue(1), Verification(pr_template_text="## Summary\n\nTests added.")).endswith("\n\nFixes #1")
    assert pr_body(issue(1), None) == "Fixes #1\n\nResolves: https://x/1"
    assert pr_body(issue(1), template, note="Adds a ValueError for zero.") == "Adds a ValueError for zero.\n\nFixes #1"
    assert pr_title(issue()) == "Add input validation to divide() in calc.py"


def test_pr_ref_changes_with_the_text_so_an_old_card_cannot_open_a_different_pr():
    a = pr_ref(issue(), "title", "body", HEAD)
    assert a == pr_ref(issue(), "title", "body", HEAD)
    assert a != pr_ref(issue(), "title", "edited body", HEAD) and a != pr_ref(issue(), "other", "body", HEAD)
    assert a != pr_ref(issue(), "title", "body", "demo:other-branch")


async def test_verification_keeps_the_pr_template_text():
    gh_ = FakeGH(root=[{"name": "PULL_REQUEST_TEMPLATE.md"}], files={"PULL_REQUEST_TEMPLATE.md": "## Summary\n\nFixes #\n"})
    v = (await verify_issue(gh_, Issue(repo="a/b", number=1, title="t", url="u"), NOW)).verification
    assert v.pr_template == "PULL_REQUEST_TEMPLATE.md" and "Fixes #" in v.pr_template_text


# ---- commands, card, review text ----

def test_open_pr_commands_match_whole_messages_only():
    for text in ["open pr", "Open PR", "open a pull request", "create the PR", "send my pr", "submit pull request."]:
        assert parse_command(text).kind == "open_pr", text
    for text in ["I want to open a pr for my project", "find me an issue, then open pr", "open pr please for python"]:
        assert parse_command(text).kind == "text", text
    assert parse_command('{"action": "open_pr"}').kind == "open_pr"
    e = parse_command("edit pr: Adds a clear error for zero.\nTested with pytest.")
    assert e.kind == "edit_pr" and e.text == "Adds a clear error for zero.\nTested with pytest."
    assert parse_command("edit comment: hi").kind == "edit"  # the two edits do not collide


def test_pr_review_card_matches_the_documented_shape_and_carries_a_matching_ref():
    r = RankedIssue(candidate=issue())
    p = pr_review_payload(r, "T", "Fixes #1", HEAD, "main", live=True, check="1 commit ahead")
    assert set(p) == {"title", "summary_rows", "approve_cta", "reject_cta"}
    assert all(set(row) == {"label", "value"} for row in p["summary_rows"])
    assert p["approve_cta"]["selection"]["ref"] == pr_ref(r.candidate, "T", "Fixes #1", HEAD)
    assert p["approve_cta"]["selection"]["action"] == "approve" and p["reject_cta"]["selection"]["action"] == "reject"
    msg = pr_review_message("n", r, "T", "Fixes #1", HEAD, "main", False, "(dry)")
    assert msg.content[1].metadata["card_kind"] == "review"
    assert "DRY RUN" in next(row["value"] for row in json.loads(msg.content[1].metadata["card_payload"])["summary_rows"]
                             if row["label"] == "Mode")


def test_pr_review_text_shows_everything_that_will_be_opened():
    r = RankedIssue(candidate=issue())
    text = format_pr_review(r, "Add input validation", "## Summary\n\nFixes #1", HEAD, "main", True, "1 commit ahead")
    for expected in ["Nothing has been opened yet", "LIVE", "Your changes: 1 commit ahead", f"From: `{HEAD}`",
                     "Into: `main` of owner/proj", "Title: Add input validation", "> Fixes #1", '"approve"', '"reject"']:
        assert expected in text, expected
    assert "DRY RUN" in format_pr_review(r, "t", "b", HEAD, "main", False, "")
