"""Action agent: the only agent that writes to GitHub. Fork, claim comment, branch.

Safety: it acts only on requests from the Orchestrator, only with an approval id, only on allowlisted repos
while DEMO_MODE is on (anything else is a dry run), and every step reports its own result so a retry can
repeat only what failed.
"""

import logging
import re

from uagents import Agent, Context

from copilot import config
from copilot.chat import worker_chat_protocol
from copilot.config import PROFILES_DIR
from copilot.logic.review import approval_marker
from copilot.models import ActionRequest, ActionResponse, StepResult
from copilot.tools.github_client import GitHubClient
from copilot.tools.http import ApiError, RateLimitError

logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Worker agent of the Open-Source Contribution Copilot. After the user's explicit approval, forks the repository, "
    "posts the claim comment and creates a starter branch. Live writes are restricted to allowlisted repos."
)
STEP_ORDER = ("fork", "comment", "branch", "pr_check", "pr")
WRITE_STEPS = {"fork", "comment", "branch", "pr"}  # pr_check only reads, so it needs no approval
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")


def describe_error(exc: ApiError) -> str:
    if isinstance(exc, RateLimitError):
        return "GitHub is rate-limiting requests; retry in a minute"
    if exc.status == 401:
        return "GitHub rejected the access token (expired or invalid)"
    if exc.status == 403:
        return f"the token lacks permission for this action ({exc.message[:80]})"
    if exc.status == 404:
        return "not found (is the repository public, and does the token have the public_repo scope?)"
    if exc.status is None:
        return f"{exc.message}; GitHub may be unreachable"
    return f"GitHub said: {exc.message[:120]}"


def validate(req: ActionRequest) -> str | None:
    if WRITE_STEPS & set(req.steps) and not req.approval_id:
        return "no approval id: refusing to write anything"
    if not _REPO.match(req.repo) or req.issue_number < 1:
        return "invalid repository or issue number"
    if not _BRANCH.match(req.branch_name) or ".." in req.branch_name:
        return "invalid branch name"
    if "comment" in req.steps and (not req.comment_text.strip() or len(req.comment_text) > 4000):
        return "the comment is empty or too long"
    if {"pr", "pr_check"} & set(req.steps) and req.fork_full_name and not _REPO.match(req.fork_full_name):
        return "invalid fork name"
    if "pr" in req.steps and (not req.pr_title.strip() or len(req.pr_title) > 256 or len(req.pr_body) > 6000):
        return "the pull request title is empty or too long, or the description is too long"
    return None


def _dry_run(req: ActionRequest, steps: list[str]) -> ActionResponse:
    detail = {
        "fork": f"would fork {req.repo} into your account",
        "comment": f"would post the claim comment on {req.repo}#{req.issue_number}",
        "branch": f"would create branch `{req.branch_name}` in your fork from `{req.default_branch}`",
        "pr_check": f"would check that `{req.branch_name}` in your fork has your commit(s)",
        "pr": f"would open the pull request \"{req.pr_title}\" into `{req.default_branch}` of {req.repo}",
    }
    return ActionResponse(
        request_id=req.request_id, ok=True, dry_run=True,
        steps=[StepResult(name=s, status="simulated", detail=detail[s]) for s in steps],
        fork_full_name=req.fork_full_name,
    )


async def _pr_check(gh: GitHubClient, req: ActionRequest, fork: str) -> StepResult:
    """Read-only: does the contributor's branch exist in the fork and carry commits of its own? Is a PR already open?"""
    if not fork:
        return StepResult(name="pr_check", status="failed", detail="no fork is known yet, so there is no branch to check")
    head = f"{fork.split('/')[0]}:{req.branch_name}"
    try:
        existing = await gh.find_open_pr(req.repo, head)
        if existing:
            return StepResult(name="pr_check", status="exists", url=existing.get("html_url", ""),
                              detail=f"a pull request is already open (#{existing.get('number')})")
        comparison = await gh.compare(req.repo, req.default_branch, head)
    except ApiError as exc:
        return StepResult(name="pr_check", status="failed", detail=describe_error(exc))
    if comparison is None:
        return StepResult(name="pr_check", status="failed",
                          detail=f"I can't find branch `{req.branch_name}` in {fork}")
    ahead = comparison.get("ahead_by", 0)
    if ahead < 1:
        return StepResult(name="pr_check", status="failed",
                          detail=f"branch `{req.branch_name}` has no commits of its own yet. Push your fix to it first")
    files = [f["filename"] for f in comparison.get("files", [])][:6]
    return StepResult(name="pr_check", status="ok",
                      detail=f"branch `{req.branch_name}` is {ahead} commit(s) ahead of `{req.default_branch}`"
                             + (f", changing: {', '.join(files)}" if files else ""))


