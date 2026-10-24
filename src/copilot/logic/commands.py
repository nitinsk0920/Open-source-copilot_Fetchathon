"""Deterministic text commands. They must always work, whatever the LLM or cards do."""

import json
import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal["pick", "cards", "help", "approve", "reject", "edit", "retry", "open_pr", "edit_pr", "text"]

_PICK = re.compile(r"^(?:i(?:'ll|\s+will)?\s+)?(?:pick|choose|select|take|go\s+with)?\s*(?:issue\s*)?(?:#|number|no\.?)?\s*(\d{1,2})\s*[.!]?$", re.I)
_PICK_PROSE = re.compile(r"\b(?:index|number|option|issue|pick(?:ed)?)\D{0,12}(\d{1,2})\b", re.I)
_EDIT = re.compile(r"^(?:edit(?: the)? comment|change(?: the)? comment|comment)\s*[:\-]\s*(.+)$", re.I | re.S)
_OPEN_PR = re.compile(r"^(?:open|create|send|submit|make)(?: a| the| my)?\s+(?:pr|pull request)$", re.I)
_EDIT_PR = re.compile(r"^(?:edit|change)(?: the)? (?:pr|pull request)(?: description| body)?\s*[:\-]\s*(.+)$", re.I | re.S)
# Whole-message matches only: a write action must never be triggered by a word inside a sentence.
_APPROVE = {"approve", "approved", "yes", "confirm", "go ahead", "do it", "proceed"}
_REJECT = {"reject", "cancel", "no", "stop", "abort", "never mind", "nevermind"}
_ACTIONS: dict[str, Kind] = {"approve": "approve", "reject": "reject", "retry": "retry", "cards": "cards",
                             "open_pr": "open_pr"}


@dataclass
class Command:
    kind: Kind
    index: int | None = None  # 1-based, for "pick"
    text: str = ""  # for "edit"
    ref: str = ""  # what a card click was showing; the Orchestrator refuses it if that is no longer current


def parse_command(text: str) -> Command:
    stripped = text.strip()
    if not stripped:
        return Command("help")
    # A card click reaches us as a JSON string when the user @-mentioned the agent directly.
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict):
            ref = str(data.get("ref", ""))
            # ASI:One adds its own keys to the selection (e.g. "approved": true); only OUR "action" decides, and an
            # explicit "approved": false on an approve is never treated as an approval.
            if data.get("action") == "pick" and str(data.get("index", "")).isdigit():
                return Command("pick", int(data["index"]), ref=ref)
            if data.get("action") == "approve" and data.get("approved") is False:
                return Command("text")
            if data.get("action") in _ACTIONS:
                return Command(_ACTIONS[data["action"]], ref=ref)
    lowered = re.sub(r"[.!]+$", "", stripped.lower()).strip()
    if lowered in {"help", "hi", "hello", "hey", "start", "?"}:
        return Command("help")
    if lowered in {"cards", "card", "card test", "show cards", "interactive"}:
        return Command("cards")
    if lowered in _APPROVE:
        return Command("approve")
    if lowered in _REJECT:
        return Command("reject")
    if lowered == "retry":
        return Command("retry")
    if _OPEN_PR.match(lowered):
        return Command("open_pr")
    edit_pr = _EDIT_PR.match(stripped)  # before _EDIT: "edit pr: ..." must not be read as a comment edit
    if edit_pr:
        return Command("edit_pr", text=edit_pr.group(1).strip())
    edit = _EDIT.match(stripped)
    if edit:
        return Command("edit", text=edit.group(1).strip())
    match = _PICK.match(stripped)
    if match:
        return Command("pick", int(match.group(1)))
    # Planner-relayed clicks arrive as prose ("The user picked ... index 2"), only trust it when it is clearly one.
    if re.search(r"\b(?:user\s+)?(?:picked|selected|chose)\b", lowered):
        prose = _PICK_PROSE.search(stripped)
        if prose:
            return Command("pick", int(prose.group(1)))
    return Command("text")
