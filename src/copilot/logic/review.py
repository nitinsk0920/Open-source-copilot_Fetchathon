"""Text the user reviews before approving. Pure functions."""

import hashlib
import re

from copilot.models import Issue, Verification

MAX_COMMENT_CHARS = 2000


def shorten(text: str, max_len: int) -> str:
    """Cut at a word boundary instead of mid-word."""
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    return (cut.rsplit(" ", 1)[0] if " " in cut and text[max_len] != " " else cut).rstrip(" -")


_FILLER = {"a", "an", "and", "at", "by", "for", "from", "in", "is", "of", "on", "or", "the", "to", "with"}


def slugify(text: str, max_len: int = 30) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > max_len:
        cut = slug[:max_len]
        slug = cut.rsplit("-", 1)[0] if "-" in cut and slug[max_len] != "-" else cut
        words = slug.split("-")
        while len(words) > 1 and words[-1] in _FILLER:  # never end on "in", "to", "the"...
            words.pop()
        slug = "-".join(words)
    return slug.strip("-") or "change"


_SAFE_BRANCH = re.compile(r"^(?![-/.])[A-Za-z0-9._/-]{1,100}$")


def apply_branch_pattern(pattern: str, number: int) -> str | None:
    """'fix/issue-<number>' -> 'fix/issue-2'. None if the result is not a safe git branch name."""
    name = re.sub(r"<[^<>]+>|\{[^{}]+\}", str(number), pattern.strip())
    if _SAFE_BRANCH.match(name) and ".." not in name and "//" not in name and not name.endswith(("/", ".", ".lock")):
        return name
    return None


def branch_name(issue: Issue, verification: Verification | None = None) -> str:
    """The repo's own naming rule from CONTRIBUTING when it has one, else a descriptive default."""
    if verification and verification.branch_pattern:
        named = apply_branch_pattern(verification.branch_pattern, issue.number)
        if named:
            return named
    return f"fix-issue-{issue.number}-{slugify(issue.title)}"


def issue_ref(issue: Issue) -> str:
    return f"{issue.repo}#{issue.number}"


def review_ref(issue: Issue, comment: str, branch: str) -> str:
    """Identifies exactly what a review showed (issue + comment + branch). An Approve card carries it, so a stale
    card, left over from before a comment edit or a different pick, can never approve something else."""
    digest = hashlib.sha1(f"{comment}\n{branch}".encode()).hexdigest()[:8]
    return f"{issue_ref(issue)}:{digest}"


def default_comment(issue: Issue, verification: Verification | None) -> str:
    guide = f" I'll read {verification.contributing_path} first and" if verification and verification.has_contributing else " I'll"
    return (
        f"Hi! I'd like to work on this issue.{guide} open a pull request once it's ready. "
        "Is it still available, and is there anything I should know before starting? Thanks!"
    )


_FIXES_LINE = re.compile(r"(?im)^(\s*Fixes\s+#)[ \t]*$")


def pr_title(issue: Issue) -> str:
    return issue.title.strip()[:200]


def pr_body(issue: Issue, verification: Verification | None, note: str = "") -> str:
    """The description: the user's own text if they wrote one, else the repo's PR template with `Fixes #N` filled in."""
    fixes = f"Fixes #{issue.number}"
    if note.strip():
        return f"{note.strip()[:MAX_COMMENT_CHARS]}\n\n{fixes}"
    template = (verification.pr_template_text if verification else "").strip()
    if template:
        if _FIXES_LINE.search(template):
            return _FIXES_LINE.sub(rf"\g<1>{issue.number}", template, count=1)
        return f"{template}\n\n{fixes}"
    return f"{fixes}\n\nResolves: {issue.url}"


def pr_ref(issue: Issue, title: str, body: str, head: str) -> str:
    """Like review_ref, for the pull request review: binds an Approve card to exactly this title, body and branch."""
    digest = hashlib.sha1(f"{title}\n{body}\n{head}".encode()).hexdigest()[:8]
    return f"{issue_ref(issue)}:pr:{digest}"


def approval_marker(approval_id: str) -> str:
    """Hidden HTML comment appended to the claim comment so a retry can tell it was already posted."""
    return f"<!-- oss-copilot:{approval_id} -->"


def clean_comment(text: str) -> str | None:
    """User-edited comment: trimmed, bounded, and never able to forge our idempotency marker."""
    text = text.replace("<!--", "").replace("-->", "").strip()
    return text[:MAX_COMMENT_CHARS] if text else None
