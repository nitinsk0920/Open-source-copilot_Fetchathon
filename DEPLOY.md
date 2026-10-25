# Running the agents headless

The agents are normal Python processes that call out to Agentverse (mailbox polling), GitHub and ASI:One. **No inbound
port or public IP is needed.** Run one copy on any always-on machine.

## What you need on the host

- Python 3.10+ (developed on 3.12), git, outbound HTTPS.
- The repo plus a `.env` (copy `.env.example`; see the README for each variable). Treat `.env` like a password file:
  it holds the agent seeds, the GitHub token and the LLM key.
- **The same seeds you used before.** The seed decides the agent's address and its Agentverse mailbox. New seeds mean
  new agents that need new Inspector connections and new profiles.

```bash
git clone <your repo> && cd <your repo>
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt && pip install -e .
# create .env with your values, then:
python scripts/run_all.py
```

## What `run_all.py` does when nobody is watching

- Starts Discovery, Analyst, Action and Orchestrator as separate processes; needs no terminal or stdin and writes one
  prefixed, timestamped line per log line to stdout.
- If an agent crashes it is restarted after a backoff (2s, 5s, 15s, 30s, 60s); the others keep running. More than 5
  crashes of one agent within 10 minutes stops everything and exits with code 1, so your supervisor can alert.
- `SIGTERM`, `SIGINT` (Ctrl+C) and, on Windows, Ctrl+Break stop all agents cleanly (exit code 0).
- It finds the code itself (working directory and `PYTHONPATH` are set), so it can be started from any directory.

First start on a **new** machine or with new seeds: open each agent's Inspector link from the log and click
**Connect -> Mailbox** once. Restarting the same agents afterwards needs nothing.

## Keep it running

**Linux (systemd)**, `/etc/systemd/system/oss-copilot.service`:

```ini
[Unit]
Description=Open-Source Contribution Copilot agents
After=network-online.target
Wants=network-online.target

[Service]
User=copilot
WorkingDirectory=/opt/oss-copilot
ExecStart=/opt/oss-copilot/.venv/bin/python scripts/run_all.py
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now oss-copilot
journalctl -u oss-copilot -f
```

**Windows:** run `python scripts\run_all.py` under a service wrapper such as NSSM (Application = the venv's
`python.exe`, arguments = `scripts\run_all.py`, startup directory = the repo), or as a Task Scheduler task at startup
that restarts on failure.

**Docker** (minimal): a `python:3.12-slim` image that copies the repo, runs `pip install -r requirements.txt && pip
install -e .`, and has `CMD ["python", "scripts/run_all.py"]`. Pass the variables with `--env-file .env` (do not bake
`.env` into the image) and mount a volume at `/app/.state` if you want conversations to survive container restarts.

## Render (free web service)

This is how the submitted demo is hosted. The agents only call out, but Render's free tier needs a web service that answers
on a port, and it **sleeps a service after about 15 minutes without web visits**, so two extra pieces are needed.

| Setting | Value |
|---|---|
| Type | Web Service (a Background Worker is not available on the free plan) |
| Runtime | Python 3 |
| Build command | `pip install -r requirements.txt && pip install -e .` |
| Start command | `python scripts/run_all.py` |
| Instance type | Free |

Environment variables (set in Render's dashboard, never in the repo):
`PYTHON_VERSION=3.12.8`, `ORCHESTRATOR_PORT=10000` (Render's default web port, so it sees the service as alive), the four
`*_SEED_PHRASE` values (identical to the ones used before, so the agent addresses and mailboxes carry over),
`ASI1_API_KEY`, `GITHUB_TOKEN`, `DEMO_MODE=true`, `ALLOWED_TARGET_REPOS`.

- **Keep it awake.** Add a free uptime monitor (for example UptimeRobot) that requests
  `https://<your-service>.onrender.com/submit` every 5 minutes; it answers `OK - Agent is running`. The `/` page returns 404 on
  purpose, so monitor `/submit`.
- **Cut over without a gap.** Deploy, wait for four `started` lines and no `mailbox not found` in the log, **then** stop any other
  copy (your PC), then send a test message. Two copies with the same seeds split the messages.
- **Watch memory.** The four processes share the free instance's 512 MB. If the service is killed and restarts in a loop,
  move up an instance size or run locally again.
- **Restarts lose chat history** (the disk is temporary), not the agents.
- A single `503 upstream connect error` from the Agentverse mailbox in the log is transient; the agent retries by itself.

## Operating notes

- **Never run two copies with the same seeds.** Mailbox messages would be split between them.
- **Down time is tolerated:** while the process is stopped, messages queue in the Agentverse mailbox and are handled
  when it comes back. Users still see nothing until then, so keep it supervised.
- **State:** conversations are kept in memory and mirrored to `.state/sessions.json`; contribution plans go to
  `plans/`. Both are git-ignored. Losing them only loses chat history, not agent identity.
- **Quick health check:** `curl http://127.0.0.1:8000/submit` (ports 8000-8003) answers `OK - Agent is running`. A real
  end-to-end check that uses your mailboxes and is read-only (no GitHub writes):
  `python scripts/local_e2e.py --running`.
- **Safety defaults:** keep `DEMO_MODE=true` and `ALLOWED_TARGET_REPOS` set to your own test repo. Anything else is a
  dry run. Use a throwaway GitHub account for `GITHUB_TOKEN` and rotate it if it ever leaks.
- **Updating:** `git pull && pip install -r requirements.txt && pip install -e .`, then restart the service. Run
  `pytest` first if you changed code.
- **GitHub rate limits:** 5,000 requests/hour and 30 searches/minute with a token. One search-and-verify costs roughly
  100 requests, so a handful of users per hour is fine; many concurrent users would need per-user OAuth tokens.
