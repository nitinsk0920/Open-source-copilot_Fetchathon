"""End-to-end test without ASI:One: runs the four agents locally (no mailboxes, throwaway seeds and ports, so your
real agents and their Agentverse registrations are untouched) plus a scripted "user" agent that chats with the
Orchestrator over the real Agent Chat Protocol.

    python scripts/local_e2e.py                 # dry run: nothing is written to GitHub
    python scripts/local_e2e.py owner/test-repo # that repo is allowlisted, so approve makes REAL writes

Needs GITHUB_TOKEN and ASI1_API_KEY in .env (read from the environment, never printed).
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

SEEDS = {"ORCHESTRATOR": "e2e-orchestrator-seed-3f9a", "DISCOVERY": "e2e-discovery-seed-3f9a",
         "ANALYST": "e2e-analyst-seed-3f9a", "ACTION": "e2e-action-seed-3f9a"}
PORTS = {"ORCHESTRATOR_PORT": "18000", "DISCOVERY_PORT": "18001", "ANALYST_PORT": "18002", "ACTION_PORT": "18003"}
CLIENT_SEED, CLIENT_PORT = "e2e-client-seed-3f9a", 18010

SCENARIO = [  # (message, substrings that mean "this step is finished", timeout seconds)
    ("I'm a second-year student, I know Python, I have about a weekend", ["pick 2", "none passed", "no open", "couldn't"], 180),
    ("pick 1", ['"approve"'], 30),
    ("approve", ["Saved to", "Not everything worked", "refused", "couldn't reach"], 150),
]


def click(cards: list, kind: str, *path: str) -> str:
    """What ASI:One sends when a button is clicked: the button's selection plus keys of its own ("approved")."""
    payload = next(p for k, p in reversed(cards) if k == kind)
    node = payload
    for key in path:
        node = node["items"][0] if key == "items[0]" else node[key]
    return json.dumps({**node["selection"], "approved": True})


def card_scenario(cards: list) -> list:
    return [
        ("I'm a beginner, I know Python, I have about a weekend", ["pick 2", "none passed", "no open", "couldn't"], 180),
        ("cards", ["interactive picker"], 30),
        (lambda: click(cards, "carousel", "items[0]", "primary_cta"), ["Or tap a button"], 30),
        ('{"action": "approve", "ref": "bogus/repo#1:deadbeef"}', ["earlier review"], 30),  # stale: must NOT run
        (lambda: click(cards, "review", "reject_cta"), ["Cancelled"], 30),
        ("pick 1", ["Or tap a button"], 30),
        (lambda: click(cards, "review", "approve_cta"), ["Saved to", "Not everything worked", "refused", "couldn't reach"], 150),
        ("open pr", ["Or tap a button"], 60),  # the branch check, then the pull request review + its card
        ('{"action": "approve", "ref": "bogus/repo#1:pr:deadbeef"}', ["earlier review"], 30),  # stale: must NOT open
        ("edit pr: Adds a clear error message. Tested with pytest.", ["Or tap a button"], 30),
        (lambda: click(cards, "review", "approve_cta"), ["nothing was opened", "was not opened", "refused"], 60),
    ]


def agent_env(allow: str) -> dict:
    env = dict(os.environ, LOCAL_ONLY="1", ALLOWED_TARGET_REPOS=allow, DEMO_MODE="true", PYTHONUNBUFFERED="1",
               PYTHONIOENCODING="utf-8")
    env.update({f"{k}_SEED_PHRASE": v for k, v in SEEDS.items()})
    env.update(PORTS)
    return env


