import httpx
import respx

from copilot.tools.github_client import API_URL, GitHubClient, parse_issue_item
from tests.conftest import no_sleep

ITEM = {
    "number": 7,
    "title": "Fix typo in docs",
    "html_url": "https://github.com/acme/widget/issues/7",
    "repository_url": f"{API_URL}/repos/acme/widget",
    "labels": [{"name": "good first issue"}, {"name": "docs"}],
    "comments": 2,
    "created_at": "2026-10-01T10:00:00Z",
    "updated_at": "2026-10-20T10:00:00Z",
    "body": "x" * 2000,
}
PR_ITEM = {**ITEM, "number": 8, "pull_request": {"url": "..."}}


def test_parse_issue_item():
    c = parse_issue_item(ITEM, language="python")
    assert (c.repo, c.number, c.language) == ("acme/widget", 7, "python")
    assert c.labels == ["good first issue", "docs"]
    assert len(c.body_excerpt) == 600


@respx.mock
async def test_search_issues_filters_prs_and_sends_auth():
    route = respx.get(f"{API_URL}/search/issues").mock(
        return_value=httpx.Response(200, json={"total_count": 2, "items": [ITEM, PR_ITEM]})
    )
    client = GitHubClient(sleep=no_sleep)
    total, items = await client.search_issues("is:issue", language="python")
    assert total == 2 and [i.number for i in items] == [7]
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer test-token-not-real"
    assert req.url.params["sort"] == "updated"
    await client.aclose()


@respx.mock
async def test_search_is_cached():
    route = respx.get(f"{API_URL}/search/issues").mock(return_value=httpx.Response(200, json={"total_count": 0, "items": []}))
    client = GitHubClient(sleep=no_sleep)
    await client.search_issues("q")
    await client.search_issues("q")
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_no_token_means_no_auth_header(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "")
    route = respx.get(f"{API_URL}/search/issues").mock(return_value=httpx.Response(200, json={"items": []}))
    client = GitHubClient(sleep=no_sleep)
    await client.search_issues("q")
    assert "authorization" not in route.calls.last.request.headers
    await client.aclose()
