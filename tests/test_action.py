import httpx
import pytest
import respx

from copilot.agents.action import describe_error, execute, validate
from copilot.logic.review import approval_marker
from copilot.models import ActionRequest
from copilot.tools.github_client import API_URL, GitHubClient
from copilot.tools.http import ApiError, RateLimitError
from tests.conftest import no_sleep

REPO, FORK = "owner/proj", "demo/proj"


def req(**kw) -> ActionRequest:
    base = dict(request_id="r1", approval_id="appr1", repo=REPO, issue_number=7, default_branch="main",
                comment_text="Hi! I'd like to work on this.", branch_name="fix-issue-7-typo")
    return ActionRequest(**{**base, **kw})


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("ALLOWED_TARGET_REPOS", REPO)


def gh() -> GitHubClient:
    return GitHubClient(sleep=no_sleep, retries=1)


def mock_happy(comments=None):
    respx.post(f"{API_URL}/repos/{REPO}/forks").mock(
        return_value=httpx.Response(202, json={"full_name": FORK, "html_url": f"https://github.com/{FORK}"}))
    respx.get(f"{API_URL}/repos/{REPO}/issues/7/comments").mock(return_value=httpx.Response(200, json=comments or []))
    comment = respx.post(f"{API_URL}/repos/{REPO}/issues/7/comments").mock(
        return_value=httpx.Response(201, json={"html_url": f"https://github.com/{REPO}/issues/7#c1"}))
    respx.get(f"{API_URL}/repos/{FORK}/git/ref/heads/main").mock(
        return_value=httpx.Response(200, json={"object": {"sha": "abc123"}}))
    ref = respx.post(f"{API_URL}/repos/{FORK}/git/refs").mock(return_value=httpx.Response(201, json={}))
    return comment, ref


def test_validation_blocks_unsafe_requests():
    assert "approval" in validate(req(approval_id=""))
    assert validate(req(repo="not a repo")) == "invalid repository or issue number"
    assert validate(req(branch_name="../evil")) == "invalid branch name"
    assert validate(req(branch_name="bad name")) == "invalid branch name"
    assert validate(req(comment_text="  ")) == "the comment is empty or too long"
    assert validate(req()) is None


@respx.mock
async def test_dry_run_when_repo_not_allowlisted_makes_no_calls():
    resp = await execute(gh(), req())  # ALLOWED_TARGET_REPOS is empty
    assert resp.ok and resp.dry_run and [s.status for s in resp.steps] == ["simulated"] * 3
    assert len(respx.calls) == 0


@respx.mock
async def test_no_approval_means_no_calls_even_if_allowlisted(live):
    resp = await execute(gh(), req(approval_id=""))
    assert not resp.ok and not resp.steps and len(respx.calls) == 0


@respx.mock
async def test_live_run_does_all_three_steps(live):
    comment, ref = mock_happy()
    resp = await execute(gh(), req())
    assert resp.ok and not resp.dry_run and resp.fork_full_name == FORK
    assert [(s.name, s.status) for s in resp.steps] == [("fork", "ok"), ("comment", "ok"), ("branch", "ok")]
    body = comment.calls.last.request.content.decode()
    assert "Hi! I" in body and approval_marker("appr1") in body
    assert b"refs/heads/fix-issue-7-typo" in ref.calls.last.request.content and b"abc123" in ref.calls.last.request.content


@respx.mock
async def test_comment_failure_is_reported_and_other_steps_still_run(live):
    mock_happy()
    respx.post(f"{API_URL}/repos/{REPO}/issues/7/comments").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible by personal access token"}))
    resp = await execute(gh(), req())
    status = {s.name: s.status for s in resp.steps}
    assert not resp.ok and status == {"fork": "ok", "comment": "failed", "branch": "ok"}
    assert "comment" in resp.error and "lacks permission" in next(s for s in resp.steps if s.name == "comment").detail


@respx.mock
async def test_retry_runs_only_the_requested_step_and_reuses_the_fork(live):
    comment, ref = mock_happy()
    resp = await execute(gh(), req(steps=["comment"], fork_full_name=FORK))
    assert resp.ok and [s.name for s in resp.steps] == ["comment"]
    assert comment.call_count == 1 and ref.call_count == 0
    assert not any(c.request.url.path.endswith("/forks") for c in respx.calls)


@respx.mock
async def test_comment_is_not_posted_twice_when_marker_exists(live):
    comment, _ = mock_happy(comments=[{"body": f"Hi\n\n{approval_marker('appr1')}", "html_url": "https://x/c9"}])
    resp = await execute(gh(), req(steps=["comment"]))
    assert resp.ok and resp.steps[0].detail == "comment was already posted" and comment.call_count == 0


@respx.mock
async def test_branch_already_existing_counts_as_success(live):
    mock_happy()
    respx.post(f"{API_URL}/repos/{FORK}/git/refs").mock(
        return_value=httpx.Response(422, json={"message": "Reference already exists"}))
    resp = await execute(gh(), req(steps=["branch"], fork_full_name=FORK))
    assert resp.ok and "already existed" in resp.steps[0].detail


@respx.mock
async def test_fork_failure_skips_branch(live):
    mock_happy()
    respx.post(f"{API_URL}/repos/{REPO}/forks").mock(return_value=httpx.Response(401, json={"message": "Bad credentials"}))
    resp = await execute(gh(), req())
    status = {s.name: s.status for s in resp.steps}
    assert status == {"fork": "failed", "comment": "ok", "branch": "skipped"} and not resp.ok


@respx.mock
async def test_branch_without_known_fork_is_skipped(live):
    resp = await execute(gh(), req(steps=["branch"]))
    assert resp.steps[0].status == "skipped" and not resp.ok


@respx.mock
async def test_fork_never_ready_times_out_cleanly():
    respx.get(f"{API_URL}/repos/{FORK}/git/ref/heads/main").mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(ApiError) as exc:
        await GitHubClient(sleep=no_sleep).wait_for_fork(FORK, "main", timeout=0.05, interval=0.01)
    assert "not ready" in exc.value.message


def test_error_descriptions_are_friendly():
    assert "token" in describe_error(ApiError(401, "Bad credentials"))
    assert "rate-limiting" in describe_error(RateLimitError(403, "x", 60))
    assert "public_repo" in describe_error(ApiError(404, "Not Found"))
    assert "unreachable" in describe_error(ApiError(None, "network error (ConnectTimeout)"))
