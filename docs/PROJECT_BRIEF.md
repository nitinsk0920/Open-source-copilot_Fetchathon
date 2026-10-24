# Project Brief: Open-Source Contribution Copilot

> **Status:** this is the original brief the project was planned from, kept as a design record. The system is built;
> `README.md` describes the final design. Where the brief and the README differ (for example the sub-agent
> architecture in section 4, and which bonus features were built), the README is current.

---

## 1. Context

I am participating in the **ASI:One Agent Challenge – From Intent to Action** hackathon (Fetch.ai ecosystem). The goal is an AI agent (or multi-agent system) that is **discoverable through ASI:One**, understands a user's intent, and **takes real action**, not just chat. It must be demonstrable **entirely inside an ASI:One conversation**, with no custom frontend.

The event gave me free credits for **ASI:One** (LLM API) and **Agentverse**.

## 2. The problem we are solving

Beginners want to contribute to open source but rarely complete a first contribution, because:
1. Searching "good first issue" returns thousands of results, and many are dead (inactive repo, already claimed, PR already open, mislabeled as easy).
2. They can't judge whether an issue fits their skills or is doable in their time.
3. They stall on setup: reading contribution rules, forking, branching, writing a polite claim comment.
4. They lose track once the maintainer replies days later.

**Our agent turns "I want to contribute" into a live-verified issue, matched to the user's skills, with the setup actions done for them.**

**Target users:** students and early-career developers who want a real open-source contribution (resume, GSoC, Hacktoberfest).

**Outcome produced:** (a) a short ranked list of live-verified issues, (b) after user approval: a forked repo, a posted claim comment, and a starter branch, (c) a personalized contribution plan file, (d) later, a notification when the maintainer replies.

**Why this is more than a chatbot wrapper (important for judging):** an ordinary chat AI with web search can *find* issues. Ours must differ by (1) **live verification** (last commit date, claimed in comments, linked/open PRs, CONTRIBUTING rules), (2) **real side-effecting actions** on GitHub, (3) **multi-agent orchestration**, (4) **recovery** when a candidate fails verification, and (5) **follow-up monitoring**. Do not let the design collapse into "send text to an LLM and return the answer."

## 3. Hackathon rules and requirements (must all be satisfied)

### Mandatory (for prize eligibility)
- Register **at least one agent on Agentverse**.
- Implement the **Agent Chat Protocol (ACP)**.
- Be **discoverable and directly usable through ASI:One**.
- Demonstrate **meaningful tool execution or multi-agent orchestration**.
- Complete the **primary user workflow entirely within an ASI:One conversation** (no custom frontend).
- Submit a **public GitHub repository** with instructions to run or test.

### Bonus (aim for these where feasible)
- Effective **multi-agent collaboration**.
- **Payment Protocol** with a credible monetization model (idea: premium deep-analysis or mentorship-plan tier; testnet only).
- **ASI Interactive Cards** (issue carousel, skills form, review/approve screen) for guided interaction inside ASI:One.
- Strong **reliability, error handling, recovery** from failed tool calls.
- Creative use of **real-time data** and external services.
- An agent that could **keep operating after the hackathon**.

### Deliverables (keep in mind from day one)
- Public **ASI:One shared chat URL** showing the complete workflow.
- **Agentverse agent profile URL(s)** for every submitted agent.
- **Public GitHub repo** (README with setup, env vars, how to test via ASI:One).
- **Short demo video** (~2 min).
- Short description: problem, target users, outcome.

## 4. Platform facts (starting assumptions, verified against current docs while building)

These were the starting assumptions, each checked against the official docs (Fetch.ai uAgents, Agentverse, Innovation Lab, ASI:One) while building. Where they differed, the implementation follows the docs: for example `uagents` 0.26.1 instead of the pinned 0.23.6, and four standalone agents instead of a single Bureau, because the Agentverse Inspector cannot connect a mailbox for Bureau-hosted agents.

### uAgents + Chat Protocol + Agentverse
- Framework: **uAgents** (Python). Chat protocol imports:
  ```python
  from uagents import Agent, Context, Protocol
  from uagents_core.contrib.protocols.chat import (
      ChatMessage, ChatAcknowledgement, TextContent, EndSessionContent, chat_protocol_spec
  )
  ```
- Typical agent setup seen in official examples:
  ```python
  chat_proto = Protocol(spec=chat_protocol_spec)
  agent = Agent(name="...", seed="<secret seed from env>", port=8000,
                mailbox=True, publish_agent_details=True)
  # handlers: @chat_proto.on_message(ChatMessage) -> send ChatAcknowledgement, then reply ChatMessage
  agent.include(chat_proto, publish_manifest=True)
  agent.run()
  ```
