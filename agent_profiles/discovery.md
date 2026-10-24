# Discovery (worker of the Open-Source Contribution Copilot)

Searches GitHub for open, unassigned, recently updated issues labelled for beginners, in the languages you ask for, and returns a diverse candidate list (at most two issues per repository).

This is an internal worker. To find a contribution, talk to the **Open-Source Contribution Copilot** (Orchestrator) agent; it calls this agent, then verifies every candidate live before showing you anything.

Receives `SearchRequest` (languages, labels, recency) and returns `SearchResponse` (candidates, queries used, warnings).
