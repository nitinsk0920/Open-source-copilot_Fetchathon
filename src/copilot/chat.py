"""Small helpers around the Agent Chat Protocol so handlers stay short."""

import re
from datetime import datetime, timezone
from uuid import uuid4

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)


_LEADING_MENTIONS = re.compile(r"^(?:\s*@\S+)+\s*")


def strip_mentions(text: str) -> str:
    """Drop leading '@agent1q...' / '@handle' mentions ASI:One prepends when a user @-tags the agent."""
    return _LEADING_MENTIONS.sub("", text).strip()


def raw_text_of(msg: ChatMessage) -> str:
    return " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()


def worker_chat_protocol(role: str) -> Protocol:
    """Workers still speak ACP so their Agentverse profile is valid; they just point users to the Orchestrator."""
    proto = Protocol(spec=chat_protocol_spec)

    @proto.on_message(ChatMessage)
    async def on_chat(ctx: Context, sender: str, msg: ChatMessage) -> None:
        await acknowledge(ctx, sender, msg)
        await reply(ctx, sender, f"I'm the {role} worker of the Open-Source Contribution Copilot. "
                                 "Please talk to the Orchestrator agent instead; it will call me for you.")

    @proto.on_message(ChatAcknowledgement)
    async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
        pass

    return proto


def text_of(msg: ChatMessage) -> str:
    """Concatenate the text parts of an incoming message, minus any leading @mention."""
    return strip_mentions(raw_text_of(msg))


def is_session_start(msg: ChatMessage) -> bool:
    return any(isinstance(c, StartSessionContent) for c in msg.content)


async def acknowledge(ctx: Context, sender: str, msg: ChatMessage) -> None:
    await ctx.send(
        sender,
        ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id),
    )


async def reply(ctx: Context, sender: str, text: str) -> None:
    await ctx.send(
        sender,
        ChatMessage(
            timestamp=datetime.now(timezone.utc),
            msg_id=uuid4(),
            content=[TextContent(type="text", text=text)],
        ),
    )
