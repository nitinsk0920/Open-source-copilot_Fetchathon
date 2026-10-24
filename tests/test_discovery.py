import httpx
import respx

from copilot.agents.discovery import handle_search
from copilot.models import SearchRequest
from copilot.tools.github_client import API_URL, GitHubClient
from tests.conftest import no_sleep


def item(repo: str, number: int) -> dict:
    return {"number": number, "title": "t", "html_url": f"https://github.com/{repo}/issues/{number}",
            "repository_url": f"{API_URL}/repos/{repo}", "labels": [], "comments": 0}


@respx.mock
async def test_partial_failure_returns_warning_not_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if "language:go" in request.url.params["q"]:
            return httpx.Response(422, json={"message": "Validation Failed"})
        return httpx.Response(200, json={"total_count": 1, "items": [item("a/x", 1)]})

    respx.get(f"{API_URL}/search/issues").mock(side_effect=handler)
    client = GitHubClient(sleep=no_sleep)
    resp = await handle_search(client, SearchRequest(request_id="r1", languages=["python", "go"]))
    assert resp.ok and len(resp.candidates) == 1
    assert len(resp.queries) == 2 and "go failed" in resp.warnings[0]


@respx.mock
async def test_all_searches_failing_is_an_error():
    respx.get(f"{API_URL}/search/issues").mock(return_value=httpx.Response(422, json={"message": "Validation Failed"}))
    resp = await handle_search(GitHubClient(sleep=no_sleep), SearchRequest(request_id="r2", languages=["python"]))
    assert not resp.ok and "Validation Failed" in resp.error


@respx.mock
async def test_zero_results_is_a_valid_answer_not_a_failure():
    respx.get(f"{API_URL}/search/issues").mock(return_value=httpx.Response(200, json={"total_count": 0, "items": []}))
    resp = await handle_search(GitHubClient(sleep=no_sleep), SearchRequest(request_id="r0", languages=["brainfuck"]))
    assert resp.ok and resp.candidates == [] and resp.error == ""


@respx.mock
async def test_widen_uses_more_labels_and_longer_recency():
    route = respx.get(f"{API_URL}/search/issues").mock(return_value=httpx.Response(200, json={"items": []}))
    await handle_search(GitHubClient(sleep=no_sleep), SearchRequest(request_id="rw", languages=["cobol"], widen=True))
    q = route.calls.last.request.url.params["q"]
    assert '"help wanted"' in q and "beginner" in q and "label:" in q


@respx.mock
async def test_demo_mode_pins_issues_from_the_allowlisted_repo_first(monkeypatch):
    monkeypatch.setenv("ALLOWED_TARGET_REPOS", "me/demo-repo")

    def handler(request: httpx.Request) -> httpx.Response:
        if "repo:me/demo-repo" in request.url.params["q"]:
            return httpx.Response(200, json={"items": [item("me/demo-repo", n) for n in (1, 2, 3)]})
        return httpx.Response(200, json={"items": [item("other/x", n) for n in (1, 2, 3)]})

    respx.get(f"{API_URL}/search/issues").mock(side_effect=handler)
    resp = await handle_search(GitHubClient(sleep=no_sleep), SearchRequest(request_id="rp", languages=["python"]))
    assert [c.repo for c in resp.candidates[:3]] == ["me/demo-repo"] * 3  # not limited by the 2-per-repo cap
    assert {c.repo for c in resp.candidates} == {"me/demo-repo", "other/x"}


@respx.mock
async def test_rate_limit_gives_friendly_message_not_githubs_text():
    limited = httpx.Response(403, json={"message": "You have exceeded a secondary rate limit. See the Terms of Service"})
    respx.get(f"{API_URL}/search/issues").mock(return_value=limited)
    resp = await handle_search(GitHubClient(sleep=no_sleep), SearchRequest(request_id="r4", languages=["python"]))
    assert not resp.ok and "try again in a minute" in resp.error and "Terms of Service" not in resp.error


@respx.mock
async def test_searches_are_spaced_apart():
    respx.get(f"{API_URL}/search/issues").mock(return_value=httpx.Response(200, json={"items": []}))
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    client = GitHubClient(sleep=record, search_interval=5.0)
    await client.search_issues("a")
    await client.search_issues("b")
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 5.0


async def test_no_language_is_an_error():
    resp = await handle_search(GitHubClient(sleep=no_sleep), SearchRequest(request_id="r3", languages=[" "]))
    assert not resp.ok
