import json

from uagents_core.contrib.protocols.chat import MetadataContent, TextContent

from copilot.cards import review_message, review_payload
from copilot.formatting import format_review
from copilot.logic.commands import parse_command
from copilot.logic.ranking import RankedIssue
from copilot.logic.review import apply_branch_pattern, branch_name, review_ref
from copilot.logic.verification import parse_branch_pattern, verify_issue
from copilot.models import Issue, Verification
from copilot.tools.plan_writer import build_plan, resolve_paths
from tests.test_verification import NOW, FakeGH

CONTRIBUTING = """# Contributing

1. Comment on an issue to say you'd like to work on it.
2. Fork the repo and create a branch named `fix/issue-<number>`.
3. Open a pull request and link the issue with "Fixes #<number>".
"""


def issue(n: int = 2, title: str = "Fix typo in error message in utils.js") -> Issue:
    return Issue(repo="acme/demo", number=n, title=title, url="u")


# ---- branch naming rule from CONTRIBUTING ----

def test_parse_branch_pattern():
    assert parse_branch_pattern(CONTRIBUTING) == "fix/issue-<number>"
    assert parse_branch_pattern("Name your branch `feature/{issue}-short-name` please") == "feature/{issue}-short-name"
    assert parse_branch_pattern("Create a branch named `main`") == ""  # no issue placeholder: not a rule we can apply
    assert parse_branch_pattern("Use `fix/issue-<number>` for commits") == ""  # not about a branch
    assert parse_branch_pattern("") == ""


def test_the_plain_contributing_file_beats_a_translated_copy():
    from copilot.logic.verification import pick_contributing

    assert pick_contributing(["README.md", "CONTRIBUTING.es.md", "CONTRIBUTING.md"]) == "CONTRIBUTING.md"
    assert pick_contributing([".github/CONTRIBUTING.rst", "CONTRIBUTING.zh-CN.md"]) == ".github/CONTRIBUTING.rst"
    assert pick_contributing(["CONTRIBUTING.es.md"]) == "CONTRIBUTING.es.md"  # better than nothing
    assert pick_contributing(["README.md"]) == ""


def test_apply_branch_pattern_is_safe():
    assert apply_branch_pattern("fix/issue-<number>", 2) == "fix/issue-2"
    assert apply_branch_pattern("feature/{issue}-x", 14) == "feature/14-x"
    for bad in ["../<number>", "-x-<number>", "a b-<number>", "x//<number>", "<number>.lock", "a;rm -rf-<number>"]:
        assert apply_branch_pattern(bad, 2) is None, bad


def test_branch_name_prefers_the_repos_rule_and_falls_back():
    assert branch_name(issue(), Verification(branch_pattern="fix/issue-<number>")) == "fix/issue-2"
    assert branch_name(issue(), Verification()) == "fix-issue-2-fix-typo-in-error-message"
    assert branch_name(issue(), Verification(branch_pattern="../<number>")) == "fix-issue-2-fix-typo-in-error-message"
    assert branch_name(issue()) == "fix-issue-2-fix-typo-in-error-message"


async def test_verification_records_the_branch_rule():
    gh = FakeGH(root=[{"name": "CONTRIBUTING.md"}], files={"CONTRIBUTING.md": CONTRIBUTING})
    v = (await verify_issue(gh, Issue(repo="a/b", number=2, title="t", url="u"), NOW)).verification
    assert v.branch_pattern == "fix/issue-<number>"
    assert "Branches must be named `fix/issue-<number>`" in v.contributing_notes


def test_review_text_says_when_the_branch_follows_the_repo_rule():
    r = RankedIssue(candidate=issue(), verification=Verification(branch_pattern="fix/issue-<number>",
                                                                 contributing_path="CONTRIBUTING.md"))
    assert "named as CONTRIBUTING.md asks" in format_review(r, "c", "fix/issue-2", live=True)
    plain = RankedIssue(candidate=issue(), verification=Verification())
    assert "asks" not in format_review(plain, "c", "fix-issue-2-x", live=True)


