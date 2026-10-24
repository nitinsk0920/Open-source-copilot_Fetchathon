# Analyst (worker of the Open-Source Contribution Copilot)

Takes candidate GitHub issues and **verifies each one live** before anything is shown to a contributor:

- repository is not archived and has been pushed to recently
- the issue is still open, unlocked and unassigned
- nobody has commented "can I work on this?" / "I'll take it" recently
- no open pull request already references the issue
- reads the repo's CONTRIBUTING file (CLA, sign-off, "ask first" rules) and PR template
- drops non-English issues by default

Every dropped issue comes back with the reason. Survivors are ranked against the contributor's languages, level and time budget by the ASI:One LLM, with a short reason each.

This is an internal worker. Talk to the **Open-Source Contribution Copilot** (Orchestrator) agent instead.
