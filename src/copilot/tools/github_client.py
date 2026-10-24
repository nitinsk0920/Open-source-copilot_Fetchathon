"""Async GitHub REST client (read side for now). All auth goes through get_github_token()."""

import asyncio
import base64
import logging
import time
from typing import Any

import httpx

from copilot.models import IssueCandidate
from copilot.tools.github_auth import get_github_token
from copilot.tools.http import ApiError, TTLCache, request_with_retry

logger = logging.getLogger(__name__)

API_URL = "https://api.github.com"
BODY_EXCERPT_CHARS = 600


def parse_issue_item(item: dict[str, Any], language: str = "") -> IssueCandidate:
    """Map a /search/issues item to an IssueCandidate."""
    repo = item.get("repository_url", "").removeprefix(f"{API_URL}/repos/")
    body = (item.get("body") or "").strip()
    return IssueCandidate(
        repo=repo,
        number=item["number"],
        title=item.get("title", ""),
        url=item.get("html_url", ""),
        labels=[lbl["name"] for lbl in item.get("labels", []) if isinstance(lbl, dict) and "name" in lbl],
        comments=item.get("comments", 0),
        created_at=item.get("created_at", ""),
        updated_at=item.get("updated_at", ""),
        language=language,
        body_excerpt=body[:BODY_EXCERPT_CHARS],
    )


class GitHubClient:
    def __init__(
        self,
        user_id: str | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        cache_ttl: float = 300.0,
        timeout: float = 15.0,
        retries: int = 3,
        sleep=None,
        search_interval: float = 1.0,
        max_concurrency: int = 4,
    ):
        self.user_id = user_id
        self._http = http or httpx.AsyncClient(base_url=API_URL, timeout=timeout)
        self._cache = TTLCache(cache_ttl)
        self._retries = retries
        self._sleep = sleep or asyncio.sleep
        self._search_interval = search_interval  # GitHub's secondary limit punishes rapid-fire searches
        self._search_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(max_concurrency)
        self._last_search = 0.0
        self._warned_no_token = False

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        token = get_github_token(self.user_id)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif not self._warned_no_token:
            logger.warning("No GitHub token configured: using unauthenticated limits (60/hr, search 10/min)")
            self._warned_no_token = True
        return headers

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None,
                       retries: int | None = None):
        async with self._slots:  # small batches: GitHub's secondary limits punish bursts of concurrent calls
            return await request_with_retry(
                self._http, method, path, params=params, headers=self._headers(), json=json,
                retries=self._retries if retries is None else retries, sleep=self._sleep,
            )

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        key = (path, tuple(sorted((params or {}).items())))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        data = (await self._request("GET", path, params=params)).json()
        self._cache.set(key, data)
        return data

    async def get_optional(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        """Like get_json, but a missing resource (404), empty repo (409) or unavailable one (451) gives None."""
        try:
            return await self.get_json(path, params)
        except ApiError as exc:
            if exc.status in (404, 409, 451):
                return None
            raise

    # --- read helpers used by the Analyst ---
    async def get_repo(self, repo: str) -> dict:
        return await self.get_json(f"/repos/{repo}")

    async def get_issue(self, repo: str, number: int) -> dict:
        return await self.get_json(f"/repos/{repo}/issues/{number}")

    async def get_issue_comments(self, repo: str, number: int) -> list[dict]:
        return await self.get_json(f"/repos/{repo}/issues/{number}/comments", {"per_page": 100}) or []

    async def get_issue_timeline(self, repo: str, number: int) -> list[dict]:
        return await self.get_json(f"/repos/{repo}/issues/{number}/timeline", {"per_page": 100}) or []

    async def list_dir(self, repo: str, path: str = "") -> list[dict]:
        data = await self.get_optional(f"/repos/{repo}/contents/{path}".rstrip("/"))
        return data if isinstance(data, list) else []

    async def get_file_text(self, repo: str, path: str, max_chars: int = 20000) -> str | None:
        data = await self.get_optional(f"/repos/{repo}/contents/{path}")
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")[:max_chars]

    # --- write helpers used by the Action agent (only ever called after approval) ---
    async def get_login(self) -> str:
        return (await self.get_json("/user"))["login"]

    async def fork_repo(self, repo: str) -> dict:
        """202 and async on GitHub's side. Forking an already-forked repo returns the existing fork."""
        return (await self._request("POST", f"/repos/{repo}/forks", json={})).json()

    async def wait_for_fork(self, fork: str, branch: str, *, timeout: float = 60.0, interval: float = 2.0) -> str:
        """Poll until the fork's base branch is readable; returns its commit sha."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                resp = await self._request("GET", f"/repos/{fork}/git/ref/heads/{branch}", retries=0)
                return resp.json()["object"]["sha"]
            except ApiError as exc:
                if exc.status not in (404, 409, None):
                    raise
            if time.monotonic() >= deadline:
                raise ApiError(None, f"the fork was not ready after {timeout:g}s")
            await self._sleep(interval)

    async def create_comment(self, repo: str, number: int, body: str) -> dict:
        # POST is not idempotent, so no automatic retries: a duplicate comment is worse than a reported failure.
        return (await self._request("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body},
                                    retries=0)).json()

    async def find_comment_with(self, repo: str, number: int, marker: str) -> dict | None:
        resp = await self._request("GET", f"/repos/{repo}/issues/{number}/comments", params={"per_page": 100})
        return next((c for c in resp.json() if marker in (c.get("body") or "")), None)

    async def compare(self, repo: str, base: str, head: str) -> dict | None:
        """Compare `base` with `head` ("forkowner:branch"). None if the branch does not exist."""
        try:
            return (await self._request("GET", f"/repos/{repo}/compare/{base}...{head}")).json()
        except ApiError as exc:
            if exc.status in (404, 422):
                return None
            raise

    async def find_open_pr(self, repo: str, head: str) -> dict | None:
        resp = await self._request("GET", f"/repos/{repo}/pulls", params={"state": "open", "head": head})
        return next(iter(resp.json()), None)

    async def create_pr(self, repo: str, *, title: str, head: str, base: str, body: str) -> dict:
        # POST is not idempotent: no automatic retries. The caller checks for an existing PR for this head first.
        return (await self._request("POST", f"/repos/{repo}/pulls", retries=0,
                                    json={"title": title, "head": head, "base": base, "body": body})).json()

    async def create_branch(self, fork: str, branch: str, sha: str) -> str:
        """Returns "created" or "exists"."""
        try:
            await self._request("POST", f"/repos/{fork}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha},
                                retries=0)
            return "created"
        except ApiError as exc:
            if exc.status == 422 and "already exists" in exc.message.lower():
                return "exists"
            raise

    async def search_issues(self, query: str, *, per_page: int = 30, language: str = "") -> tuple[int, list[IssueCandidate]]:
        """Run an issue search. Returns (total_count, parsed issues). PRs are excluded by the query (is:issue)."""
        async with self._search_lock:
            gap = self._search_interval - (time.monotonic() - self._last_search)
            if gap > 0:
                await self._sleep(gap)
            try:
                data = await self.get_json(
                    "/search/issues", {"q": query, "sort": "updated", "order": "desc", "per_page": per_page}
                )
            finally:
                self._last_search = time.monotonic()
        items = [parse_issue_item(i, language) for i in data.get("items", []) if "pull_request" not in i]
        return data.get("total_count", len(items)), items

    async def aclose(self) -> None:
        await self._http.aclose()
