"""Orchestrator: the only agent the user talks to. It owns the conversation state machine:

idle -> presented -> awaiting_approval -> executing -> done | partial (retry repeats only the failed steps)

Nothing is written to GitHub until the user types "approve"; only then is an approval id minted and sent to Action.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, chat_protocol_spec

from copilot import config
from copilot.cards import carousel_message, pr_review_message, review_message
from copilot.chat import acknowledge, is_session_start, raw_text_of, reply, text_of
from copilot.config import PROFILES_DIR
from copilot.formatting import (format_none_survived, format_pr_review, format_ranked, format_review,
                                format_search_failure, format_steps)
from copilot.logic.commands import parse_command
from copilot.logic.intent import UserProfile, parse_profile, quick_languages
from copilot.logic.ranking import TOP_N, RankedIssue, dropped_from_json, ranked_from_json
from copilot.logic.review import (branch_name, clean_comment, default_comment, issue_ref, pr_body, pr_ref, pr_title,
                                  review_ref)
from copilot.models import (ActionRequest, ActionResponse, AnalyzeRequest, AnalyzeResponse, Dropped, SearchRequest,
                            SearchResponse, StepInfo)
from copilot.state import SessionStore
from copilot.tools.llm_client import LLMClient
from copilot.tools.plan_writer import build_plan, write_plan

logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Finds a live-verified beginner-friendly open-source issue that matches your skills, "
    "then (after your approval) forks, claims and sets up a starter branch."
)

GREETING = (
    "Hi! I'm the Open-Source Contribution Copilot.\n\n"
    "Tell me your languages, experience level and how much time you have, and I'll find "
    "beginner issues that are actually still open and unclaimed, check each one live, and, only after you "
    "approve, fork the repo, post a polite claim comment and create a starter branch for you. When you've "
    "pushed your own fix, type \"open pr\" and I'll open the pull request, again only after you approve.\n\n"
    "Try: \"Find me a beginner Python issue I can finish in a weekend\"."
)
ASK_LANGUAGE = (
    "Which programming language(s) do you want to work in? For example: "
    "\"Python\" or \"JavaScript and TypeScript\". Experience level and time you can spend help too "
    "(e.g. \"I'm a student, about 5 hours\")."
)
GENERIC_ERROR = "Something went wrong on my side, and nothing was changed on GitHub. Please try again."
NO_LIST = "I don't have a list for you yet. " + ASK_LANGUAGE

MIN_RESULTS = 3  # fewer survivors than this triggers the one-time widening pass
SEARCH_TIMEOUT_S = 60.0
ANALYZE_TIMEOUT_S = 150.0
ACTION_TIMEOUT_S = 150.0
PR_TIMEOUT_S = 60.0
STEP_ORDER = ("fork", "comment", "branch")

TextHandler = Callable[[Context, str, str], Awaitable[None]]


def build_chat_protocol(on_text: TextHandler) -> Protocol:
    """ACP wiring: log, acknowledge, then hand the @mention-free text over."""
    proto = Protocol(spec=chat_protocol_spec)

    @proto.on_message(ChatMessage)
    async def on_chat(ctx: Context, sender: str, msg: ChatMessage) -> None:
        # Message text is logged (first 200 chars) during development to see how ASI:One relays card clicks.
        ctx.logger.info(
            "chat in: sender=%s session=%s msg_id=%s session_start=%s text=%r",
            sender[:14], ctx.session, msg.msg_id, is_session_start(msg), raw_text_of(msg)[:200],
        )
        await acknowledge(ctx, sender, msg)
        try:
            await on_text(ctx, sender, text_of(msg))
        except Exception:  # never leave the chat hanging
            ctx.logger.exception("unhandled error while handling a message")
            await reply(ctx, sender, GENERIC_ERROR)

    @proto.on_message(ChatAcknowledgement)
    async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
        ctx.logger.info("ack from %s for %s", sender[:14], msg.acknowledged_msg_id)

    return proto


@dataclass
class _Pending:
    user: str
    kind: str  # "search" | "analyze" | "action"
    deadline: float
    profile: UserProfile
    widened: bool = False  # True once the recovery pass has been used
    ranked: list[RankedIssue] = field(default_factory=list)  # survivors so far (first pass, before widening)
    dropped: list[Dropped] = field(default_factory=list)
    considered: int = 0
    seen: set[tuple[str, int]] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def create_orchestrator(
    *, seed: str, port: int, discovery_address: str, analyst_address: str, action_address: str,
    llm: LLMClient | None = None, store: SessionStore | None = None,
) -> Agent:
    agent = Agent(
        name="oss-contribution-copilot",
        seed=seed,
        port=port,
        **config.connectivity(port),
        publish_agent_details=True,
        description=DESCRIPTION,
        readme_path=str(PROFILES_DIR / "orchestrator.md"),
        handle_messages_concurrently=True,  # verification and ranking take seconds; don't block acks or other users
    )
    store = store or SessionStore()
    pending: dict[str, _Pending] = {}

    async def dispatch(ctx: Context, entry: _Pending, address: str, build_message, failure: str) -> bool:
        """Register a pending request and send it. On send failure, tell the user and forget it."""
        request_id = uuid4().hex
        pending[request_id] = entry
        try:
            await ctx.send(address, build_message(request_id))
            return True
        except Exception as exc:
            pending.pop(request_id, None)
            ctx.logger.error("could not reach %s agent: %s", entry.kind, type(exc).__name__)
            await reply(ctx, entry.user, failure)
            return False

    # ------------------------------------------------------------------ search -> verify -> present
    async def start_search(ctx: Context, sender: str, text: str) -> None:
        session = store.get(sender)
        session.profile = await parse_profile(llm, text, session.profile)
        store.save()
        profile = session.profile
        if not profile.languages:
            await reply(ctx, sender, ASK_LANGUAGE)
            return
        await reply(ctx, sender, f"Looking for open beginner issues for {profile.summary()}...")
        await send_search(ctx, _Pending(sender, "search", time.monotonic() + SEARCH_TIMEOUT_S, profile))

    async def send_search(ctx: Context, entry: _Pending) -> None:
        entry.kind, entry.deadline = "search", time.monotonic() + SEARCH_TIMEOUT_S
        await dispatch(ctx, entry, discovery_address,
                       lambda rid: SearchRequest(request_id=rid, languages=entry.profile.languages, widen=entry.widened),
                       "I couldn't reach my search agent. Please try again in a minute.")

    async def widen(ctx: Context, entry: _Pending, why: str) -> None:
        await reply(ctx, entry.user, f"{why}, so I'm widening the search (more labels, older issues)...")
        entry.widened = True
        await send_search(ctx, entry)

    async def finalize(ctx: Context, entry: _Pending) -> None:
        session = store.get(entry.user)
        if not entry.ranked:
            session.stage, session.ranked, session.dropped = "idle", [], entry.dropped
            store.save()
            await reply(ctx, entry.user, format_none_survived(entry.profile, entry.considered, entry.dropped,
                                                              entry.warnings))
            return
        session.ranked, session.dropped, session.considered = entry.ranked, entry.dropped, entry.considered
        session.picked, session.stage, session.steps = None, "presented", []
        session.comment = session.branch = session.approval_id = session.fork_full_name = session.plan_path = ""
        store.save()
        await reply(ctx, entry.user, format_ranked(entry.profile, entry.ranked, entry.considered, entry.dropped,
                                                   entry.warnings, entry.widened))

    @agent.on_message(SearchResponse)
    async def on_search_response(ctx: Context, sender: str, msg: SearchResponse) -> None:
        if sender != discovery_address:
            ctx.logger.warning("ignoring SearchResponse from unexpected sender %s", sender[:14])
            return
        entry = pending.pop(msg.request_id, None)
        if entry is None or entry.kind != "search":
            ctx.logger.info("late or duplicate SearchResponse %s ignored", msg.request_id[:8])
            return
        ctx.logger.info("SearchResponse %s ok=%s candidates=%d widened=%s", msg.request_id[:8], msg.ok,
                        len(msg.candidates), entry.widened)
        try:
            entry.warnings += msg.warnings
            if not msg.ok:
                if entry.ranked:  # the widening pass failed, but we still have first-pass survivors
                    entry.warnings.append(f"the widened search failed ({msg.error})")
                    await finalize(ctx, entry)
                else:
                    await reply(ctx, entry.user, format_search_failure(msg))
                return
            fresh = [c for c in msg.candidates if (c.repo.lower(), c.number) not in entry.seen]
            if not fresh:
                if entry.widened:
                    await finalize(ctx, entry)
                else:
                    await widen(ctx, entry, "I found no candidates at first")
                return
            entry.seen |= {(c.repo.lower(), c.number) for c in fresh}
            await reply(ctx, entry.user, f"Found {len(fresh)} candidates. Checking each one live (repo activity, "
                                         "claim comments, open PRs, contribution rules)...")
            entry.kind, entry.deadline = "analyze", time.monotonic() + ANALYZE_TIMEOUT_S
            p = entry.profile
            await dispatch(ctx, entry, analyst_address, lambda rid: AnalyzeRequest(
                request_id=rid, languages=p.languages, level=p.level, time_budget_hours=p.time_budget_hours or 0.0,
                interests=p.interests, candidates=fresh),
                "I couldn't reach my verification agent. Please try again in a minute.")
        except Exception:
            ctx.logger.exception("failed to handle search response")
            await reply(ctx, entry.user, GENERIC_ERROR)

    @agent.on_message(AnalyzeResponse)
    async def on_analyze_response(ctx: Context, sender: str, msg: AnalyzeResponse) -> None:
        if sender != analyst_address:
            ctx.logger.warning("ignoring AnalyzeResponse from unexpected sender %s", sender[:14])
            return
        entry = pending.pop(msg.request_id, None)
        if entry is None or entry.kind != "analyze":
            ctx.logger.info("late or duplicate AnalyzeResponse %s ignored", msg.request_id[:8])
            return
        ctx.logger.info("AnalyzeResponse %s ok=%s considered=%d verified=%d", msg.request_id[:8], msg.ok,
                        msg.considered, msg.verified)
        try:
            if not msg.ok:
                if entry.ranked:
                    entry.warnings.append(f"verifying the extra candidates failed ({msg.error})")
                    await finalize(ctx, entry)
                else:
                    await reply(ctx, entry.user, f"I couldn't verify the candidates ({msg.error}). "
                                                 "Nothing was changed. Please try again in a minute.")
                return
            entry.ranked = sorted(entry.ranked + ranked_from_json(msg.ranked_json), key=lambda r: -r.fit)[:TOP_N]
            entry.dropped += dropped_from_json(msg.dropped_json)
            entry.considered += msg.considered
            entry.warnings += msg.warnings
            if len(entry.ranked) < MIN_RESULTS and not entry.widened:
                await widen(ctx, entry, f"Only {len(entry.ranked)} of {entry.considered} candidates passed live "
                                        "verification")
            else:
                await finalize(ctx, entry)
        except Exception:
            ctx.logger.exception("failed to handle analyze response")
            await reply(ctx, entry.user, GENERIC_ERROR)

    # ------------------------------------------------------------------ pick -> review -> approve -> act
    async def show_review(ctx: Context, sender: str) -> None:
        """The text review is the source of truth and is always sent; the Approve/Reject card is an add-on."""
        session = store.get(sender)
        r = session.picked_issue()
        assert r is not None
        live = config.live_writes_allowed(r.candidate.repo)
        await reply(ctx, sender, format_review(r, session.comment, session.branch, live))
        try:
            await ctx.send(sender, review_message('Or tap a button (you can still type "approve" or "reject").',
                                                  r, session.comment, session.branch, live))
        except Exception:
            ctx.logger.exception("could not send the review card; the text commands still work")

    # ---- pull request: after the user pushed their own fix ("open pr" -> branch check -> review -> approve -> open)
    def pr_head(session) -> str:
        owner = session.fork_full_name.split("/")[0] if session.fork_full_name else "<your-github-username>"
        return f"{owner}:{session.branch}"

    def pr_description(session) -> str:
        r = session.picked_issue()
        return pr_body(r.candidate, r.verification, session.pr_note)

    def pr_base(session) -> str:
        r = session.picked_issue()
        return r.verification.default_branch if r and r.verification else "main"

    def current_ref(session) -> str:
        """What the user is being asked to approve right now (a card click must carry exactly this)."""
        r = session.picked_issue()
        if session.stage == "pr_review":
            return pr_ref(r.candidate, session.pr_title, pr_description(session), pr_head(session))
        return review_ref(r.candidate, session.comment, session.branch)

    def is_stale(session, ref: str) -> bool:
        """A card click carries a reference to what the card showed. Refuse it if that is no longer the current review."""
        return bool(ref) and (session.picked_issue() is None or ref != current_ref(session))

    async def show_pr_review(ctx: Context, sender: str) -> None:
        session = store.get(sender)
        r = session.picked_issue()
        live = config.live_writes_allowed(r.candidate.repo)
        body, head, base = pr_description(session), pr_head(session), pr_base(session)
        await reply(ctx, sender, format_pr_review(r, session.pr_title, body, head, base, live, session.pr_check))
        try:
            await ctx.send(sender, pr_review_message('Or tap a button (you can still type "approve" or "reject").',
                                                     r, session.pr_title, body, head, base, live, session.pr_check))
        except Exception:
            ctx.logger.exception("could not send the pull request card; the text commands still work")

    async def open_pr(ctx: Context, sender: str) -> None:
        """Step 1: a read-only check that the user's branch really carries their commits. No approval needed to look."""
        session = store.get(sender)
        r = session.picked_issue()
        if session.stage == "executing":
            await reply(ctx, sender, "I'm still running your approved actions. One moment.")
        elif r is None or session.stage not in ("done", "pr_review", "pr_done"):
            await reply(ctx, sender, "There is nothing to open a pull request for yet. Pick an issue and approve the "
                                     "fork, comment and branch first; after you've pushed your fix, type \"open pr\".")
        else:
            session.pr_title = session.pr_title or pr_title(r.candidate)
            store.save()
            await reply(ctx, sender, f"Checking `{session.branch}` for your changes...")
            entry = _Pending(sender, "prcheck", time.monotonic() + PR_TIMEOUT_S, session.profile)
            await dispatch(ctx, entry, action_address, lambda rid: ActionRequest(
                request_id=rid, approval_id="", repo=r.candidate.repo, issue_number=r.candidate.number,
                default_branch=pr_base(session), comment_text=session.comment, branch_name=session.branch,
                steps=["pr_check"], fork_full_name=session.fork_full_name, user_id=sender),
                "I couldn't reach my action agent. Nothing was changed. Please try again in a minute.")

    async def edit_pr(ctx: Context, sender: str, text: str) -> None:
        session = store.get(sender)
        cleaned = clean_comment(text)
        if session.stage != "pr_review":
            await reply(ctx, sender, 'There is no pull request review open. Push your fix, then type "open pr".')
        elif not cleaned:
            await reply(ctx, sender, 'The description can\'t be empty. Send "edit pr: <your text>".')
        else:
            session.pr_note = cleaned
            store.save()
            await show_pr_review(ctx, sender)

    async def approve_pr(ctx: Context, sender: str, ref: str) -> None:
        """Step 2: the user approved the pull request they were shown. This is where the PR is actually opened."""
        session = store.get(sender)
        if is_stale(session, ref):
            await reply(ctx, sender, "That button is from an earlier review, so I did not open anything. "
                                     'Please use the latest review above, or type "approve".')
            return
        r = session.picked_issue()
        session.stage, session.approval_id = "executing", uuid4().hex
        store.save()
        live = config.live_writes_allowed(r.candidate.repo)
        await reply(ctx, sender, "Approved. " + ("Opening the pull request..." if live else
                                                 "Running it as a dry run (nothing will be opened)..."))
        entry = _Pending(sender, "pr", time.monotonic() + PR_TIMEOUT_S, session.profile)
        sent = await dispatch(ctx, entry, action_address, lambda rid: ActionRequest(
            request_id=rid, approval_id=session.approval_id, repo=r.candidate.repo, issue_number=r.candidate.number,
            default_branch=pr_base(session), comment_text=session.comment, branch_name=session.branch, steps=["pr"],
            fork_full_name=session.fork_full_name, user_id=sender, pr_title=session.pr_title,
            pr_body=pr_description(session)),
            "I couldn't reach my action agent, so no pull request was opened. Reply \"approve\" to try again.")
        if not sent:
            session.stage = "pr_review"
            store.save()

    async def on_pr_response(ctx: Context, entry: _Pending, msg: ActionResponse) -> None:
        session = store.get(entry.user)
        step = msg.steps[0] if msg.steps else None
        if step is None:  # refused before doing anything
            session.stage = "pr_review" if entry.kind == "pr" else "done"
            store.save()
            await reply(ctx, entry.user, f"The action agent refused the request: {msg.error}. Nothing was changed.")
        elif entry.kind == "prcheck":
            if step.status == "exists":
                session.pr_url, session.stage = step.url, "pr_done"
                store.save()
                await reply(ctx, entry.user, f"A pull request for this branch is already open: {step.url}")
            elif step.status in ("ok", "simulated"):
                session.pr_check, session.stage = step.detail, "pr_review"
                store.save()
                await show_pr_review(ctx, entry.user)
            else:
                session.stage = "done"
                store.save()
                await reply(ctx, entry.user, f"I can't open a pull request yet: {step.detail}.\n"
                                             'When your fix is pushed, type "open pr" again.')
        else:  # the pull request itself
            if step.status in ("ok", "exists"):
                session.pr_url, session.stage = step.url, "pr_done"
                store.save()
                await reply(ctx, entry.user, ("Pull request opened: " if step.status == "ok" else
                                              "A pull request for this branch is already open: ") + step.url +
                            "\nThe maintainer will review it, which can take a day or two. Be patient and answer "
                            "any comments on the PR. I never merge anything.")
            elif step.status == "simulated":
                session.stage = "pr_done"
                store.save()
                await reply(ctx, entry.user, f"Dry run complete, nothing was opened on GitHub: {step.detail}.")
            else:
                session.stage = "pr_review"
                store.save()
                await reply(ctx, entry.user, f"The pull request was not opened: {step.detail}.\n"
                                             'Reply "approve" to try again (a PR is never opened twice), or "reject".')

    async def pick(ctx: Context, sender: str, index: int, ref: str = "") -> None:
        session = store.get(sender)
        if session.stage == "executing":
            await reply(ctx, sender, "I'm still running your approved actions. One moment.")
        elif not session.ranked:
            await reply(ctx, sender, NO_LIST)
        elif not 1 <= index <= len(session.ranked):
            await reply(ctx, sender, f"Please pick a number between 1 and {len(session.ranked)}.")
        elif ref and ref != issue_ref(session.ranked[index - 1].candidate):
            await reply(ctx, sender, 'That card belongs to an older list. Type "pick N" using the current list above.')
        else:
            session.picked, session.stage = index, "awaiting_approval"
            r = session.ranked[index - 1]
            session.comment = default_comment(r.candidate, r.verification)
            session.branch = branch_name(r.candidate, r.verification)
            session.approval_id, session.fork_full_name, session.steps, session.plan_path = "", "", [], ""
            session.pr_title = session.pr_note = session.pr_check = session.pr_url = ""
            store.save()
            await show_review(ctx, sender)

    async def edit_comment(ctx: Context, sender: str, text: str) -> None:
        session = store.get(sender)
        if session.stage != "awaiting_approval":
            await reply(ctx, sender, 'There is no pending review. Pick an issue first (for example "pick 1").')
            return
        cleaned = clean_comment(text)
        if not cleaned:
            await reply(ctx, sender, "The comment can't be empty. Send \"edit comment: <your text>\".")
            return
        session.comment = cleaned
        store.save()
        await show_review(ctx, sender)

    async def dispatch_action(ctx: Context, sender: str, steps: list[str]) -> None:
        session = store.get(sender)
        r = session.picked_issue()
        assert r is not None
        branch = r.verification.default_branch if r.verification else "main"
        entry = _Pending(sender, "action", time.monotonic() + ACTION_TIMEOUT_S, session.profile)
        sent = await dispatch(ctx, entry, action_address, lambda rid: ActionRequest(
            request_id=rid, approval_id=session.approval_id, repo=r.candidate.repo, issue_number=r.candidate.number,
            default_branch=branch, comment_text=session.comment, branch_name=session.branch, steps=steps,
            fork_full_name=session.fork_full_name, user_id=sender),
            "I couldn't reach my action agent. Nothing was changed. Reply \"retry\" to try again.")
        if not sent:
            session.stage = "partial" if session.steps else "awaiting_approval"
            store.save()

    async def approve(ctx: Context, sender: str, ref: str = "") -> None:
        session = store.get(sender)
        if session.stage == "executing":
            await reply(ctx, sender, "Already running. I'll report as soon as it finishes.")
            return
        if session.stage == "pr_review" and session.picked_issue() is not None:
            await approve_pr(ctx, sender, ref)
            return
        if session.stage != "awaiting_approval" or session.picked_issue() is None:
            await reply(ctx, sender, 'There is nothing to approve yet. Pick an issue first (for example "pick 1").')
            return
        if is_stale(session, ref):
            await reply(ctx, sender, "That Approve button is from an earlier review, so I did not run anything. "
                                     'Please use the latest review above, or type "approve".')
            return
        session.stage, session.approval_id = "executing", uuid4().hex  # the only place an approval id is created
        store.save()
        r = session.picked_issue()
        live = config.live_writes_allowed(r.candidate.repo)
        await reply(ctx, sender, "Approved. " + ("Running it now: fork, comment, branch..." if live else
                                                 "Running it as a dry run (nothing will be written)..."))
        await dispatch_action(ctx, sender, list(STEP_ORDER))

    async def retry(ctx: Context, sender: str) -> None:
        session = store.get(sender)
        if session.stage != "partial" or not session.approval_id:
            await reply(ctx, sender, "There is nothing to retry.")
            return
        todo = [s for s in STEP_ORDER if s not in {x.name for x in session.steps if x.status in ("ok", "simulated")}]
        session.stage = "executing"
        store.save()
        await reply(ctx, sender, f"Retrying only: {', '.join(todo)}...")
        await dispatch_action(ctx, sender, todo)

    async def reject(ctx: Context, sender: str, ref: str = "") -> None:
        session = store.get(sender)
        if session.stage in ("awaiting_approval", "pr_review") and is_stale(session, ref):
            await reply(ctx, sender, 'That Reject button is from an earlier review, so I ignored it. '
                                     'Type "reject" to cancel the current one.')
        elif session.stage == "pr_review":
            session.stage = "done"
            store.save()
            await reply(ctx, sender, 'Cancelled. No pull request was opened. Type "open pr" whenever you are ready.')
        elif session.stage in ("awaiting_approval", "partial"):
            done = [s.name for s in session.steps if s.status == "ok"]
            session.stage = "presented"
            store.save()
            await reply(ctx, sender, "Cancelled." + (f" Already done on GitHub: {', '.join(done)}." if done else
                                                     " Nothing was changed on GitHub.") +
                        ' You can "pick N" another issue from the list.')
        else:
            await reply(ctx, sender, "There is nothing to cancel.")

    @agent.on_message(ActionResponse)
    async def on_action_response(ctx: Context, sender: str, msg: ActionResponse) -> None:
        if sender != action_address:
            ctx.logger.warning("ignoring ActionResponse from unexpected sender %s", sender[:14])
            return
        entry = pending.pop(msg.request_id, None)
        if entry is None or entry.kind not in ("action", "prcheck", "pr"):
            ctx.logger.info("late or duplicate ActionResponse %s ignored", msg.request_id[:8])
            return
        if entry.kind in ("prcheck", "pr"):
            try:
                await on_pr_response(ctx, entry, msg)
            except Exception:
                ctx.logger.exception("failed to handle the pull request response")
                await reply(ctx, entry.user, GENERIC_ERROR)
            return
        session = store.get(entry.user)
        try:
            if not msg.steps:  # refused before doing anything
                session.stage = "partial" if session.steps else "awaiting_approval"
                store.save()
                await reply(ctx, entry.user, f"The action agent refused the request: {msg.error}. Nothing was changed.")
                return
            merged = {s.name: s for s in session.steps}
            merged.update({s.name: StepInfo(**s.dict()) for s in msg.steps})
            session.steps = [merged[n] for n in STEP_ORDER if n in merged]
            session.fork_full_name = msg.fork_full_name or session.fork_full_name
            session.dry_run = msg.dry_run
            failed = [s.name for s in session.steps if s.status not in ("ok", "simulated")]
            head = "Dry run complete, nothing was written to GitHub:" if msg.dry_run else "Results:"
            if failed:
                session.stage = "partial"
                store.save()
                await reply(ctx, entry.user, f"{head}\n{format_steps(session.steps)}\n\nNot everything worked: "
                                             f"{', '.join(failed)}. Reply \"retry\" to repeat only the failed "
                                             "step(s); the steps that succeeded will not be repeated.")
                return
            session.stage = "done"
            r = session.picked_issue()
            plan = build_plan(r, session.steps, branch=session.branch, comment=session.comment,
                              fork_full_name=session.fork_full_name, dry_run=msg.dry_run)
            try:
                session.plan_path = str(write_plan(plan, r.candidate.repo, r.candidate.number))
            except OSError:
                ctx.logger.warning("could not save the plan file")
            store.save()
            await reply(ctx, entry.user, f"{head}\n{format_steps(session.steps)}")
            await reply(ctx, entry.user, plan + (f"\n(Saved to `{session.plan_path}`)" if session.plan_path else ""))
        except Exception:
            ctx.logger.exception("failed to handle action response")
            await reply(ctx, entry.user, GENERIC_ERROR)

    # ------------------------------------------------------------------ cards (add-on; text always works)
    async def send_cards(ctx: Context, sender: str) -> None:
        session = store.get(sender)
        if not session.ranked:
            await reply(ctx, sender, NO_LIST)
            return
        try:
            await ctx.send(sender, carousel_message("Here is the interactive picker (you can still type \"pick 2\").",
                                                    session.ranked))
        except Exception:
            ctx.logger.exception("could not send card")
            await reply(ctx, sender, "I couldn't show the interactive picker. Type \"pick 2\" to choose instead.")

    # ------------------------------------------------------------------ message routing
    async def on_text(ctx: Context, sender: str, text: str) -> None:
        cmd = parse_command(text)
        session = store.get(sender)
        if session.stage == "executing" and cmd.kind not in ("approve", "pick"):
            await reply(ctx, sender, "I'm still running your approved actions. I'll report as soon as they finish.")
        elif cmd.kind == "help":
            await reply(ctx, sender, GREETING)
        elif cmd.kind == "pick":
            await pick(ctx, sender, cmd.index or 0, cmd.ref)
        elif cmd.kind == "approve":
            await approve(ctx, sender, cmd.ref)
        elif cmd.kind == "reject":
            await reject(ctx, sender, cmd.ref)
        elif cmd.kind == "edit":
            await edit_comment(ctx, sender, cmd.text)
        elif cmd.kind == "retry":
            await retry(ctx, sender)
        elif cmd.kind == "open_pr":
            await open_pr(ctx, sender)
        elif cmd.kind == "edit_pr":
            await edit_pr(ctx, sender, cmd.text)
        elif cmd.kind == "cards":
            await send_cards(ctx, sender)
        elif session.stage in ("awaiting_approval", "partial", "pr_review") and not quick_languages(text):
            what = ('"approve", "edit pr: <text>" or "reject"' if session.stage == "pr_review" else
                    '"approve", "edit comment: <text>" or "reject"')
            await reply(ctx, sender, f"I'm waiting for your decision: {what}"
                                     + (' ("retry" repeats the failed step)' if session.stage == "partial" else "") + ".")
        else:
            await start_search(ctx, sender, text)

    agent.include(build_chat_protocol(on_text), publish_manifest=True)

    @agent.on_interval(period=5.0)
    async def expire_pending(ctx: Context) -> None:
        now = time.monotonic()
        for request_id, entry in list(pending.items()):
            if now <= entry.deadline:
                continue
            pending.pop(request_id, None)
            ctx.logger.warning("%s request %s timed out", entry.kind, request_id[:8])
            if entry.kind == "action":
                session = store.get(entry.user)
                session.stage = "partial"
                store.save()
                await reply(ctx, entry.user, "The action agent didn't answer in time, so I don't know what was done "
                                             "on GitHub. Reply \"retry\": it is safe, because comments carry a marker "
                                             "and forks and branches are only created once.")
            elif entry.kind in ("prcheck", "pr"):
                session = store.get(entry.user)
                session.stage = "pr_review" if entry.kind == "pr" else "done"
                store.save()
                await reply(ctx, entry.user, "The action agent didn't answer in time. " + (
                    'I don\'t know whether the pull request was opened. Reply "approve" to try again: an existing PR '
                    "for the branch is detected, so it is never opened twice." if entry.kind == "pr" else
                    'Nothing was changed. Type "open pr" to try again.'))
            elif entry.ranked:
                entry.warnings.append("the widened search timed out")
                await finalize(ctx, entry)
            else:
                await reply(ctx, entry.user, "This is taking too long, so I stopped waiting. Nothing was changed. "
                                             "Please try again in a minute.")

    return agent