# ---- plan: file names checked against the real repo ----

def test_plan_matches_a_misspelled_file_name_to_the_real_one():
    assert resolve_paths(["utils.js"], ["README.md", "utlis.js"]) == ["`utlis.js` (the issue says `utils.js`)"]
    assert resolve_paths(["README.md"], ["README.md", "utlis.js"]) == ["`README.md`"]
    assert resolve_paths(["src/app.py"], ["README.md"]) == ["`src/app.py`"]  # nested paths can't be checked at top level
    assert resolve_paths(["totally_other.rs"], ["README.md"]) == ["`totally_other.rs`"]
    assert resolve_paths(["utils.js"], []) == ["`utils.js`"]


def test_plan_uses_the_real_file_and_branch():
    v = Verification(root_files=["README.md", "utlis.js"], branch_pattern="fix/issue-<number>")
    r = RankedIssue(candidate=issue(), verification=v)
    plan = build_plan(r, [], branch="fix/issue-2", comment="c", fork_full_name="me/demo", dry_run=False)
    assert "`utlis.js` (the issue says `utils.js`)" in plan and "git checkout fix/issue-2" in plan


# ---- review card ----

def test_review_card_matches_the_documented_shape():
    r = RankedIssue(candidate=issue(), verification=Verification(default_branch="main"))
    p = review_payload(r, "Hi! Can I take this?", "fix/issue-2", live=True)
    assert set(p) == {"title", "summary_rows", "approve_cta", "reject_cta"}  # nothing the docs don't list
    assert all(set(row) == {"label", "value"} and isinstance(row["value"], str) for row in p["summary_rows"])
    assert p["approve_cta"]["primary"] is True and "primary" not in p["reject_cta"]
    assert p["approve_cta"]["selection"]["action"] == "approve" and p["reject_cta"]["selection"]["action"] == "reject"
    assert "LIVE" in next(row["value"] for row in p["summary_rows"] if row["label"] == "Mode")
    dry = review_payload(r, "c", "b", live=False)
    assert "DRY RUN" in next(row["value"] for row in dry["summary_rows"] if row["label"] == "Mode")


def test_review_card_message_is_a_valid_card_and_truncates_long_comments():
    r = RankedIssue(candidate=issue())
    msg = review_message("narration", r, "x" * 1500, "b", live=True)
    text, meta = msg.content
    assert isinstance(text, TextContent) and isinstance(meta, MetadataContent)
    assert meta.metadata["card_kind"] == "review" and meta.metadata["requires_card_interaction"] == "true"
    assert all(isinstance(v, str) for v in meta.metadata.values())
    rows = json.loads(meta.metadata["card_payload"])["summary_rows"]
    assert len(next(row["value"] for row in rows if row["label"] == "2. Comment")) <= 240


def test_card_clicks_parse_with_their_reference_and_ignore_platform_keys():
    r = RankedIssue(candidate=issue())
    sel = review_payload(r, "c", "b", True)["approve_cta"]["selection"]
    click = parse_command(json.dumps({**sel, "approved": True}))  # ASI:One adds "approved": true on its own
    assert click.kind == "approve" and click.ref == review_ref(r.candidate, "c", "b")
    assert parse_command(json.dumps({**sel, "approved": False})).kind == "text"  # never approve on an explicit false
    reject = parse_command(json.dumps({**review_payload(r, "c", "b", True)["reject_cta"]["selection"], "approved": True}))
    assert reject.kind == "reject"  # the extra key must not turn a Reject into anything else


def test_review_ref_changes_when_the_comment_branch_or_issue_changes():
    a = review_ref(issue(2), "comment", "branch")
    assert a == review_ref(issue(2), "comment", "branch")
    assert a != review_ref(issue(2), "edited comment", "branch")  # a stale Approve card can't approve an edited comment
    assert a != review_ref(issue(2), "comment", "other-branch")
    assert a != review_ref(issue(3), "comment", "branch")
