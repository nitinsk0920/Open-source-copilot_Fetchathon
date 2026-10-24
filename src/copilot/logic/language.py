"""Detect non-English issues so they can be dropped (with a reason). Heuristic, no network."""

import re
import unicodedata
from collections import Counter

from copilot.models import Issue

NON_LATIN_THRESHOLD = 0.30  # share of letters in a non-Latin script
MIN_WORDS_FOR_STOPWORDS = 6

_SCRIPT_NAMES = {
    "CJK": "Chinese", "HIRAGANA": "Japanese", "KATAKANA": "Japanese", "HANGUL": "Korean",
    "CYRILLIC": "Russian/Cyrillic", "ARABIC": "Arabic", "DEVANAGARI": "Hindi", "THAI": "Thai",
    "HEBREW": "Hebrew", "GREEK": "Greek", "BENGALI": "Bengali", "TAMIL": "Tamil",
}
_STOPWORDS = {
    "English": set("the and is to of in for with this that it on be are as not we you when should can".split()),
    "Spanish": set("el la los las de que en un una para con por es del se no".split()),
    "Portuguese": set("não uma para com os do da em ao é você são".split()),
    "French": set("le les des une et pour dans est que pas du au".split()),
    "German": set("der die das und ist nicht mit für ein eine zu auf den".split()),
}
_NOISE = re.compile(r"```.*?```|`[^`]*`|https?://\S+", re.S)


def non_english_reason(candidate: Issue) -> str | None:
    """A human-readable reason if the issue text is probably not English, else None."""
    text = _NOISE.sub(" ", f"{candidate.title}\n{candidate.body_excerpt}")
    scripts: Counter[str] = Counter()
    for ch in text:
        if ch.isalpha():
            scripts[unicodedata.name(ch, "UNKNOWN").split()[0]] += 1
    letters = sum(scripts.values())
    if not letters:
        return None

    non_latin = {s: n for s, n in scripts.items() if s != "LATIN"}
    if sum(non_latin.values()) / letters >= NON_LATIN_THRESHOLD:
        has_kana = "HIRAGANA" in non_latin or "KATAKANA" in non_latin  # kana + kanji is Japanese, kanji alone Chinese
        top = max(non_latin, key=non_latin.get)
        return f"not in English ({'Japanese' if has_kana else _SCRIPT_NAMES.get(top, top.title())})"

    words = re.findall(r"[^\W\d_]+", text.lower())
    if len(words) >= MIN_WORDS_FOR_STOPWORDS:
        hits = {lang: sum(w in sw for w in words) for lang, sw in _STOPWORDS.items()}
        other, count = max(((l, c) for l, c in hits.items() if l != "English"), key=lambda x: x[1])
        if count >= 3 and count > 2 * hits["English"]:
            return f"not in English ({other})"
    return None


def filter_non_english(candidates: list[Issue]) -> tuple[list[Issue], list[tuple[Issue, str]]]:
    kept: list[Issue] = []
    dropped: list[tuple[Issue, str]] = []
    for c in candidates:
        reason = non_english_reason(c)
        (dropped.append((c, reason)) if reason else kept.append(c))
    return kept, dropped
