"""Run one agent per process: python -m copilot.main <orchestrator|discovery>."""

import argparse
import logging
import os

from copilot.config import agent_address, env_int, require_env, setup_logging


def build(name: str):
    if name == "orchestrator":
        from copilot.agents.orchestrator import create_orchestrator

        return create_orchestrator(
            seed=require_env("ORCHESTRATOR_SEED_PHRASE"),
            port=env_int("ORCHESTRATOR_PORT", 8000),
            discovery_address=agent_address("DISCOVERY_SEED_PHRASE", "DISCOVERY_ADDRESS"),
            analyst_address=agent_address("ANALYST_SEED_PHRASE", "ANALYST_ADDRESS"),
            action_address=agent_address("ACTION_SEED_PHRASE", "ACTION_ADDRESS"),
            llm=make_llm(),
        )
    if name == "discovery":
        from copilot.agents.discovery import create_discovery

        return create_discovery(
            seed=require_env("DISCOVERY_SEED_PHRASE"),
            port=env_int("DISCOVERY_PORT", 8001),
        )
    if name == "analyst":
        from copilot.agents.analyst import create_analyst

        return create_analyst(
            seed=require_env("ANALYST_SEED_PHRASE"), port=env_int("ANALYST_PORT", 8002), llm=make_llm(),
        )
    if name == "action":
        from copilot.agents.action import create_action

        return create_action(
            seed=require_env("ACTION_SEED_PHRASE"), port=env_int("ACTION_PORT", 8003),
            orchestrator_address=agent_address("ORCHESTRATOR_SEED_PHRASE", "ORCHESTRATOR_ADDRESS"),
        )
    raise SystemExit(f"unknown agent {name!r}")


def make_llm():
    from copilot.tools.llm_client import LLMClient

    if not os.getenv("ASI1_API_KEY", "").strip():
        logging.getLogger(__name__).warning("ASI1_API_KEY not set: running without the LLM (keyword fallback)")
        return None
    return LLMClient()


AGENTS = ("orchestrator", "discovery", "analyst", "action")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("agent", choices=AGENTS, nargs="?", default="orchestrator")
    args = parser.parse_args()
    setup_logging()
    build(args.agent).run()


if __name__ == "__main__":
    main()