async def _open_pr(gh: GitHubClient, req: ActionRequest, fork: str) -> StepResult:
    if not fork:
        return StepResult(name="pr", status="failed", detail="no fork is known yet, so there is nothing to open a PR from")
    head = f"{fork.split('/')[0]}:{req.branch_name}"
    try:
        existing = await gh.find_open_pr(req.repo, head)  # makes a retry safe: never a second PR for the same branch
        if existing:
            return StepResult(name="pr", status="exists", url=existing.get("html_url", ""),
                              detail=f"a pull request is already open (#{existing.get('number')})")
        pr = await gh.create_pr(req.repo, title=req.pr_title.strip(), head=head, base=req.default_branch, body=req.pr_body)
        return StepResult(name="pr", status="ok", detail=f"pull request #{pr.get('number')} opened",
                          url=pr.get("html_url", ""))
    except ApiError as exc:
        if exc.status == 422 and "no commits" in exc.message.lower():
            return StepResult(name="pr", status="failed",
                              detail=f"branch `{req.branch_name}` has no commits of its own yet. Push your fix first")
        return StepResult(name="pr", status="failed", detail=describe_error(exc))


async def execute(gh: GitHubClient, req: ActionRequest) -> ActionResponse:
    problem = validate(req)
    if problem:
        return ActionResponse(request_id=req.request_id, ok=False, error=problem)
    steps = [s for s in STEP_ORDER if s in req.steps]
    if not config.live_writes_allowed(req.repo):
        logger.info("dry run for %s (not on the demo allowlist)", req.repo)
        return _dry_run(req, steps)

    results: dict[str, StepResult] = {}
    fork = req.fork_full_name

    if "fork" in steps:
        try:
            data = await gh.fork_repo(req.repo)
            fork = data["full_name"]
            results["fork"] = StepResult(name="fork", status="ok", detail=f"fork is {fork}",
                                         url=data.get("html_url", f"https://github.com/{fork}"))
        except ApiError as exc:
            results["fork"] = StepResult(name="fork", status="failed", detail=describe_error(exc))

    if "comment" in steps:
        try:
            marker = approval_marker(req.approval_id)
            existing = await gh.find_comment_with(req.repo, req.issue_number, marker)
            if existing:
                results["comment"] = StepResult(name="comment", status="ok", detail="comment was already posted",
                                                url=existing.get("html_url", ""))
            else:
                posted = await gh.create_comment(req.repo, req.issue_number, f"{req.comment_text.strip()}\n\n{marker}")
                results["comment"] = StepResult(name="comment", status="ok", detail="claim comment posted",
                                                url=posted.get("html_url", ""))
        except ApiError as exc:
            results["comment"] = StepResult(name="comment", status="failed", detail=describe_error(exc))

    if "branch" in steps:
        if not fork:
            reason = "the fork step failed" if "fork" in results else "no fork is known yet"
            results["branch"] = StepResult(name="branch", status="skipped", detail=f"skipped: {reason}")
        else:
            try:
                sha = await gh.wait_for_fork(fork, req.default_branch)
                outcome = await gh.create_branch(fork, req.branch_name, sha)
                results["branch"] = StepResult(
                    name="branch", status="ok",
                    detail=f"branch `{req.branch_name}` {'already existed' if outcome == 'exists' else 'created'} in {fork}",
                    url=f"https://github.com/{fork}/tree/{req.branch_name}")
            except ApiError as exc:
                results["branch"] = StepResult(name="branch", status="failed", detail=describe_error(exc))

    if "pr_check" in steps:
        results["pr_check"] = await _pr_check(gh, req, fork)
    if "pr" in steps:
        results["pr"] = await _open_pr(gh, req, fork)

    ordered = [results[s] for s in steps]
    failed = [s.name for s in ordered if s.status not in ("ok", "exists")]
    return ActionResponse(
        request_id=req.request_id, ok=not failed, steps=ordered, fork_full_name=fork,
        error=f"step(s) did not complete: {', '.join(failed)}" if failed else "",
    )


def create_action(*, seed: str, port: int, orchestrator_address: str) -> Agent:
    agent = Agent(
        name="oss-copilot-action",
        seed=seed,
        port=port,
        **config.connectivity(port),
        publish_agent_details=True,
        description=DESCRIPTION,
        readme_path=str(PROFILES_DIR / "action.md"),
        handle_messages_concurrently=True,
    )
    gh = GitHubClient()

    @agent.on_message(ActionRequest)
    async def on_action(ctx: Context, sender: str, msg: ActionRequest) -> None:
        if sender != orchestrator_address:  # nobody but the Orchestrator may trigger writes
            ctx.logger.warning("ignoring ActionRequest from unexpected sender %s", sender)
            return
        ctx.logger.info("ActionRequest %s: %s#%d steps=%s", msg.request_id[:8], msg.repo, msg.issue_number, msg.steps)
        try:
            response = await execute(gh, msg)
        except Exception as exc:
            ctx.logger.exception("unexpected action failure")
            response = ActionResponse(request_id=msg.request_id, ok=False, error=f"internal error ({type(exc).__name__})")
        for step in response.steps:
            ctx.logger.info("step %s: %s (%s)", step.name, step.status, step.detail)
        await ctx.send(sender, response)

    agent.include(worker_chat_protocol("Action"), publish_manifest=True)
    return agent
