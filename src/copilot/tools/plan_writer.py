"""Build the personalised contribution plan (markdown) and save it under plans/ (gitignored)."""

import difflib
import re
from pathlib import Path

from copilot.config import ROOT
from copilot.logic.ranking import RankedIssue
from copilot.logic.review import shorten
from copilot.models import StepInfo

PLANS_DIR = ROOT / "plans"

_SETUP = [  # (marker file, install, test)
    ("package.json", "npm install", "npm test"),
    ("pyproject.toml", "python -m venv .venv && pip install -e .", "pytest"),
    ("requirements.txt", "python -m venv .venv && pip install -r requirements.txt", "pytest"),
    ("setup.py", "python -m venv .venv && pip install -e .", "pytest"),
    ("Cargo.toml", "cargo build", "cargo test"),
    ("go.mod", "go build ./...", "go test ./..."),
    ("pom.xml", "mvn -q compile", "mvn test"),
    ("build.gradle", "./gradlew build", "./gradlew test"),
    ("Gemfile", "bundle install", "bundle exec rake test"),
    ("composer.json", "composer install", "vendor/bin/phpunit"),
]
_PATH = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|js|jsx|ts|tsx|md|rs|go|java|rb|php|c|cc|cpp|h|cs|kt|swift|json|ya?ml|toml|html|css))\b")


def setup_commands(root_files: list[str]) -> tuple[str, str] | None:
    names = set(root_files)
    for marker, install, test in _SETUP:
        if marker in names:
            return install, test
    return None


def mentioned_paths(text: str, limit: int = 5) -> list[str]:
    return list(dict.fromkeys(m for m in _PATH.findall(text) if "/" in m or "." in m))[:limit]


def resolve_paths(paths: list[str], root_files: list[str]) -> list[str]:
    """Issues often misname files (or the repo does). Match top-level names against the real listing."""
    shown = []
    for p in paths:
        if not root_files or "/" in p or p in root_files:
            shown.append(f"`{p}`")
            continue
        close = difflib.get_close_matches(p, root_files, n=1, cutoff=0.75)
        shown.append(f"`{close[0]}` (the issue says `{p}`)" if close else f"`{p}`")
    return shown


def build_plan(r: RankedIssue, steps: list[StepInfo], *, branch: str, comment: str, fork_full_name: str,
               dry_run: bool) -> str:
    c, v = r.candidate, r.verification
    default = v.default_branch if v else "main"
    fork = fork_full_name or f"<your-github-username>/{c.repo.split('/')[1]}"
    owner, name = fork.split("/")  # the fork can be renamed by GitHub (e.g. repo-1) if the name was taken
    sign = " -s" if v and any("signed off" in n for n in v.contributing_notes) else ""
    lines = [f"# Contribution plan: {c.repo} #{c.number}", "", f"**{c.title}**  ", c.url, ""]
    if r.reason:
        lines += [f"Why it fits you: {r.reason}", ""]
    lines += ["## " + ("What I would do (DRY RUN, nothing was written to GitHub)" if dry_run else "What I did for you")]
    for s in steps:
        mark = {"ok": "done", "simulated": "would do", "failed": "FAILED", "skipped": "skipped"}.get(s.status, s.status)
        lines.append(f"- **{s.name}** ({mark}): {s.detail}" + (f" {s.url}" if s.url else ""))
    lines += ["", "Claim comment text:", "", *(f"> {ln}" for ln in comment.splitlines()), ""]

    lines += ["## Before you start"]
    lines.append("- Wait for the maintainer's reply to your comment if you can; replies often take a few days.")
    if v and v.has_contributing:
        lines.append(f"- Read `{v.contributing_path}` in the repo.")
        lines += [f"- {n}" for n in v.contributing_notes]
    else:
        lines.append("- This repo has no CONTRIBUTING file, so keep your change small and follow the existing code style.")
    lines += [""]

    lines += ["## Steps", f"1. Clone your fork: `git clone https://github.com/{fork}.git && cd {name}`",
              f"2. Switch to your branch: `git checkout {branch}`",
              f"3. Stay in sync with the original: `git remote add upstream https://github.com/{c.repo}.git && "
              f"git fetch upstream && git rebase upstream/{default}`"]
    cmds = setup_commands(v.root_files) if v else None
    if cmds:
        lines.append(f"4. Install and run the tests before changing anything: `{cmds[0]}` then `{cmds[1]}`")
    else:
        lines.append("4. Look for setup and test instructions in the README, and run the tests before changing anything.")
    paths = mentioned_paths(f"{c.title}\n{c.body_excerpt}")  # the file is often named in the title alone
    where = (", ".join(resolve_paths(paths, v.root_files if v else []))
             if paths else "search the codebase for the names used in the issue title")
    lines += [f"5. Where to edit: {where}",
              f"6. Commit: `git add -A && git commit{sign} -m \"{shorten(c.title, 50)} (#{c.number})\"`",
              f"7. Push: `git push -u origin {branch}`",
              "8. Open the pull request. Easiest: once your fix is pushed, type `open pr` in this chat; I check that your "
              "branch has your commit(s), show you the description, and open it only after you approve. Or do it yourself: "
              f"https://github.com/{c.repo}/compare/{default}...{owner}:{branch}"]
    if v and v.pr_template:
        lines.append(f"   Fill in the repo's PR template (`{v.pr_template}`) and write `Fixes #{c.number}` in the description.")
    else:
        lines.append(f"   Write `Fixes #{c.number}` in the description so the issue closes when it is merged.")
    lines += ["9. Answer review comments politely and push follow-up commits to the same branch.", ""]
    return "\n".join(lines)


def write_plan(text: str, repo: str, number: int, directory: Path = PLANS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{repo.replace('/', '__')}__{number}.md"
    path.write_text(text, encoding="utf-8")
    return path
