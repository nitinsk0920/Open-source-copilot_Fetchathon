"""Merge per-language search results into one diverse candidate list. Pure."""

from copilot.models import IssueCandidate


def select_candidates(
    per_language: list[list[IssueCandidate]], max_results: int = 20, per_repo_cap: int = 2
) -> tuple[list[IssueCandidate], int]:
    """Dedupe, cap issues per repo, and interleave languages round-robin.

    Returns (selected, total_found) where total_found counts unique issues before the cap/truncation.
    """
    seen: set[tuple[str, int]] = set()
    unique_lists: list[list[IssueCandidate]] = []
    for results in per_language:
        unique: list[IssueCandidate] = []
        for c in results:
            key = (c.repo.lower(), c.number)
            if key not in seen:
                seen.add(key)
                unique.append(c)
        unique_lists.append(unique)
    total_found = len(seen)

    per_repo: dict[str, int] = {}
    selected: list[IssueCandidate] = []
    index = 0
    while len(selected) < max_results and any(index < len(u) for u in unique_lists):
        for unique in unique_lists:
            if index < len(unique) and len(selected) < max_results:
                c = unique[index]
                repo_key = c.repo.lower()
                if per_repo.get(repo_key, 0) < per_repo_cap:
                    per_repo[repo_key] = per_repo.get(repo_key, 0) + 1
                    selected.append(c)
        index += 1
    return selected, total_found