- `publish_manifest=True` publishes the protocol manifest to Agentverse, which is what makes ASI:One able to discover and talk to the agent.
- **Mailbox mode** (`mailbox=True`): a locally running agent polls Agentverse for queued messages, so no public IP / ngrok is needed. The agent process **must be running** for it to respond. Alternative: expose a stable public HTTPS endpoint.
- One example repo pinned `uagents==0.23.6` and `uagents-core==0.4.1`; confirm current compatible versions.
- Agentverse flow: run the agent, open the inspector link it prints, connect the **Mailbox**, then open the agent profile on Agentverse and test via ASI:One. Fill in the agent's profile (name, description, README) properly; discoverability depends on it.
- Alternative route: **Agentverse SDK** (framework adapters that wrap existing apps and add ACP). Hosted agents on Agentverse are also possible but more restricted in packages. **Default decision: local uAgents with mailbox.**
- **Open question at the time (resolved: four standalone agents, each with its own mailbox and profile):** how sub-agents (Discovery, Analyst, Action) should talk to the Orchestrator. Options: each is its own uAgent registered on Agentverse (needs each to be reachable, mailbox or local endpoints) vs. a single process hosting several agents (e.g., `Bureau`). Research the officially supported pattern and pick the one that keeps the **multi-agent bonus** while staying reliable for a live demo. Every submitted agent needs its own Agentverse profile URL.

### ASI:One API (the LLM)
- It is an **OpenAI-compatible LLM API**; use the `openai` Python package with a custom base URL and my key. Confirm the exact base URL and request format in the ASI:One docs.
- Models listed in the dashboard: `asi1-mini` (fast/cheap, use for simple parsing), `asi1` (default reasoning), `asi1-ultra` (hard planning only if needed).
- Key lives in `.env` as `ASI1_API_KEY`. Using ASI:One's own LLM is optional but preferred since credits are provided.
- Do not confuse: **ASI:One chat** (asi1.ai, where users talk to agents) vs. **ASI:One API** (LLM our agent calls).

### ASI Interactive Cards (bonus)
- Docs: https://innovationlab.fetch.ai/resources/docs/interactive-cards/asi-interactive-cards
- I have **not** verified the format. Read the docs, then design: a skills/preferences form, an issue carousel with "Pick this one", and an approval/review card (Approve / Edit comment). Fall back gracefully to plain text if cards fail.

### Payment Protocol (bonus)
- Not verified. Check Innovation Lab docs for the current API. Testnet only. Keep it optional and isolated so it can't break the core flow.

### GitHub REST API
- Free. Auth with a personal access token (`Authorization: Bearer`, `Accept: application/vnd.github+json`).
- Rate limits: **unauthenticated 60 req/hr; authenticated 5,000 req/hr**. **Search API: 30 req/min authenticated (10 unauthenticated), a separate budget.** There are also **secondary limits** on concurrent requests, so run Analyst checks in small batches, not all at once.
- Handle 403/429 with backoff; cache results for a few minutes (helps repeated demo runs).
- Action calls: `POST /repos/{o}/{r}/forks` (async, returns 202; poll until the fork exists), `POST /repos/{o}/{r}/issues/{n}/comments` (on the **original** repo), branch via `GET .../git/ref/heads/{default}` then `POST /repos/{me}/{r}/git/refs` (on the **fork**).
- Token scope: a classic PAT with `public_repo` is simplest; verify whether fine-grained tokens can cover fork + comment + ref creation before choosing.

## 5. Proposed architecture (as planned; see README.md for the final design)

```
User (ASI:One chat)
   | ChatMessage (ACP)
   v
Orchestrator agent   <- the only agent the user talks to; uses ASI:One LLM
   |-> Discovery agent -> GitHub Search API
   |-> Analyst agent   -> GitHub repo/issue/timeline/contents APIs + LLM scoring
   `-> Action agent    -> GitHub fork / comment / branch APIs (only after approval)
