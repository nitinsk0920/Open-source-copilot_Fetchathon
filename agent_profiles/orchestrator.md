# Open-Source Contribution Copilot

Turns "I want to contribute to open source" into a **live-verified** first issue that fits your skills, then helps you get set up to work on it.

## What it does
1. Asks about your languages, experience level and time budget.
2. Searches GitHub for beginner-friendly issues and **checks each one live**: is the repo still active, has someone already claimed the issue, is a PR already open, what do the contribution rules say. Dead or taken issues are dropped, with the reason shown.
3. Shows a short ranked list matched to your skills.
4. After you pick one and **explicitly approve**, forks the repo, posts a polite claim comment and creates a starter branch.
5. Gives you a personalised contribution plan: clone, branch, run tests, where to edit, open the PR.
6. After you push your own fix, type `open pr`: it checks your branch, shows the pull request, and opens it once you approve.

Nothing is written to GitHub until you approve.

## Try these prompts
- `Find me a beginner Python issue I can finish in a weekend`
- `I know JavaScript and React, about 5 hours a week, what can I contribute to?`
- `I want a good first issue for my GSoC application, I use Rust`
- `pick 2`, `approve`

## Notes
- Demo mode: live write actions run only against an allowlisted test repository. Other issues get a dry run showing exactly what would happen.
- Built with uAgents and the Agent Chat Protocol, using the ASI:One LLM.
