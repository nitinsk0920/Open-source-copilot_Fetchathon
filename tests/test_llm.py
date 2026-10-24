from types import SimpleNamespace

import openai
import pytest
from pydantic import BaseModel

from copilot.logic.intent import UserProfile, parse_profile
from copilot.logic.ranking import _LLMItem, _LLMRanking, heuristic_rank, rank_candidates, validate_ranking
from copilot.models import Issue
from copilot.tools.llm_client import LLMClient, LLMError, extract_json


class Shape(BaseModel):
    n: int


def fake_openai(*replies):
    """Mimics AsyncOpenAI: each call returns the next reply (a string) or raises it if it is an exception."""
    queue = list(replies)
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=item))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


class FakeLLM:
    """Stands in for LLMClient at the logic level."""

    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error, self.calls = result, error, []

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def cand(i: int, comments: int = 0) -> Issue:
    return Issue(repo=f"o/r{i}", number=i, title=f"t{i}", url="u", comments=comments)


def test_extract_json_handles_fences_and_prose():
    assert extract_json('```json\n{"n": 1}\n```') == {"n": 1}
    assert extract_json('Sure! {"n": 2} hope that helps') == {"n": 2}
    with pytest.raises(ValueError):
        extract_json("no json here")


async def test_complete_json_ok_and_json_mode_requested():
    client, calls = fake_openai('{"n": 3}')
    assert (await LLMClient(client=client).complete_json(model="m", system="s", user="u", schema=Shape)).n == 3
    assert calls[0]["response_format"] == {"type": "json_object"}


async def test_complete_json_repairs_once_then_fails():
    client, calls = fake_openai("garbage", '{"n": 4}')
    assert (await LLMClient(client=client).complete_json(model="m", system="s", user="u", schema=Shape)).n == 4
    assert len(calls) == 2 and "not valid JSON" in calls[1]["messages"][-1]["content"]
    client, _ = fake_openai("garbage", "still garbage")
    with pytest.raises(LLMError):
        await LLMClient(client=client).complete_json(model="m", system="s", user="u", schema=Shape)


async def test_api_errors_become_llm_error_without_details():
    client, _ = fake_openai(openai.OpenAIError("secret-ish detail"))
    with pytest.raises(LLMError) as exc:
        await LLMClient(client=client).complete_json(model="m", system="s", user="u", schema=Shape)
    assert "secret-ish" not in str(exc.value)


async def test_empty_reply_is_llm_error():
    client, _ = fake_openai("  ")
    with pytest.raises(LLMError):
        await LLMClient(client=client).complete_json(model="m", system="s", user="u", schema=Shape)


async def test_parse_profile_merges_with_existing():
    llm = FakeLLM(UserProfile(languages=["JS", "python"], level="beginner", time_budget_hours=10))
    existing = UserProfile(languages=["rust"], interests=["docs"])
    p = await parse_profile(llm, "student, python and js, a weekend", existing)
    assert p.languages == ["javascript", "python"] and p.level == "beginner"
    assert p.time_budget_hours == 10 and p.interests == ["docs"]


async def test_parse_profile_keeps_old_values_when_new_says_nothing():
    llm = FakeLLM(UserProfile())  # LLM found nothing at all
    existing = UserProfile(languages=["python"], level="beginner", time_budget_hours=5)
    assert await parse_profile(llm, "thanks", existing) == existing


async def test_parse_profile_falls_back_to_keywords_when_llm_fails_or_is_absent():
    for llm in (FakeLLM(error=LLMError("down")), None):
        p = await parse_profile(llm, "I want a Rust and Python issue")
        assert p.languages == ["python", "rust"]


def test_validate_ranking_drops_bad_ids_dedupes_and_clamps():
    result = _LLMRanking(ranking=[
        _LLMItem(id=2, fit=7, reason="a"), _LLMItem(id=99, fit=10), _LLMItem(id=2, fit=9),
        _LLMItem(id=1, fit=15, difficulty="easy", est_hours=2, reason="b"), _LLMItem(id=3, fit=-4),
    ])
    ranked = validate_ranking(result, [cand(1), cand(2), cand(3)], top_n=5)
    assert [(r.candidate.number, r.fit) for r in ranked] == [(1, 10), (2, 7), (3, 1)]
    assert all(r.ai_ranked for r in ranked)


async def test_rank_candidates_uses_llm_and_sends_untrusted_data_warning():
    llm = FakeLLM(_LLMRanking(ranking=[_LLMItem(id=2, fit=8, reason="good")]))
    ranked = await rank_candidates(llm, UserProfile(languages=["python"]), [cand(1), cand(2)])
    assert [r.candidate.number for r in ranked] == [2]
    assert "untrusted" in llm.calls[0]["system"] and "<issues>" in llm.calls[0]["user"]


async def test_rank_candidates_falls_back_on_error_or_no_valid_ids():
    cands = [cand(1, comments=5), cand(2, comments=0), cand(3, comments=0)]
    for llm in (FakeLLM(error=LLMError("down")), FakeLLM(_LLMRanking(ranking=[_LLMItem(id=42)])), None):
        ranked = await rank_candidates(llm, UserProfile(), cands, top_n=2)
        assert [r.candidate.number for r in ranked] == [2, 3] and not ranked[0].ai_ranked


def test_heuristic_rank_empty():
    assert heuristic_rank([]) == []
