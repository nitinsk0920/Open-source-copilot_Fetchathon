"""ASI:One LLM client (OpenAI-compatible). Never logs the key or prompts' secrets."""

import json
import logging
import os
import re
from typing import Any, TypeVar

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel

from copilot import config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.asi1.ai/v1"
FAST_MODEL = os.getenv("LLM_FAST_MODEL", "asi1-mini")
MAIN_MODEL = os.getenv("LLM_MAIN_MODEL", "asi1")

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The LLM call failed or returned something unusable. Callers fall back to heuristics."""


def extract_json(text: str) -> Any:
    """Parse a JSON object out of a model reply, tolerating code fences and surrounding prose."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start : end + 1])


class LLMClient:
    def __init__(self, *, client: Any = None, timeout: float = 45.0, max_retries: int = 2):
        # `client` is injectable for tests (openai 3.x uses its own HTTP stack, so HTTP mocking won't reach it).
        self._client = client or AsyncOpenAI(
            api_key=config.require_env("ASI1_API_KEY"), base_url=BASE_URL, timeout=timeout, max_retries=max_retries
        )

    async def _complete(self, model: str, messages: list[dict], max_tokens: int, temperature: float) -> str:
        kwargs: dict[str, Any] = dict(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
        try:
            try:
                resp = await self._client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
            except openai.BadRequestError:
                resp = await self._client.chat.completions.create(**kwargs)  # JSON mode unsupported for this model
        except openai.OpenAIError as exc:
            logger.warning("LLM call failed (%s): %s", model, type(exc).__name__)
            raise LLMError(f"{type(exc).__name__}") from None
        content = resp.choices[0].message.content if resp.choices else None
        if not content or not content.strip():
            raise LLMError("empty response")
        return content

    async def complete_json(
        self, *, model: str, system: str, user: str, schema: type[T], max_tokens: int = 1200, temperature: float = 0.1
    ) -> T:
        """Ask for JSON and validate it against `schema`; one repair attempt, then LLMError."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for attempt in range(2):
            text = await self._complete(model, messages, max_tokens, temperature)
            try:
                return schema.model_validate(extract_json(text))
            except ValueError as exc:  # JSONDecodeError and pydantic ValidationError both subclass it
                logger.warning("LLM returned invalid JSON (attempt %d): %s", attempt + 1, type(exc).__name__)
                messages += [
                    {"role": "assistant", "content": text[:2000]},
                    {"role": "user", "content": "That was not valid JSON for the requested schema. Reply with only the JSON object."},
                ]
        raise LLMError("model returned invalid JSON")
