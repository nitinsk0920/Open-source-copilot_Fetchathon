"""Turn agent results into chat text."""

from datetime import datetime, timezone

from copilot.logic.intent import UserProfile
from copilot.logic.ranking import RankedIssue
from copilot.models import Dropped, SearchResponse, StepInfo

MAX_FILTERED_SHOWN = 8


def _age(iso: str, now: datetime | None = None) -> str:
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "recently"
    days = ((now or datetime.now(timezone.utc)) - then).days
    if days <= 0:
        return "today"
    return "1 day ago" if days == 1 else f"{days} days ago"


def format_search_failure(resp: SearchResponse) -> str:
    return f"I couldn't search GitHub right now: {resp.error}\nPlease try again in a minute."


def filtered_lines(dropped: list[Dropped]) -> str:
    lines = [f"- {d.repo} #{d.number}: {d.reason}" for d in dropped[:MAX_FILTERED_SHOWN]]
    if len(dropped) > MAX_FILTERED_SHOWN:
        lines.append(f"- ...and {len(dropped) - MAX_FILTERED_SHOWN} more")
    return "\n".join(lines)


def format_none_survived(profile: UserProfile, considered: int, dropped: list[Dropped], warnings: list[str]) -> str:
    notes = "".join(f"\n_Note: {w}_" for w in warnings)
    if not considered:
        return (f"I found no open, unassigned beginner issues for {profile.summary()}, even after widening the search."
                f"{notes}\nTry another language (for example \"Python or JavaScript\") or a different topic.")
    return (f"I checked {considered} candidates for {profile.summary()}, but none passed live verification, "
            f"even after widening the search:\n{filtered_lines(dropped)}{notes}\n"
            "Try another language, or tell me what you'd like to work on (for example \"documentation\" or \"tests\").")


def format_ranked(profile: UserProfile, ranked: list[RankedIssue], considered: int, dropped: list[Dropped],
                  warnings: list[str], widened: bool = False) -> str:
    ai = any(r.ai_ranked for r in ranked)
    head = (f"I checked {considered} candidates live for **{profile.summary()}**"
            f"{' (after widening the search)' if widened else ''}. {considered - len(dropped)} survived; "
            f"here are the top {len(ranked)}{'' if ai else ' (AI ranking was unavailable, so ordered by freshness)'}:")
    lines = [head, ""]
    for i, r in enumerate(ranked, 1):
        c, v = r.candidate, r.verification
        meta = [r.difficulty] if r.difficulty != "unknown" else []
        if r.est_hours:
            meta.append(f"~{r.est_hours:g} h")
        if r.ai_ranked:
            meta.append(f"fit {r.fit}/10")
        lines.append(f"{i}. **{c.repo}** #{c.number}: {c.title}")
        if meta:
            lines.append(f"   {' | '.join(meta)}")
        if r.reason:
            lines.append(f"   Why: {r.reason}")
        if v:
            lines.append(f"   Verified live: {', '.join(v.checks)}")
            lines += [f"   Heads-up: {n}" for n in v.notes]
        lines.append(f"   {', '.join(c.labels[:4])} | {c.comments} comments | updated {_age(c.updated_at)}")
        lines.append(f"   {c.url}")
    if dropped:
        lines += ["", f"Filtered out ({len(dropped)} of {considered}):", filtered_lines(dropped)]
    lines += [""] + [f"_Note: {w}_" for w in warnings]
    lines.append('Reply "pick 2" to choose one, or "cards" for the interactive picker.')
    return "\n".join(lines)


def format_review(r: RankedIssue, comment: str, branch: str, live: bool) -> str:
    c, v = r.candidate, r.verification
    default = v.default_branch if v else "main"
    mode = ("LIVE: this repo is on the demo allowlist, so approving will really write to GitHub."
            if live else
            "DRY RUN: this repo is not on the demo allowlist, so approving will NOT write anything to GitHub. "
            "I'll show exactly what would happen and still prepare your plan.")
    lines = [f"You picked **{c.repo}** #{c.number}: {c.title}", c.url]
    if r.reason:
        lines.append(f"Why it fits you: {r.reason}")
    lines += ["", "**Review: this is exactly what I will do. Nothing has been changed yet.**", mode, "",
              f"1. Fork `{c.repo}` into your GitHub account",
              f"2. Post this comment on the issue:", *(f"   > {ln}" for ln in comment.splitlines()),
              f"3. Create branch `{branch}` in your fork, from `{default}`"
              + (f" (named as {v.contributing_path} asks)" if v and v.branch_pattern else ""),
              "4. Write your contribution plan (clone, test, edit, commit, open the PR)", "",
              'I won\'t open the pull request now: after you\'ve pushed your own fix, type "open pr".', "",
              'Reply "approve" to run it, "edit comment: <your text>" to change the comment, '
              'or "reject" to cancel. You can also "pick N" to choose a different issue.']
    return "\n".join(lines)


def format_pr_review(r: RankedIssue, title: str, body: str, head: str, base: str, live: bool, check: str) -> str:
    c = r.candidate
    mode = ("LIVE: this repo is on the demo allowlist, so approving will really open the pull request."
            if live else
            "DRY RUN: this repo is not on the demo allowlist, so approving will NOT open anything.")
    return "\n".join([
        f"**Pull request review for {c.repo} #{c.number}. Nothing has been opened yet.**", mode, "",
        f"Your changes: {check or 'not checked'}",
        f"From: `{head}`", f"Into: `{base}` of {c.repo}",
        f"Title: {title}", "Description:", *(f"> {ln}" for ln in body.splitlines()), "",
        'Reply "approve" to open it, "edit pr: <description>" to write your own description (I add `Fixes #'
        f'{c.number}`), or "reject" to cancel.'])


def format_steps(steps: list[StepInfo]) -> str:
    icon = {"ok": "done", "simulated": "would do", "failed": "FAILED", "skipped": "skipped"}
    return "\n".join(f"- {s.name}: {icon.get(s.status, s.status)}, {s.detail}" + (f"\n  {s.url}" if s.url else "")
                     for s in steps)
