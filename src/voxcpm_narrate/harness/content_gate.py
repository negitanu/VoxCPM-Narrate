"""Local, fail-closed content verification before publishing narration."""
import re
from difflib import SequenceMatcher
import time
import unicodedata
from functools import lru_cache

from voxcpm_narrate.harness.metrics import char_error_rate
from voxcpm_narrate.japanese import japanese_numbers

GATE_VERSION = 'content-v2'


@lru_cache(maxsize=4096)
def phonetic(text):
    """Comparable kana readings for dates, numbers and spelling variants."""
    from voxcpm_narrate.speech_rate import reader
    return normalize(''.join(item['hira'] for item in reader().convert(
        japanese_numbers(unicodedata.normalize('NFKC', text)))))


def inspection_ranges(samples, sr=16000):
    ranges = [(0, samples)]
    if samples <= 8 * sr:
        return ranges
    windows = [(i, min(i + 6 * sr, samples)) for i in range(0, samples, 6 * sr)]
    # A fractional word or breath has no reliable language or transcript.
    if windows[-1][1] - windows[-1][0] < 2 * sr:
        windows[-2] = (windows[-2][0], samples)
        windows.pop()
    return ranges + windows


def numeric_facts(value):
    return re.findall(r'\d+(?:\.\d+)?(?:年|月|日|時|分|円|%)|(?<!\d)\d{4}(?!\d)',
                      re.sub(r'[\s、,]', '', unicodedata.normalize('NFKC', value)))


def normalize(text):
    text = unicodedata.normalize('NFKC', text).lower()
    text = ''.join(chr(ord(c) - 0x60) if 'ァ' <= c <= 'ヶ' else c for c in text)
    return ''.join(c for c in text if unicodedata.category(c)[0] in 'LN')


def local_error(reference, hypothesis):
    """Edit distance against the best contiguous reference span (linear memory)."""
    if not hypothesis:
        return 1.0
    previous = [0] * (len(reference) + 1)
    for i, char in enumerate(hypothesis, 1):
        current = [i]
        for j, target in enumerate(reference, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (char != target)))
        previous = current
    return min(previous) / len(hypothesis)


def inspect_audio(path, references, asr, checkpoint=lambda: None):
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    from math import gcd

    started = time.monotonic()
    wav, sr = sf.read(path, dtype='float32')
    report = dict(version=GATE_VERSION, passed=False, reasons=[], warnings=[], windows=[])
    if (wav.ndim != 1 or not len(wav) or not np.isfinite(wav).all()
            or len(wav) / sr > 120 or np.max(np.abs(wav)) < 1e-4):
        report['reasons'] = ['無音・不正な音声、または120秒を超える音声']
        return report
    divisor = gcd(sr, 16000)
    audio = resample_poly(wav, 16000 // divisor, sr // divisor)
    sources = [r for r in references if normalize(r)]
    refs = [normalize(r) for r in sources]
    readings = [phonetic(r) for r in sources]
    if not refs:
        raise ValueError('照合する原稿がありません')
    # Full transcript catches omissions/repetition; windows expose speech an ASR
    # decoder may silently omit when processing a long clip in one pass.
    ranges = inspection_ranges(len(audio))
    for index, (start, end) in enumerate(ranges):
        checkpoint()
        raw = asr.recognize(audio[start:end])
        languages = re.findall(r'<\|(ja|zh|en|ko|yue)\|>', raw)
        text = re.sub(r'<\|[^|]*\|>', '', raw).strip()
        hyp = normalize(text)
        metric = char_error_rate if index == 0 else local_error
        spelling_error = min(metric(r, hyp) for r in refs)
        reading_error = min(metric(r, phonetic(text)) for r in readings)
        error = min(spelling_error, reading_error)
        # Empty edge windows may contain only trailing silence; never allow an
        # empty full transcription to pass.
        audible = float(np.sqrt(np.mean(audio[start:end] ** 2))) > .005
        # Moderate ASR disagreement is reviewable, not grounds for repeated synthesis.
        failed = (not hyp and (index == 0 or audible)) or (bool(hyp) and error > (0.45 if index == 0 else 0.50))
        # A short unsolicited prefix is diluted in whole/window CER. Look for
        # the opening reference phrase preceded by three or more extra chars.
        prefixes = []
        if start == 0:
            for ref in refs:
                blocks = SequenceMatcher(None, ref[:16], hyp, autojunk=False).get_matching_blocks()
                opening = next((b for b in blocks if b.a <= 2 and b.size >= 4), None)
                prefixes.append(opening is not None and opening.b - opening.a >= 3)
        extra_opening = bool(prefixes) and all(prefixes)
        foreign = any(lang != 'ja' for lang in languages)
        if failed or extra_opening:
            report['reasons'].append(f'{start / 16000:.1f}–{end / 16000:.1f}秒: '
                                     + ('日本語以外かつ原稿との不一致' if foreign and failed else
                                        '冒頭に原稿外の発話' if extra_opening else '原稿との不一致'))
        elif foreign or error > .20:
            report['warnings'].append(f'{start / 16000:.1f}–{end / 16000:.1f}秒: '
                                      + ('言語判定が不確実（原稿との一致を優先）' if foreign else
                                         '文字起こしに表記・読みの差があります。試聴で確認してください'))
        if index == 0:
            # Small numeric substitutions can be serious even with a very low CER.
            # ASR is not proof of a reading error: surface it without a retry loop.
            numeric_refs = [numeric_facts(r) for r in sources if numeric_facts(r)]
            observed = numeric_facts(text)
            if numeric_refs and observed and all(observed != r for r in numeric_refs):
                report['warnings'].append('日付・数値の文字起こしが原稿と異なります。該当箇所を試聴してください')
        report['windows'].append(dict(start=start / 16000, end=end / 16000,
                                      text=text, languages=languages, error=round(error, 4),
                                      spelling_error=round(spelling_error, 4),
                                      reading_error=round(reading_error, 4)))
    report.update(passed=not report['reasons'], elapsed_sec=round(time.monotonic() - started, 3))
    return report
