"""ASI Interactive Cards: an issue carousel and an Approve/Reject review card. Cards are an add-on: the plain-text
commands always work, and the text of every step is sent regardless.

Format: https://innovationlab.fetch.ai/resources/docs/interactive-cards/asi-interactive-cards
metadata values must all be strings, so the payload is JSON-encoded. ASI:One returns the clicked button's `selection`
(as a JSON string on a direct @mention) plus keys of its own such as "approved", which are never trusted.
"""

import json
from datetime import datetime, timezone
from uuid import uuid4

from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent, TextContent

from copilot.logic.ranking import RankedIssue
from copilot.logic.review import issue_ref, pr_ref, review_ref

MAX_PAYLOAD_BYTES = 64 * 1024  # documented limit
CARD_IMAGE = "https://github.com/github.png?size=160"  # one fixed, neutral image (not the repo owner's avatar)


def carousel_payload(ranked: list[RankedIssue]) -> dict:
    items = []
    for i, r in enumerate(ranked, 1):
        c = r.candidate
        badges = [{"label": f"{r.difficulty}", "variant": "success" if r.difficulty == "easy" else "info"}]
        if r.est_hours:
            badges.append({"label": f"~{r.est_hours:g} h", "variant": "info"})
        items.append({
            "id": f"issue_{i}",
            "image": CARD_IMAGE,
            "title": f"{c.repo} #{c.number}",
            "subtitle": c.title[:120],
            "badges": badges,
            "secondary_text": (r.reason or f"{c.comments} comments")[:160],
            # "ref" lets the Orchestrator refuse a click from an old list that has since been replaced
            "primary_cta": {"label": "Pick this one", "selection": {"action": "pick", "index": i, "ref": issue_ref(c)}},
        })
    return {"title": "Pick an issue", "subtitle": "Tap one, or just type \"pick 2\"", "items": items}


def review_payload(r: RankedIssue, comment: str, branch: str, live: bool) -> dict:
    c = r.candidate
    default = r.verification.default_branch if r.verification else "main"
    ref = review_ref(c, comment, branch)
    return {
        "title": "Approve these actions?",
        "summary_rows": [
            {"label": "Issue", "value": f"{c.repo} #{c.number}"},
            {"label": "Mode", "value": "LIVE: this will write to GitHub" if live else "DRY RUN: nothing is written"},
            {"label": "1. Fork", "value": c.repo},
            {"label": "2. Comment", "value": comment if len(comment) <= 240 else comment[:237] + "..."},
            {"label": "3. Branch", "value": f"{branch} (from {default})"},
        ],
        "approve_cta": {"label": "Approve", "selection": {"action": "approve", "ref": ref}, "primary": True},
        "reject_cta": {"label": "Reject", "selection": {"action": "reject", "ref": ref}},
    }


def pr_review_payload(r: RankedIssue, title: str, body: str, head: str, base: str, live: bool, check: str) -> dict:
    c = r.candidate
    ref = pr_ref(c, title, body, head)
    return {
        "title": "Open this pull request?",
        "summary_rows": [
            {"label": "Issue", "value": f"{c.repo} #{c.number}"},
            {"label": "Mode", "value": "LIVE: this will open a real pull request" if live else "DRY RUN: nothing is opened"},
            {"label": "From", "value": head},
            {"label": "Into", "value": f"{c.repo} ({base})"},
            {"label": "Title", "value": title[:200]},
            {"label": "Description", "value": body if len(body) <= 240 else body[:237] + "..."},
            {"label": "Your changes", "value": check[:240] or "(not checked)"},
        ],
        "approve_cta": {"label": "Open pull request", "selection": {"action": "approve", "ref": ref}, "primary": True},
        "reject_cta": {"label": "Cancel", "selection": {"action": "reject", "ref": ref}},
    }


def _card_message(narration: str, kind: str, payload: dict) -> ChatMessage:
    encoded = json.dumps(payload)
    if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError("card payload too large")
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[
            TextContent(type="text", text=narration),
            MetadataContent(type="metadata", metadata={
                "card_protocol_version": "1",
                "requires_card_interaction": "true",
                "card_kind": kind,
                "card_payload": encoded,
            }),
        ],
    )


def carousel_message(narration: str, ranked: list[RankedIssue]) -> ChatMessage:
    return _card_message(narration, "carousel", carousel_payload(ranked))


def review_message(narration: str, r: RankedIssue, comment: str, branch: str, live: bool) -> ChatMessage:
    return _card_message(narration, "review", review_payload(r, comment, branch, live))


def pr_review_message(narration: str, r: RankedIssue, title: str, body: str, head: str, base: str, live: bool,
                      check: str) -> ChatMessage:
    return _card_message(narration, "review", pr_review_payload(r, title, body, head, base, live, check))
