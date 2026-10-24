# Action (worker of the Open-Source Contribution Copilot)

The only agent that writes to GitHub, and only **after the contributor's explicit approval** in chat:

1. forks the repository into the contributor's account
2. posts a polite claim comment on the issue
3. creates a starter branch in the fork
4. later, once the contributor has pushed their own fix: checks (read-only) that the branch really has their commit(s) and, after a second approval, opens the pull request

Safety rules: it only accepts requests from the Orchestrator, requires an approval id, and while demo mode is on it performs live writes only on an allowlisted test repository (anything else is a dry run that reports what would happen). Each step reports its own result, so a failed step can be retried without repeating the others. It never writes code, never merges, and never opens a second pull request for the same branch.

This is an internal worker. Talk to the **Open-Source Contribution Copilot** (Orchestrator) agent instead.
