"""Conservative Japanese number readings, before multilingual TTS inference."""

import re
from datetime import date
import unicodedata

DEFAULT_CONTROL = "日本語、明瞭な声、自然な抑揚、会話に近いテンポ"
LEGACY_CONTROL = "ややゆっくり、落ち着いたプレゼン説明調"
JAPANESE_VOICE = (
    "A native Japanese speaker, speaking Japanese with clear articulation, "
    "natural expressive intonation and a conversational pace"
)
DIGITS = ["ゼロ", "いち", "に", "さん", "よん", "ご", "ろく", "なな", "はち", "きゅう"]


def integer_reading(value: str) -> str:
    digits = value.replace(",", "")
    if len(digits) > 16 or (len(digits) > 1 and digits.startswith("0")):
        return "".join(DIGITS[int(char)] for char in digits)
    number = int(digits)
    if number == 0:
        return DIGITS[0]
    groups = []
    for large in ("", "まん", "おく", "ちょう"):
        group = number % 10000
        number //= 10000
        if group:
            parts = []
            for divisor, unit, exceptional in (
                (1000, "せん", {3: "さんぜん", 8: "はっせん"}),
                (100, "ひゃく", {3: "さんびゃく", 6: "ろっぴゃく", 8: "はっぴゃく"}),
                (10, "じゅう", {}),
            ):
                count, group = divmod(group, divisor)
                if count:
                    parts.append(exceptional.get(count, (DIGITS[count] if count > 1 else "") + unit))
            if group:
                parts.append(DIGITS[group])
            groups.append("".join(parts) + large)
    return "".join(reversed(groups))


def number_reading(value: str) -> str:
    sign = ""
    if value.startswith("-"):
        sign, value = "マイナス", value[1:]
    if "." in value:
        whole, fraction = value.split(".")
        return sign + integer_reading(whole) + "てん" + "".join(DIGITS[int(c)] for c in fraction)
    return sign + integer_reading(value)


def kanji_number(value: str) -> str:
    """Preserve a number's lexical identity instead of flattening it to kana."""
    value = value.replace(',', '')
    if value.startswith('-'):
        return 'マイナス' + kanji_number(value[1:])
    digits = '零一二三四五六七八九'
    if '.' in value:
        whole, fraction = value.split('.')
        return kanji_number(whole) + '点' + ''.join(digits[int(c)] for c in fraction)
    if len(value) > 16 or (len(value) > 1 and value.startswith('0')):
        return ''.join(digits[int(c)] for c in value)
    number = int(value)
    if not number:
        return '零'
    groups = []
    for large in ('', '万', '億', '兆'):
        number, group = divmod(number, 10000)
        if not group:
            continue
        parts = []
        for divisor, unit in ((1000, '千'), (100, '百'), (10, '十')):
            count, group = divmod(group, divisor)
            if count:
                parts.append((digits[count] if count > 1 else '') + unit)
        if group:
            parts.append(digits[group])
        groups.append(''.join(parts) + large)
    return ''.join(reversed(groups))


def japanese_numbers(text: str, *, style: str = 'hiragana') -> str:
    if style not in ('hiragana', 'kanji'):
        raise ValueError('Unsupported number reading style')

    # Normalize only numeric width, leaving user readings and punctuation intact.
    text = re.sub(r"[０-９％．，]", lambda m: unicodedata.normalize("NFKC", m[0]), text)
    # Only unambiguous, valid year/month/day dates; leave IDs and versions alone.
    def calendar_date(match):
        year, month, day = int(match[1]), int(match[3]), int(match[4])
        try:
            date(year, month, day)
        except ValueError:
            return match[0]
        return f'{year}年{month}月{day}日'
    text = re.sub(r'(?<![A-Za-z0-9_./-])(\d{4})([/-])(\d{1,2})\2(\d{1,2})(?![A-Za-z0-9_./-])',
                  calendar_date, text)
    if style == 'kanji':
        # Keep counters and adjacent particles in their original orthography:
        # 二千二十六年に, 二人で, 一日間. No invented POS/SSML tokens.
        text = re.sub(r'(?<![A-Za-z\d.])0+(\d+)(?=[年月日時分])', r'\1', text)
        numeric = r'-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
        text = re.sub(rf'(?<![A-Za-z\d.])({numeric})\s*%',
                      lambda m: kanji_number(m[1]) + 'パーセント', text)
        return re.sub(rf'(?<![A-Za-z\d.])({numeric})(?![A-Za-z\d.]|,\d)',
                      lambda m: kanji_number(m[1]), text)
    text = re.sub(r"(?<![\d.])(\d{1,4})年", lambda m: integer_reading(m[1]) + "ねん", text)
    months = {4: "しがつ", 7: "しちがつ", 9: "くがつ"}
    days = {1: "ついたち", 2: "ふつか", 3: "みっか", 4: "よっか", 5: "いつか",
            6: "むいか", 7: "なのか", 8: "ようか", 9: "ここのか", 10: "とおか",
            14: "じゅうよっか", 20: "はつか", 24: "にじゅうよっか"}
    text = re.sub(r"(?<![\d.])(\d{1,2})月", lambda m: months.get(int(m[1]),
                  integer_reading(str(int(m[1]))) + "がつ") if 1 <= int(m[1]) <= 12 else m[0], text)
    # 日間 is a duration, not the first day of a month.
    text = re.sub(r"(?<![\d.])(\d{1,2})日(?!間)", lambda m: days.get(int(m[1]),
                  integer_reading(str(int(m[1]))) + "にち") if 1 <= int(m[1]) <= 31 else m[0], text)
    text = re.sub(r"(?<![\d.])(\d{1,2})時", lambda m: {4: "よじ", 7: "しちじ", 9: "くじ"}.get(
        int(m[1]), integer_reading(m[1]) + "じ"), text)
    def minutes(match):
        n = int(match[1])
        if n >= 60:
            return match[0]
        if n % 10 in (1, 6, 8):
            prefix = integer_reading(str(n - n % 10)) if n >= 10 else ""
            return prefix + {1: "いっぷん", 6: "ろっぷん", 8: "はっぷん"}[n % 10]
        if n and n % 10 == 0:
            return integer_reading(str(n))[:-3] + "じゅっぷん"
        return integer_reading(str(n)) + ("ぷん" if n % 10 in (3, 4) else "ふん")
    text = re.sub(r"(?<![\d.])(\d+)分(?!の)", minutes, text)
    numeric = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    text = re.sub(f"({numeric})\\s*[%％]", lambda m: number_reading(m[1]) + "パーセント", text)
    text = re.sub(f"({numeric})円", lambda m: number_reading(m[1]) + "えん", text)
    # Leave alphanumeric IDs and version strings to explicit reading overrides.
    text = re.sub(rf"(?<![A-Za-z\d.])({numeric})(?![A-Za-z\d.]|,\d)",
                  lambda m: number_reading(m[1]), text)
    return text
