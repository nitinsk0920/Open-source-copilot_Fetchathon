"""Discovery agent: turns a SearchRequest into ~20 candidate issues via the GitHub Search API."""

import logging

from uagents import Agent, Context

from copilot import config
from copilot.chat import worker_chat_protocol
from copilot.config import PROFILES_DIR
from copilot.logic.query_builder import DEFAULT_LABELS, WIDENED_LABELS, build_issue_query
from copilot.logic.selection import select_candidates
from copilot.models import IssueCandidate, SearchRequest, SearchResponse
from copilot.tools.github_client import GitHubClient
from copilot.tools.http import ApiError, RateLimitError

logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Worker agent of the Open-Source Contribution Copilot. Searches GitHub for open, unassigned "
    "beginner-friendly issues in the requested languages."
)
MAX_LANGUAGES = 3
PER_LANGUAGE_FETCH = 30
WIDENED_RECENCY_DAYS = 180


async def handle_search(client: GitHubClient, req: SearchRequest) -> SearchResponse:
    languages = list(dict.fromkeys(lang.strip().lower() for lang in req.languages if lang.strip()))[:MAX_LANGUAGES]
    if not languages:
        return SearchResponse(request_id=req.request_id, ok=False, error="no programming language was given")

    # Recovery pass (the Orchestrator asks for it when too few issues survive verification): more labels, older issues.
    labels = req.labels or (list(WIDENED_LABELS) if req.widen else list(DEFAULT_LABELS))
    recency_days = max(req.recency_days, WIDENED_RECENCY_DAYS) if req.widen else req.recency_days
    queries: list[str] = []
    warnings: list[str] = []
    per_language: list[list[IssueCandidate]] = []
    pinned: list[IssueCandidate] = []
    last_error = ""
    succeeded = 0  # searches that completed, even with zero results: "nothing found" is an answer, not a failure

    async def run(query: str, language: str, label: str) -> list[IssueCandidate]:
        nonlocal last_error, succeeded
        queries.append(query)
        try:
            _, items = await client.search_issues(query, per_page=PER_LANGUAGE_FETCH, language=language)
        except ApiError as exc:
            logger.warning("search for %s failed: %s", label, exc)
            if isinstance(exc, RateLimitError):
                last_error = "GitHub is rate-limiting searches right now. Please try again in a minute or two."
                warnings.append(f"search for {label} was rate-limited by GitHub")
            else:
                last_error = str(exc)
                warnings.append(f"search for {label} failed ({exc.message})")
            return []
        succeeded += 1
        logger.info("search %s -> %d issues", label, len(items))
        return items

    # Demo mode: always include the allowlisted test repo's issues, so the seeded scenarios are reachable.
    for repo in sorted(config.allowed_repos()) if config.demo_mode() else []:
        for language in languages:
            pinned += await run(build_issue_query(language, labels, recency_days, repo=repo), language, f"{repo} ({language})")
    for language in languages:
        items = await run(build_issue_query(language, labels, recency_days), language, language)
        if items:
            per_language.append(items)

    if not succeeded:
        return SearchResponse(
            request_id=req.request_id, ok=False, queries=queries, warnings=warnings,
            error=last_error or "GitHub search failed",
        )
    selected, total = select_candidates(per_language, max_results=req.max_results)
    pinned = list({(c.repo.lower(), c.number): c for c in pinned}.values())
    pinned_keys = {(c.repo.lower(), c.number) for c in pinned}
    selected = (pinned + [c for c in selected if (c.repo.lower(), c.number) not in pinned_keys])[:req.max_results]
    total = max(total, len(selected))
    return SearchResponse(
        request_id=req.request_id, ok=True, candidates=selected, total_found=total,
        queries=queries, warnings=warnings,
    )


def create_discovery(*, seed: str, port: int) -> Agent:
    agent = Agent(
        name="oss-copilot-discovery",
        seed=seed,
        port=port,
        **config.connectivity(port),
        publish_agent_details=True,
        description=DESCRIPTION,
        readme_path=str(PROFILES_DIR / "discovery.md"),
    )
    client = GitHubClient()

    @agent.on_message(SearchRequest)
    async def on_search(ctx: Context, sender: str, msg: SearchRequest) -> None:
        ctx.logger.info("SearchRequest %s from %s languages=%s", msg.request_id[:8], sender[:14], msg.languages)
        try:
            response = await handle_search(client, msg)
        except Exception as exc:  # never leave the Orchestrator waiting
            ctx.logger.exception("unexpected discovery failure")
            response = SearchResponse(request_id=msg.request_id, ok=False, error=f"internal error ({type(exc).__name__})")
        await ctx.send(sender, response)

    agent.include(worker_chat_protocol("Discovery"), publish_manifest=True)
    return agent
