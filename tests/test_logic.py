from datetime import date

from copilot.formatting import format_none_survived, format_ranked, format_review, format_search_failure
from copilot.logic.intent import UserProfile, quick_languages
from copilot.logic.query_builder import WIDENED_LABELS, build_issue_query
from copilot.logic.ranking import RankedIssue
from copilot.logic.selection import select_candidates
from copilot.models import Dropped, Issue, IssueCandidate, SearchResponse, Verification


def test_issue_and_wire_model_have_the_same_fields():
    assert set(Issue.model_fields) == set(IssueCandidate.__fields__)
    assert Issue.from_candidate(IssueCandidate(repo="a/b", number=1, title="t", url="u", labels=["x"])).labels == ["x"]


def cand(repo: str, number: int, lang: str = "python") -> IssueCandidate:
    return IssueCandidate(repo=repo, number=number, title="t", url="u", language=lang, updated_at="2026-10-20T00:00:00Z")


def test_query_basic():
    q = build_issue_query("Python", recency_days=30, today=date(2026, 10, 24))
    assert q == 'is:issue is:open no:assignee archived:false language:python label:"good first issue" updated:>=2026-09-24'


def test_query_labels_are_or_and_quoted():
    q = build_issue_query("c++", WIDENED_LABELS, today=date(2026, 10, 24))
    assert 'label:"good first issue","help wanted",beginner,easy,first-timers-only' in q
    assert "language:c++" in q


def test_query_without_language():
    assert "language:" not in build_issue_query(None, today=date(2026, 10, 24))


def test_selection_dedupes_caps_and_interleaves():
    py = [cand("a/x", 1), cand("a/x", 2), cand("a/x", 3), cand("b/y", 1)]
    js = [cand("c/z", 1, "javascript"), cand("b/y", 1, "javascript")]
    selected, total = select_candidates([py, js], max_results=10, per_repo_cap=2)
    assert total == 5  # b/y#1 counted once
    assert [(c.repo, c.number) for c in selected] == [("a/x", 1), ("c/z", 1), ("a/x", 2), ("b/y", 1)]


def test_selection_truncates():
    selected, total = select_candidates([[cand(f"r/{i}", 1) for i in range(30)]], max_results=20)
    assert len(selected) == 20 and total == 30


def test_selection_empty():
    assert select_candidates([[], []]) == ([], 0)


def test_quick_languages():
    assert quick_languages("beginner Python issue") == ["python"]
    assert quick_languages("I know JS and TypeScript") == ["javascript", "typescript"]
    assert quick_languages("I want to go ahead with it") == []
    assert quick_languages("Learning Go and C++") == ["c++", "go"]
    assert quick_languages("java") == ["java"]


def drop(repo: str, n: int, reason: str, stage: str = "verification") -> Dropped:
    return Dropped(repo=repo, number=n, title="t", reason=reason, stage=stage)


def test_format_ranked_shows_reasons_checks_and_filtered():
    profile = UserProfile(languages=["python"], level="beginner", time_budget_hours=8)
    ranked = [RankedIssue(candidate=Issue.from_candidate(cand("a/x", 1)), fit=9, difficulty="easy", est_hours=2,
                          reason="Small docs fix", ai_ranked=True,
                          verification=Verification(checks=["repo active", "no claim comments"], notes=["old claim"]))]
    dropped = [drop("b/y", 2, "already claimed: @bob asked 3 days ago")]
    text = format_ranked(profile, ranked, 5, dropped, ["search for go failed (boom)"], widened=True)
    assert "**a/x** #1" in text and "Why: Small docs fix" in text and "fit 9/10" in text
    assert "Verified live: repo active, no claim comments" in text and "Heads-up: old claim" in text
    assert "Filtered out (1 of 5)" in text and "b/y #2: already claimed" in text
    assert "search for go failed" in text and "after widening" in text and "4 survived" in text


def test_format_ranked_without_ai_says_so():
    ranked = [RankedIssue(candidate=Issue.from_candidate(cand("a/x", 1)))]
    assert "AI ranking was unavailable" in format_ranked(UserProfile(languages=["python"]), ranked, 1, [], [])


def test_format_none_survived_and_failure():
    profile = UserProfile(languages=["python"])
    assert "no open" in format_none_survived(profile, 0, [], [])
    text = format_none_survived(profile, 3, [drop("b/y", 2, "open pull request #9 already references this issue")], [])
    assert "none passed live verification" in text and "open pull request #9" in text
    assert "couldn't search" in format_search_failure(SearchResponse(request_id="r", ok=False, error="x"))


def test_format_review_live_and_dry_run():
    r = RankedIssue(candidate=Issue.from_candidate(cand("a/x", 7)), reason="Small",
                    verification=Verification(default_branch="develop"))
    live = format_review(r, "Hi!\nCan I?", "fix-issue-7-t", live=True)
    assert "LIVE" in live and "> Hi!" in live and "> Can I?" in live
    assert "`fix-issue-7-t`" in live and "from `develop`" in live and "Nothing has been changed yet" in live
    assert "DRY RUN" in format_review(r, "c", "b", live=False)
