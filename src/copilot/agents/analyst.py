"""Analyst agent: language filter -> live verification -> ranking. Reports why each issue was dropped."""

import asyncio
import logging
from datetime import datetime

from uagents import Agent, Context

from copilot import config
from copilot.chat import worker_chat_protocol
from copilot.config import PROFILES_DIR
from copilot.logic.intent import UserProfile
from copilot.logic.language import filter_non_english
from copilot.logic.ranking import dropped_to_json, rank_candidates, ranked_to_json
from copilot.logic.verification import verify_issue
from copilot.models import AnalyzeRequest, AnalyzeResponse, Dropped, Issue, Verification
from copilot.tools.github_client import GitHubClient
from copilot.tools.llm_client import LLMClient

logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Worker agent of the Open-Source Contribution Copilot. Verifies candidate GitHub issues live "
    "(repo activity, claim comments, open PRs, contribution rules) and ranks the survivors for a contributor."
)
VERIFY_BATCH = 4  # concurrent issues; each issue makes several API calls
VERIFY_TARGET = 10  # stop checking once this many survived: enough to rank, and keeps latency down

_LEVELS = {"beginner", "intermediate", "advanced"}


def _dropped(issue: Issue, reason: str, stage: str) -> Dropped:
    return Dropped(repo=issue.repo, number=issue.number, title=issue.title, url=issue.url, reason=reason, stage=stage)


async def analyze(gh: GitHubClient, llm: LLMClient | None, req: AnalyzeRequest,
                  now: datetime | None = None) -> AnalyzeResponse:
    profile = UserProfile(
        languages=req.languages, level=req.level if req.level in _LEVELS else "unknown",
        time_budget_hours=req.time_budget_hours or None, interests=req.interests,
    )
    issues = [Issue.from_candidate(c) for c in req.candidates]
    kept, language_dropped = filter_non_english(issues)
    dropped = [_dropped(i, reason, "language") for i, reason in language_dropped]

    verified: list[tuple[Issue, Verification]] = []
    unchecked = 0
    for start in range(0, len(kept), VERIFY_BATCH):
        if len(verified) >= VERIFY_TARGET or unchecked:
            break
        batch = kept[start:start + VERIFY_BATCH]
        for issue, outcome in zip(batch, await asyncio.gather(*(verify_issue(gh, i, now) for i in batch))):
            if outcome.unchecked:
                unchecked += 1
            elif outcome.drop_reason:
                dropped.append(_dropped(issue, outcome.drop_reason, "verification"))
            else:
                verified.append((issue, outcome.verification))
    warnings = []
    if unchecked:
        warnings.append("GitHub's rate limit was reached, so the remaining candidates were not checked")

    by_key = {(i.repo, i.number): v for i, v in verified}
    ranked = await rank_candidates(llm, profile, [i for i, _ in verified])
    for r in ranked:
        r.verification = by_key[(r.candidate.repo, r.candidate.number)]
    logger.info("analyzed %d candidates: %d language-dropped, %d verified, %d dropped by checks, ranked %d",
                len(issues), len(language_dropped), len(verified), len(dropped) - len(language_dropped), len(ranked))
    return AnalyzeResponse(
        request_id=req.request_id, ok=True, ranked_json=ranked_to_json(ranked),
        dropped_json=dropped_to_json(dropped), considered=len(issues), verified=len(verified),
        ai_ranked=any(r.ai_ranked for r in ranked), warnings=warnings,
    )


def create_analyst(*, seed: str, port: int, llm: LLMClient | None) -> Agent:
    agent = Agent(
        name="oss-copilot-analyst",
        seed=seed,
        port=port,
        **config.connectivity(port),
        publish_agent_details=True,
        description=DESCRIPTION,
        readme_path=str(PROFILES_DIR / "analyst.md"),
        handle_messages_concurrently=True,
    )
    gh = GitHubClient()

    @agent.on_message(AnalyzeRequest)
    async def on_analyze(ctx: Context, sender: str, msg: AnalyzeRequest) -> None:
        ctx.logger.info("AnalyzeRequest %s: %d candidates", msg.request_id[:8], len(msg.candidates))
        try:
            response = await analyze(gh, llm, msg)
        except Exception as exc:  # never leave the Orchestrator waiting
            ctx.logger.exception("unexpected analyst failure")
            response = AnalyzeResponse(request_id=msg.request_id, ok=False, error=f"internal error ({type(exc).__name__})")
        await ctx.send(sender, response)

    agent.include(worker_chat_protocol("Analyst"), publish_manifest=True)
    return agent