```

- **Orchestrator:** parse intent (skills, level, time budget) with the LLM; ask for missing info; call Discovery then Analyst; present ranked picks; **hold at an approval gate**; call Action; return results and the plan file. Owns session state and retries.
- **Discovery:** build search query (language, `good first issue`/`help wanted` style labels, `state:open`, `no:assignee`, `is:issue`, recently updated); return ~20 candidates.
- **Analyst:** for each candidate, verify live: repo last push date and activity, whether the issue is already claimed in comments/timeline, linked or open PRs, presence/contents of CONTRIBUTING.md; then use the LLM to score difficulty and fit against the user's skills and write a short reason. Drop failures and report *why* each was dropped (this is a demo highlight).
- **Action:** after explicit approval only: fork, post claim comment, create branch, generate plan file.
- **Follow-up (stretch):** poll the claimed issue and notify the user when a maintainer replies.

### User flow (user does only four things: ask, pick, approve, then code)
1. User states skills/level/time in ASI:One.
2. Agents search + verify; user sees a ranked list (with filtered-out counts and reasons).
3. User picks one.
4. Review screen shows exactly what will happen (fork, comment text, branch name). User **Approves** or edits the comment.
5. Agent executes, then returns links (fork, comment, branch) and a **contribution plan file**: clone fork, checkout branch, install and run tests, where to edit, commit/push, open PR with the repo's template, link `Fixes #N`.
6. User codes in VS Code and opens the PR themselves (agent opening the PR is an optional stretch).

## 6. Hard rules for the implementation

**Safety / correctness**
- **Never perform any write action (fork, comment, branch) before explicit user approval** in the chat.
- **Demo and test only against a test repo I own or a second GitHub account.** Never spam real maintainers. Make the target configurable (`DEMO_MODE`/allowlist) so live actions are restricted to safe repos by default.
- Secrets (`ASI1_API_KEY`, `GITHUB_TOKEN`, `AGENT_SEED_PHRASE`, Agentverse key) live in `.env`; commit `.env.example` only; `.env` is in `.gitignore`. Never log tokens.
- Single shared token is acceptable for the hackathon; document that a real product would use per-user GitHub OAuth.

**Reliability (judged)**
- Timeouts and retries with backoff on every external call; handle 403/429/5xx, empty search results, LLM failures, fork-not-ready, and partial failure (e.g., fork OK but comment fails: report exactly which step failed and retry only that step).
- Every user-facing path ends with a clear message; never leave a chat hanging. Send `ChatAcknowledgement` for each received message.
- If all candidates fail verification, widen the search or ask the user to relax constraints.
- Structured logging of each agent step (useful for the demo video).

**Code quality**
- Python 3.10+, type hints, small modules, config via env. Tools (GitHub client, LLM client) isolated from agent/protocol code so they are unit-testable.
- Add unit tests for the GitHub client and the verification/scoring logic (mock HTTP).
- Do not hardcode seed phrases or tokens. Do not commit generated plan files containing secrets.

## 7. Suggested repo layout (as planned; the real layout is in README.md)

```
.
├── docs/PROJECT_BRIEF.md     # this file
├── README.md                 # setup, env vars, run, how to test via ASI:One, limitations
├── .env.example
├── .gitignore
├── requirements.txt (or pyproject.toml)
├── src/
│   ├── agents/ (orchestrator.py, discovery.py, analyst.py, action.py)
│   ├── tools/  (github_client.py, llm_client.py, plan_writer.py)
│   ├── models.py             # message models between agents
│   ├── cards.py              # Interactive Card builders (+ text fallback)
│   └── config.py
├── tests/
└── scripts/                  # run-all, register helpers
```

## 8. Build order (milestones, each reviewed before moving on)

1. **Hello-world uAgent** with the chat protocol, mailbox connected, visible on Agentverse, replying inside ASI:One. Prove discovery before anything else.
2. Add the **GitHub search** inside it and return real issues.
3. Add the **ASI:One LLM** to parse the user message and rank results.
4. Split into **Orchestrator / Discovery / Analyst / Action** with agent-to-agent messaging; register each on Agentverse.
5. Add **Analyst verification** + the re-rank/recovery path.
6. Add the **approval gate + Action agent** (test repo only) and the **plan file** output.
7. Add **Interactive Cards**, then the **Payment Protocol** (optional), then follow-up monitoring (stretch).
8. Error-handling pass, tests, **README**, demo script, final checks against Section 3.

## 9. Definition of done (checklist)

- [ ] All agents registered on Agentverse with complete profiles; URLs recorded in README
- [ ] ACP implemented; agent reachable and usable from ASI:One
- [ ] Full primary workflow works in one ASI:One conversation (request, ranked list, pick, approve, actions, plan file)
- [ ] Live verification visibly filters bad issues, with reasons
- [ ] Write actions gated by approval and restricted to safe repos in demo mode
- [ ] Failure paths handled and demonstrable
- [ ] Public GitHub repo, README with run/test instructions and known limitations
- [ ] Public ASI:One shared chat URL + ~2 min demo video + short write-up (problem, users, outcome)
