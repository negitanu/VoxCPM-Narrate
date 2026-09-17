"""Conservative average Japanese speech-rate measurement and bounded correction."""
from functools import lru_cache
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata

import numpy as np
import soundfile as sf

from voxcpm_narrate.audio_quality import _speech_bounds
from voxcpm_narrate.japanese import japanese_numbers


@lru_cache(maxsize=1)
def reader():
    from pykakasi import kakasi
    return kakasi()


# Mora of each spelled-out Latin letter as read in Japanese (エー, ビー, ... エイチ, ダブリュー).
LETTER_MORA = dict(zip('ABCDEFGHIJKLMNOPQRSTUVWXYZ',
                       [2, 2, 2, 2, 2, 2, 2, 3, 2, 2, 2, 2, 2, 2, 2, 2, 3, 3, 2, 3, 2, 2, 5, 4, 2, 3]))
# Katakana renderings of English words carry roughly 0.7 mora per letter
# (Glean→グリーン 4/5, Nexthink→ネクスシンク 6/8, Technology→テクノロジー 6/10).
WORD_MORA_PER_LETTER = .7
LATIN = re.compile(r'[A-Za-z]+')
CAMEL = re.compile(r'[A-Z]+(?![a-z])|[A-Z]?[a-z]+')


def _latin_mora(token):
    """Estimate mora for a Latin token: acronyms are spelled, words are rendered in kana."""
    if len(token) <= 4 and sum(c.isupper() for c in token) * 2 >= len(token):
        return sum(LETTER_MORA[c.upper()] for c in token)
    spelled, letters = 0, 0
    for part in CAMEL.findall(token):
        if part.isupper():
            spelled += sum(LETTER_MORA[c] for c in part)
        else:
            letters += len(part)
    return spelled + (max(1, round(letters * WORD_MORA_PER_LETTER)) if letters else 0)


@lru_cache(maxsize=2048)
def mora_estimate(text):
    """Count mora from readings; estimate unconverted words instead of refusing to measure.

    Returns ``{'mora': int, 'estimated': [term, ...]}``. Words without a dictionary
    reading (product names, acronyms, rare kanji) get a conservative estimate so the
    pace check keeps working; the terms are reported so a reading can be added later.
    """
    reading = ''.join(x['hira'] for x in reader().convert(japanese_numbers(text)))
    reading = unicodedata.normalize('NFKC', reading)
    count = 0
    estimated = []
    for token in LATIN.findall(reading):
        count += _latin_mora(token)
        if token not in estimated:
            estimated.append(token)
    reading = LATIN.sub(' ', reading)
    previous_kana = False
    for c in reading:
        if 'ァ' <= c <= 'ヶ':
            c = chr(ord(c) - 0x60)
        if 'ぁ' <= c <= 'ゖ' or c == 'ー':
            if c not in 'ぁぃぅぇぉゃゅょゎ' or not previous_kana:
                count += 1
            previous_kana = True
        else:
            if unicodedata.category(c)[0] in 'LN':
                # Unconverted kanji or a stray digit: typically two mora each.
                count += 2
                if c not in estimated:
                    estimated.append(c)
            previous_kana = False
    if not count:
        raise ValueError('話速を測定できる日本語の読みがありません')
    return {'mora': count, 'estimated': tuple(estimated)}


def mora_count(text):
    return mora_estimate(text)['mora']


PAUSE_SEC = .25


def _pause_seconds(wav, sr, start, end):
    """Total duration of pauses of ``PAUSE_SEC`` or longer inside the speech span.

    A multi-sentence reference recording contains long sentence gaps that a single
    generated sentence never has, so averaging over the whole span would always call
    the generated audio "too fast". Comparing articulation time keeps both sides alike.
    """
    frame = max(1, round(sr * .01))
    speech = wav[start:end]
    padded = np.pad(speech, (0, (-len(speech)) % frame))
    rms = np.sqrt(np.mean(padded.reshape(-1, frame).astype(np.float64) ** 2, axis=1))
    silent = np.concatenate([[False], rms <= max(1e-5, float(np.max(rms)) * .05), [False]])
    edges = np.flatnonzero(np.diff(silent.astype(np.int8)))
    runs = (edges[1::2] - edges[0::2]) * frame / sr
    return float(runs[runs >= PAUSE_SEC].sum())


