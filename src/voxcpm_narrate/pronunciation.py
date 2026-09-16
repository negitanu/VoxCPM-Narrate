"""Lightweight unknown-word discovery for Japanese narration preprocessing."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

KANJI = re.compile(r"[一-龯々〆ヵヶ]{2,}")
KATAKANA = re.compile(r"[ァ-ヶー]{3,}")
LATIN = re.compile(r"[A-Za-z][A-Za-z0-9._+-]{1,}")
DEFAULT_KNOWN = {
    "これ", "それ", "こと", "もの", "ため", "ようす", "場合", "内容", "確認", "説明", "結果",
    "必要", "最初", "最後", "今日", "明日", "昨日", "時間", "方法", "場所", "資料", "作業",
}


def dictionary_pattern(dictionary: dict[str, str]):
    patterns = []
    for term in sorted(dictionary, key=len, reverse=True):
        if not term:
            continue
        prefix = r"(?<![A-Za-z0-9_])" if term[0].isascii() and term[0].isalnum() else ""
        suffix = r"(?![A-Za-z0-9_])" if term[-1].isascii() and term[-1].isalnum() else ""
        patterns.append(prefix + re.escape(term) + suffix)
    return re.compile("|".join(patterns)) if patterns else None


def apply_dictionary(text: str, dictionary: dict[str, str]) -> str:
    pattern = dictionary_pattern(dictionary)
    return pattern.sub(lambda m: dictionary[m[0]], text) if pattern else text


def validate_reading(term: str, reading: str) -> None:
    if not term.strip() or term != term.strip() or len(term) > 100:
        raise ValueError("用語は前後の空白を除き、1〜100文字で入力してください")
    if not reading.strip() or len(reading) > 200 or not re.fullmatch(
        r"[ぁ-ゖァ-ヺー・、。！？!? 　]+", reading
    ):
        raise ValueError("読みはひらがな・カタカナで200文字以内に入力してください")


def _candidate_terms(text: str) -> list[str]:
    found: list[str] = []
    for pattern in (KANJI, KATAKANA, LATIN):
        found.extend(pattern.findall(text))
    # Preserve first appearance while avoiding nested/duplicate candidates.
    unique = []
    for term in sorted(set(found), key=lambda value: (text.find(value), -len(value))):
        if term not in unique:
            unique.append(term)
    return unique


def find_unknown_words(text: str, dictionary: dict[str, str] | None = None,
                       known: set[str] | None = None) -> list[dict[str, object]]:
    """Return terms worth confirming before synthesis.

    This intentionally errs toward review: it does not claim that a term is absent
    from a linguistic dictionary because the optional morphological packages are not
    required by the application.
    """
    dictionary = dictionary or {}
    known = DEFAULT_KNOWN | (known or set()) | set(dictionary)
    # Mask only actual registered occurrences, not every substring of an entry.
    pattern = dictionary_pattern(dictionary)
    uncovered = pattern.sub(lambda m: " " * len(m[0]), text) if pattern else text
    results = []
    for term in _candidate_terms(uncovered):
        if term in known or term.isdigit() or len(term) < 2:
            continue
        script = "漢字" if KANJI.fullmatch(term) else "カタカナ" if KATAKANA.fullmatch(term) else "英数字"
        index = uncovered.find(term)
        results.append({"term": term, "reading": "", "script": script,
                        "context": text[max(0, index - 30):index + len(term) + 30],
                        "occurrences": uncovered.count(term), "status": "review"})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="台本から読み確認が必要な語を抽出します")
    parser.add_argument("script", type=Path)
    parser.add_argument("--dictionary", type=Path)
    args = parser.parse_args()
    dictionary = json.loads(args.dictionary.read_text()) if args.dictionary else {}
    print(json.dumps(find_unknown_words(args.script.read_text(encoding="utf-8"), dictionary),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
