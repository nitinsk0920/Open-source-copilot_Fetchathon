from copilot.agents.analyst import analyze
from copilot.logic.ranking import ranked_from_json, dropped_from_json
from copilot.logic.review import approval_marker, branch_name, clean_comment, default_comment, shorten
from copilot.models import AnalyzeRequest, Issue, IssueCandidate, StepInfo, Verification
from copilot.logic.ranking import RankedIssue
from copilot.tools.plan_writer import build_plan, mentioned_paths, setup_commands, write_plan
from tests.test_llm import FakeLLM
from tests.test_verification import NOW, FakeGH, comment


def issue(n: int = 12, title: str = "Fix typo in README!", body: str = "") -> Issue:
    return Issue(repo="acme/widget", number=n, title=title, url=f"https://github.com/acme/widget/issues/{n}", body_excerpt=body)


def test_branch_and_comment_text():
    assert branch_name(issue()) == "fix-issue-12-fix-typo-in-readme"
    assert branch_name(issue(title="!!!")) == "fix-issue-12-change"
    long = branch_name(issue(title="test(licenses): cover license classification and metadata fallbacks"))
    assert long == "fix-issue-12-test-licenses-cover-license" and not long.endswith("-")
    assert branch_name(issue(n=2, title="Fix typo in error message in utils.js")) == "fix-issue-2-fix-typo-in-error-message"
    assert shorten("cover license classification and metadata fallbacks", 50) == "cover license classification and metadata"
    assert shorten("short", 50) == "short"
    with_guide = default_comment(issue(), Verification(has_contributing=True, contributing_path="CONTRIBUTING.md"))
    assert "CONTRIBUTING.md" in with_guide and "pull request" in with_guide
    assert "CONTRIBUTING" not in default_comment(issue(), None)


def test_user_comment_cannot_forge_the_marker():
    assert clean_comment("  hello <!-- oss-copilot:x --> there ") == "hello  oss-copilot:x  there"
    assert clean_comment("   ") is None and len(clean_comment("a" * 5000)) == 2000
    assert approval_marker("abc") == "<!-- oss-copilot:abc -->"


def test_setup_and_paths():
    assert setup_commands(["README.md", "package.json"]) == ("npm install", "npm test")
    assert setup_commands(["Cargo.toml"])[1] == "cargo test" and setup_commands(["README.md"]) is None
    assert mentioned_paths("Edit src/utils/parser.py and README.md, see https://x.io/a.html") == ["src/utils/parser.py", "README.md"]


def test_plan_has_every_section_and_respects_dry_run(tmp_path):
    v = Verification(default_branch="develop", root_files=["pyproject.toml"], has_contributing=True,
                     contributing_path="CONTRIBUTING.md", contributing_notes=["Commits must be signed off (use `git commit -s`)"],
                     pr_template=".github/pull_request_template.md")
    r = RankedIssue(candidate=issue(body="Typo in docs/guide.md"), reason="Small docs fix", verification=v)
    steps = [StepInfo(name="fork", status="ok", detail="fork is me/widget", url="https://github.com/me/widget")]
    plan = build_plan(r, steps, branch="fix-issue-12-x", comment="Hi!", fork_full_name="me/widget", dry_run=False)
    for expected in ["# Contribution plan: acme/widget #12", "What I did for you", "git clone https://github.com/me/widget.git",
                     "git checkout fix-issue-12-x", "upstream/develop", "pip install -e .", "`docs/guide.md`",
                     "git commit -s", "compare/develop...me:fix-issue-12-x", "Fixes #12", "pull_request_template.md",
                     "type `open pr` in this chat", "> Hi!"]:
        assert expected in plan, expected
    title_only = RankedIssue(candidate=issue(title="Fix typo in error message in utils.js", body="One-line fix."), verification=v)
    assert "`utils.js`" in build_plan(title_only, steps, branch="b", comment="c", fork_full_name="me/widget", dry_run=False)
    renamed = build_plan(r, steps, branch="b", comment="c", fork_full_name="me/widget-1", dry_run=False)
    assert "git clone https://github.com/me/widget-1.git && cd widget-1" in renamed
    dry = build_plan(r, steps, branch="b", comment="c", fork_full_name="", dry_run=True)
    assert "DRY RUN" in dry and "<your-github-username>/widget" in dry
    path = write_plan(plan, "acme/widget", 12, tmp_path)
    assert path.name == "acme__widget__12.md" and path.read_text(encoding="utf-8") == plan


def cand(n: int, title: str = "Fix typo", repo: str = "a/b") -> IssueCandidate:
    return IssueCandidate(repo=repo, number=n, title=title, url="u", labels=["good first issue"])


async def test_analyst_filters_verifies_ranks_and_explains_drops():
    class GH(FakeGH):
        async def get_issue_comments(self, repo, n):
            return [comment("Can I work on this?", 2)] if n == 2 else []

    req = AnalyzeRequest(request_id="r", languages=["python"], level="beginner", candidates=[
        cand(1), cand(2), cand(3, "バグを直す"), cand(4)])
    resp = await analyze(GH(), None, req, NOW)
    ranked, dropped = ranked_from_json(resp.ranked_json), dropped_from_json(resp.dropped_json)
    assert resp.ok and resp.considered == 4 and resp.verified == 2
    assert sorted(r.candidate.number for r in ranked) == [1, 4] and all(r.verification for r in ranked)
    reasons = {d.number: (d.stage, d.reason) for d in dropped}
    assert reasons[3][0] == "language" and "Japanese" in reasons[3][1]
    assert reasons[2][0] == "verification" and "already claimed" in reasons[2][1]


async def test_analyst_stops_when_rate_limited_and_says_so():
    from copilot.tools.http import RateLimitError

    resp = await analyze(FakeGH(error=RateLimitError(403, "slow", 60)), None,
                         AnalyzeRequest(request_id="r", languages=["python"], candidates=[cand(1), cand(2)]), NOW)
    assert resp.ok and resp.verified == 0 and "rate limit" in resp.warnings[0]