def measure_rate(wav, sr, text):
    start, end, _ = _speech_bounds(wav, sr)
    counted = mora_estimate(text)
    duration = (end - start) / sr
    articulation = duration - _pause_seconds(wav, sr, start, end)
    return dict(mora=counted['mora'], estimated_terms=list(counted['estimated']),
                speech_sec=duration, articulation_sec=articulation,
                rate=counted['mora'] / articulation, start=start, end=end)


def reference_rate(path, text):
    wav, sr = sf.read(path, dtype='float32')
    result = measure_rate(wav, sr, text)
    if result['speech_sec'] < 3 or result['mora'] < 10:
        raise ValueError('話速の基準には3秒以上の日本語の録音を使用してください')
    return result['rate']


def adjust_rate(wav, sr, text, target):
    started = time.monotonic()
    report = dict(target=target, passed=True, applied=False, factor=1.0, advisory=True)
    try:
        measured = measure_rate(wav, sr, text)
    except ValueError as exc:
        return wav, {**report, 'warning': True, 'reason': '話速補正を省略: ' + str(exc)}
    report.update(before=measured['rate'], mora=measured['mora'],
                  estimated_terms=measured['estimated_terms'])
    if measured['estimated_terms']:
        return wav, {**report, 'warning': True, 'reason': '推定読みを含むため話速補正を省略'}
    if measured['mora'] < 8 or measured['speech_sec'] < 1:
        return wav, {**report, 'passed': True, 'reason': '短文のため話速補正を省略'}
    factor = target / measured['rate']
    report['factor'] = factor
    if not .9 <= factor <= 1.1:
        return wav, {**report, 'warning': True,
                     'reason': '目標話速との差を許容し原音を使用（補正上限±10%）'}
    if abs(factor - 1) <= .03:
        return wav, {**report, 'passed': True, 'after': measured['rate'], 'reason': '目標の許容範囲内'}
    executable = shutil.which('ffmpeg')
    if not executable:
        return wav, {**report, 'warning': True, 'reason': 'ffmpegがないため話速補正を省略'}
    # Preserve edge silence and the separately assembled sentence/paragraph gaps.
    left = max(0, measured['start'] - round(.04 * sr))
    right = min(len(wav), measured['end'] + round(.04 * sr))
    try:
        with tempfile.TemporaryDirectory(prefix='narrate-tempo-') as tmp:
            source, dest = Path(tmp) / 'source.wav', Path(tmp) / 'tempo.wav'
            sf.write(source, wav[left:right], sr, subtype='FLOAT')
            subprocess.run([executable, '-v', 'error', '-nostdin', '-y', '-i', str(source),
                            '-af', f'atempo={factor:.8f}', '-c:a', 'pcm_f32le', str(dest)],
                           check=True, capture_output=True, timeout=30)
            corrected, _ = sf.read(dest, dtype='float32')
        if not len(corrected) or not np.isfinite(corrected).all():
            raise ValueError('不正な補正音声')
        result = np.concatenate([wav[:left], corrected, wav[right:]])
        after = measure_rate(result, sr, text)['rate']
    except (OSError, ValueError, subprocess.SubprocessError):
        return wav, {**report, 'warning': True, 'reason': '話速補正に失敗したため原音を使用'}
    report.update(applied=True, after=after, warning=abs(after / target - 1) > .10,
                  reason='話速を補正（目標との残差は許容）',
                  elapsed_sec=round(time.monotonic() - started, 3))
    return result, report
