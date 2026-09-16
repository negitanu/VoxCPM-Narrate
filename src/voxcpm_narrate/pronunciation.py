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
    results = []
    for term in _candidate_terms(text):
        if term in known or term.isdigit() or len(term) < 2:
            continue
        if any(term in other and term != other for other in dictionary):
            continue
        script = "漢字" if KANJI.fullmatch(term) else "カタカナ" if KATAKANA.fullmatch(term) else "英数字"
        results.append({"term": term, "reading": dictionary.get(term, ""), "script": script,
                        "occurrences": text.count(term), "status": "known" if term in dictionary else "review"})
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
