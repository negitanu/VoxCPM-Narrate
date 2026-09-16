"""Local, fail-closed content verification before publishing narration."""
import re
from difflib import SequenceMatcher
import time
import unicodedata

from voxcpm_narrate.harness.metrics import char_error_rate

GATE_VERSION = 'content-v1'


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
    report = dict(version=GATE_VERSION, passed=False, reasons=[], windows=[])
    if (wav.ndim != 1 or not len(wav) or not np.isfinite(wav).all()
            or len(wav) / sr > 120 or np.max(np.abs(wav)) < 1e-4):
        report['reasons'] = ['無音・不正な音声、または120秒を超える音声']
        return report
    divisor = gcd(sr, 16000)
    audio = resample_poly(wav, 16000 // divisor, sr // divisor)
    refs = [normalize(r) for r in references if normalize(r)]
    if not refs:
        raise ValueError('照合する原稿がありません')
    # Full transcript catches omissions/repetition; windows expose speech an ASR
    # decoder may silently omit when processing a long clip in one pass.
    ranges = [(0, len(audio))]
    if len(audio) > 6 * 16000:
        ranges += [(i, min(i + 6 * 16000, len(audio)))
                   for i in range(0, len(audio), 6 * 16000)]
    for index, (start, end) in enumerate(ranges):
        checkpoint()
        raw = asr.recognize(audio[start:end])
        languages = re.findall(r'<\|(ja|zh|en|ko|yue)\|>', raw)
        text = re.sub(r'<\|[^|]*\|>', '', raw).strip()
        hyp = normalize(text)
        error = min((char_error_rate(r, hyp) if index == 0 else local_error(r, hyp))
                    for r in refs)
        # Empty edge windows may contain only trailing silence; never allow an
        # empty full transcription to pass.
        audible = float(np.sqrt(np.mean(audio[start:end] ** 2))) > .005
        failed = (not hyp and (index == 0 or audible)) or (bool(hyp) and error > 0.30)
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
        if foreign or failed or extra_opening:
            report['reasons'].append(f'{start / 16000:.1f}–{end / 16000:.1f}秒: '
                                     + ('日本語以外を検出' if foreign else
                                        '冒頭に原稿外の発話' if extra_opening else '原稿との不一致'))
        report['windows'].append(dict(start=start / 16000, end=end / 16000,
                                      text=text, languages=languages, error=round(error, 4)))
    report.update(passed=not report['reasons'], elapsed_sec=round(time.monotonic() - started, 3))
    return report