def run_client() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # issue titles can contain emoji
    from copilot import config  # noqa: F401  (loads .env, needed for --running)
    from uagents import Agent, Context, Protocol
    from uagents.crypto import Identity
    from uagents_core.contrib.protocols.chat import (ChatAcknowledgement, ChatMessage, MetadataContent, TextContent,
                                                     chat_protocol_spec)

    # --running: talk to the REAL agents (their addresses come from your .env seeds) through the real mailboxes.
    seed = os.environ["ORCHESTRATOR_SEED_PHRASE"] if os.getenv("E2E_TARGET") == "real" else SEEDS["ORCHESTRATOR"]
    target = Identity.from_seed(seed, 0).address
    client = Agent(name="e2e-client", seed=CLIENT_SEED, port=CLIENT_PORT,
                   endpoint=[f"http://127.0.0.1:{CLIENT_PORT}/submit"])
    proto = Protocol(spec=chat_protocol_spec)
    inbox: list[str] = []
    history: list[str] = []
    cards: list[tuple[str, dict]] = []  # (card_kind, payload) for every card the Orchestrator sends

    @proto.on_message(ChatMessage)
    async def on_msg(ctx: Context, sender: str, msg: ChatMessage) -> None:
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
        for part in msg.content:
            if isinstance(part, MetadataContent) and part.metadata.get("card_kind"):
                cards.append((part.metadata["card_kind"], json.loads(part.metadata["card_payload"])))
        inbox.append(" ".join(c.text for c in msg.content if isinstance(c, TextContent)))

    @proto.on_message(ChatAcknowledgement)
    async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
        pass

    client.include(proto)

    @client.on_event("startup")
    async def scenario(ctx: Context) -> None:
        await asyncio.sleep(12)  # let the other agents register
        scenario_steps = card_scenario(cards) if os.getenv("E2E_SCENARIO") == "cards" else SCENARIO
        steps = scenario_steps[: int(os.getenv("E2E_STEPS", len(scenario_steps)))]
        for i, (text, markers, timeout) in enumerate(steps):
            text = text() if callable(text) else text
            text = os.getenv("E2E_FIRST", text) if i == 0 else text
            repo = os.getenv("E2E_PICK_REPO", "")
            if text == "pick 1" and repo:  # choose the allowlisted repo's issue, wherever it was ranked
                import re

                listing = next((r for r in reversed(history) if 'pick 2' in r), "")
                m = re.search(rf"^(\d+)\. \*\*{re.escape(repo)}\*\*", listing, re.M | re.I)
                if not m:
                    print(f"!!! {repo} is not in the ranked list, so there is nothing safe to pick", flush=True)
                    os._exit(2)
                text = f"pick {m.group(1)}"
            inbox.clear()
            print(f"\n>>> USER: {text}", flush=True)
            started = time.monotonic()
            await ctx.send(target, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                               content=[TextContent(type="text", text=text)]))
            seen = 0
            while time.monotonic() - started < timeout:
                await asyncio.sleep(0.5)
                for reply in inbox[seen:]:
                    history.append(reply)
                    print(f"<<< [{time.monotonic() - started:5.1f}s] {reply}\n", flush=True)
                seen = len(inbox)
                if any(m in r for r in inbox for m in markers):
                    break
            else:
                print(f"!!! step timed out after {timeout}s", flush=True)
                os._exit(1)
        print("=== scenario finished ===", flush=True)
        os._exit(0)

    client.run()


def main() -> None:
    if "--client" in sys.argv:
        run_client()
        return
    if "--running" in sys.argv:  # no agents are started: read-only check of what is already running (use E2E_STEPS=1)
        env = dict(os.environ, E2E_TARGET="real", PYTHONIOENCODING="utf-8", E2E_STEPS=os.getenv("E2E_STEPS", "1"))
        sys.exit(subprocess.run([sys.executable, __file__, "--client"], env=env).returncode)
    allow = sys.argv[1] if len(sys.argv) > 1 else ""
    procs = []
    try:
        for name in ("discovery", "analyst", "action", "orchestrator"):
            procs.append(subprocess.Popen([sys.executable, "-m", "copilot.main", name], env=agent_env(allow),
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        print(f"agents started (allowlist: {allow or 'none, dry run'})", flush=True)
        code = subprocess.run([sys.executable, __file__, "--client"], env=agent_env(allow)).returncode
        print(f"client exit code {code}")
    finally:
        for p in procs:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
            else:
                p.terminate()


if __name__ == "__main__":
    main()
