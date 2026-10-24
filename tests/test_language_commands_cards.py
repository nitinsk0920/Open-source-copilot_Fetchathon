import json

from uagents_core.contrib.protocols.chat import MetadataContent, TextContent

from copilot.cards import CARD_IMAGE, carousel_message, carousel_payload
from copilot.logic.commands import parse_command
from copilot.logic.language import filter_non_english, non_english_reason
from copilot.logic.ranking import RankedIssue
from copilot.models import Issue
from copilot.state import SessionStore


def issue(title: str, body: str = "", n: int = 1) -> Issue:
    return Issue(repo="o/r", number=n, title=title, url="u", body_excerpt=body)


def test_japanese_chinese_korean_russian_detected():
    assert non_english_reason(issue("わざと Check Features を落として、ログを読んで直す")) == "not in English (Japanese)"
    assert non_english_reason(issue("修复登录页面的错误")) == "not in English (Chinese)"
    assert non_english_reason(issue("로그인 버그를 수정하세요")) == "not in English (Korean)"
    assert "Russian" in non_english_reason(issue("Исправить ошибку при входе в систему"))


def test_latin_script_languages_detected_by_stopwords():
    es = issue("Error en el login", "El botón de la página no funciona cuando se hace clic en el menú para los usuarios")
    assert non_english_reason(es) == "not in English (Spanish)"
    de = issue("Fehler beim Login", "Der Button ist nicht sichtbar und die Seite lädt nicht mit dem neuen Menü auf dem Handy")
    assert non_english_reason(de) == "not in English (German)"


def test_english_code_and_short_titles_are_kept():
    assert non_english_reason(issue("Fix typo in README", "The word `recieve` should be `receive` in the docs for this page.")) is None
    assert non_english_reason(issue("Add tests for parser")) is None
    assert non_english_reason(issue("Bug: is_palindrome is case-sensitive", "```python\ndef f(x): return x\n```")) is None
    assert non_english_reason(issue("日本語 docs", "Please translate the README to English and update the links in the docs")) is None


def test_filter_non_english_splits_with_reasons():
    kept, dropped = filter_non_english([issue("Fix typo", n=1), issue("バグを直す", n=2)])
    assert [c.number for c in kept] == [1]
    assert dropped[0][0].number == 2 and "Japanese" in dropped[0][1]


def test_commands():
    assert parse_command("pick 2").index == 2
    assert parse_command("Pick #3").index == 3
    assert parse_command("I'll take number 4!").index == 4
    assert parse_command("2").index == 2
    assert parse_command("go with 1").index == 1
    assert parse_command("cards").kind == "cards"
    assert parse_command("hi").kind == "help" and parse_command("").kind == "help"
    assert parse_command("find me a python issue with 5 hours").kind == "text"  # digits alone don't make a pick


def test_approval_commands_match_whole_messages_only():
    for word in ["approve", "Approve!", "yes", "go ahead", "Do it.", "confirm", "proceed"]:
        assert parse_command(word).kind == "approve", word
    for word in ["reject", "cancel", "No", "stop", "never mind"]:
        assert parse_command(word).kind == "reject", word
    # a write action must never be triggered by a word inside a sentence
    for sentence in ["I would approve of a Python issue", "yes please find me a Rust issue", "no python please",
                     "do it in Python", "ok", "looks good"]:
        assert parse_command(sentence).kind not in ("approve", "reject"), sentence
    assert parse_command("retry").kind == "retry"


def test_edit_comment_command():
    c = parse_command("edit comment: Hi!\nI'd love to help.")
    assert c.kind == "edit" and c.text == "Hi!\nI'd love to help."
    assert parse_command("Comment - hello there").text == "hello there"
    assert parse_command("change the comment: new text").kind == "edit"
    assert parse_command("edit comment:").kind == "text"


def test_card_actions_map_to_commands():
    assert parse_command('{"action": "approve"}').kind == "approve"
    assert parse_command('{"action": "reject"}').kind == "reject"
    assert parse_command('{"action": "unknown"}').kind == "text"


def test_card_click_formats_are_parsed():
    assert parse_command(json.dumps({"action": "pick", "index": 3})) == parse_command("pick 3")
    assert parse_command("The user picked issue_2 with index 2").index == 2
    assert parse_command('{"action": "other"}').kind == "text"
    assert parse_command("{not json").kind == "text"


def test_carousel_message_is_valid_card():
    ranked = [RankedIssue(candidate=issue("t", n=i), difficulty="easy", est_hours=2, reason="why") for i in (1, 2)]
    msg = carousel_message("narration", ranked)
    text, meta = msg.content
    assert isinstance(text, TextContent) and isinstance(meta, MetadataContent)
    assert all(isinstance(v, str) for v in meta.metadata.values())
    assert meta.metadata["card_kind"] == "carousel" and meta.metadata["requires_card_interaction"] == "true"
    payload = json.loads(meta.metadata["card_payload"])
    assert payload["items"][1]["primary_cta"]["selection"] == {"action": "pick", "index": 2, "ref": "o/r#2"}
    assert {i["image"] for i in payload["items"]} == {CARD_IMAGE}  # one fixed image, never the owner's avatar
    clicked = parse_command(json.dumps(carousel_payload(ranked)["items"][1]["primary_cta"]["selection"]))
    assert clicked.index == 2 and clicked.ref == "o/r#2"


def test_session_store_persists_and_survives_corrupt_file(tmp_path):
    path = tmp_path / "s.json"
    store = SessionStore(path)
    s = store.get("user1")
    s.profile.languages = ["python"]
    s.ranked = [RankedIssue(candidate=issue("t"), reason="r")]
    store.save()
    again = SessionStore(path).get("user1")
    assert again.profile.languages == ["python"] and again.ranked[0].candidate.title == "t"
    path.write_text("{corrupt", encoding="utf-8")
    assert SessionStore(path).get("user1").ranked == []
