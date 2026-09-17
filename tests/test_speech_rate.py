import shutil
import unittest
from unittest.mock import Mock, patch

import numpy as np

from voxcpm_narrate.speech_rate import adjust_rate, measure_rate, mora_count, mora_estimate
from voxcpm_narrate.synthesize import generate_wav


class SpeechRateTests(unittest.TestCase):
    def test_tempo_tool_failure_keeps_original_and_passes(self):
        wav = np.ones(32000, dtype='float32') * .1
        with patch('voxcpm_narrate.speech_rate.shutil.which', return_value='/missing/ffmpeg'), \
             patch('voxcpm_narrate.speech_rate.subprocess.run', side_effect=OSError('failed')):
            result, report = adjust_rate(wav, 16000, 'あいうえおあいうえお', 5.4)
        self.assertIs(result, wav)
        self.assertTrue(report['passed'])
        self.assertTrue(report['warning'])
        self.assertFalse(report['applied'])

    def test_mora_readings(self):
        self.assertEqual(mora_count('きょう、がっこう。'), 6)
        self.assertEqual(mora_count('キャット'), 3)
        self.assertEqual(mora_count('コーヒー'), 4)
        self.assertEqual(mora_count('日本語'), 4)
        with self.assertRaises(ValueError):
            mora_count('…！？')

    def test_unregistered_words_are_estimated_not_rejected(self):
        # Product names and acronyms must not block the pace check; a reading can be
        # added to the dictionary later. Acronyms are spelled out, words rendered in kana.
        self.assertEqual(mora_count('AI'), 4)            # エーアイ
        self.assertEqual(mora_count('DX'), 6)            # ディーエックス
        self.assertEqual(mora_count('CoE'), 6)           # シーオーイー
        self.assertEqual(mora_count('Glean'), 4)         # グリーン
        self.assertEqual(mora_count('Nexthink'), 6)      # ネクスシンク
        self.assertEqual(mora_count('SharePoint'), 7)    # シェアポイント ≈ 6
        self.assertEqual(mora_count('Hub and Spoke'), 8)  # ハブアンドスポーク
        self.assertEqual(mora_count('PKSHA Technology'), 11 + 7)  # ピーケーエスエイチエー + テクノロジー ≈ 6
        estimate = mora_estimate('AIをTeamsで使います。')
        self.assertEqual(estimate['mora'], 4 + 1 + 4 + 1 + 5)
        self.assertEqual(estimate['estimated'], ('AI', 'Teams'))
        self.assertEqual(mora_estimate('日本語です。')['estimated'], ())
        self.assertEqual(mora_count('鷽です'), 4)  # rare kanji still convert via pykakasi

    def test_estimated_terms_are_reported_for_dictionary_follow_up(self):
        wav = np.ones(32000, dtype='float32') * .1
        _, report = adjust_rate(wav, 16000, 'AIを使います', 5)
        self.assertNotIn('unavailable', report)
        self.assertEqual(report['estimated_terms'], ['AI'])
        self.assertEqual(report['mora'], 4 + 6)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_bounded_stretch_preserves_pitch_and_edges(self):
        sr = 16000
        core = (.1 * np.sin(2 * np.pi * 220 * np.arange(sr * 2) / sr)).astype('float32')
        wav = np.concatenate([np.zeros(3200), core, np.zeros(3200)]).astype('float32')
        text = 'あいうえおあいうえお'
        result, report = adjust_rate(wav, sr, text, 5.4)
        self.assertTrue(report['passed'], report)
        self.assertTrue(report['applied'])
        self.assertLess(len(result), len(wav))
        self.assertLessEqual(abs(report['after'] / 5.4 - 1), .04)
        self.assertTrue(np.array_equal(result[:2000], wav[:2000]))
        self.assertTrue(np.array_equal(result[-2000:], wav[-2000:]))
        clip = result[6000:14000]
        peak = np.argmax(np.abs(np.fft.rfft(clip))) * sr / len(clip)
        self.assertLess(abs(peak - 220), 3)

    def test_large_deviation_is_advisory_without_stretch(self):
        wav = np.ones(32000, dtype='float32') * .1
        result, report = adjust_rate(wav, 16000, 'あいうえおあいうえお', 8)
        self.assertIs(result, wav)
        self.assertTrue(report['passed'])
        self.assertTrue(report['warning'])
        self.assertFalse(report['applied'])

    def test_sentence_pauses_do_not_count_as_speaking_time(self):
        # A three-sentence recording with long gaps must yield the same articulation
        # rate as the same speech without gaps; otherwise single generated sentences
        # would always look "too fast" against the reference.
        sr = 16000
        tone = (.1 * np.sin(2 * np.pi * 220 * np.arange(sr) / sr)).astype('float32')
        gap = np.zeros(int(sr * .6), dtype='float32')
        continuous = np.concatenate([tone, tone, tone])
        with_pauses = np.concatenate([tone, gap, tone, gap, tone])
        text = 'あいうえおあいうえおあいうえおあいうえお'
        plain = measure_rate(continuous, sr, text)
        paused = measure_rate(with_pauses, sr, text)
        self.assertAlmostEqual(plain['rate'], 20 / 3, delta=.1)
        self.assertAlmostEqual(paused['rate'], plain['rate'], delta=.1)
        self.assertGreater(paused['speech_sec'], paused['articulation_sec'])
        # Short breaths (< 250 ms) are ordinary speaking time and stay counted.
        breath = np.concatenate([tone, np.zeros(int(sr * .15), dtype='float32'), tone, tone])
        self.assertAlmostEqual(measure_rate(breath, sr, text)['rate'], 20 / 3.15, delta=.1)

    def test_no_correction_near_target(self):
        wav = np.ones(32000, dtype='float32') * .1
        result, report = adjust_rate(wav, 16000, 'あいうえおあいうえお', 5)
        self.assertIs(result, wav)
        self.assertTrue(report['passed'])
        self.assertFalse(report['applied'])

    def test_continuation_passes_exact_transcript_and_audio(self):
        model = Mock()
        model.generate.return_value = np.ones(10, dtype='float32')
        metadata = {}
        generate_wav(model, text='新しい本文です。', control='', reference_audio='voice.wav',
                     reference_transcript='えっと、今日は3人です。', cfg_value=2,
                     inference_timesteps=10, normalize=True, seed=42, input_metadata=metadata)
        args = model.generate.call_args.kwargs
        self.assertEqual(args['prompt_wav_path'], 'voice.wav')
        self.assertEqual(args['reference_wav_path'], 'voice.wav')
        self.assertEqual(args['prompt_text'], 'えっと、今日は3人です。')
        self.assertNotIn('えっと', args['text'])
        self.assertEqual(metadata['conditioning_mode'], 'continuation')

    def test_continuation_sends_only_the_script_as_target_text(self):
        # VoxCPM concatenates prompt_text + target_text, so any parenthesised English
        # instruction would be spoken aloud mid-sentence and replace the Japanese body.
        model = Mock()
        model.generate.return_value = np.ones(10, dtype='float32')
        metadata = {}
        generate_wav(model, text='消費税は10%です。', control='日本語、明瞭な声、自然な抑揚、会話に近いテンポ',
                     reference_audio='voice.wav', reference_transcript='録音の内容です。',
                     cfg_value=2, inference_timesteps=10, normalize=True, seed=1,
                     input_metadata=metadata)
        args = model.generate.call_args.kwargs
        self.assertEqual(args['text'], '消費税はじゅうパーセントです。')
        self.assertNotIn('(', args['text'])
        self.assertNotIn('speaking Japanese', args['text'])
        self.assertEqual(metadata['model_input'], args['text'])
        self.assertIsNone(metadata['voice_instruction'])

    def test_voice_reference_without_transcript_keeps_instruction(self):
        model = Mock()
        model.generate.return_value = np.ones(10, dtype='float32')
        metadata = {}
        generate_wav(model, text='本文です。', control='穏やかに', reference_audio='voice.wav',
                     reference_transcript='   ', cfg_value=2, inference_timesteps=10,
                     normalize=True, seed=1, input_metadata=metadata)
        args = model.generate.call_args.kwargs
        self.assertNotIn('prompt_text', args)
        self.assertTrue(args['text'].startswith('(A native Japanese speaker, speaking Japanese, 穏やかに)'))
        self.assertEqual(metadata['conditioning_mode'], 'voice_reference')

    def test_transcript_without_reference_audio_is_rejected(self):
        model = Mock()
        with self.assertRaises(ValueError):
            generate_wav(model, text='本文です。', control='', reference_audio=None,
                         reference_transcript='録音の内容です。', cfg_value=2,
                         inference_timesteps=10, normalize=True, seed=1)
        model.generate.assert_not_called()

    def test_legacy_reference_has_no_continuation(self):
        model = Mock()
        model.generate.return_value = np.ones(10, dtype='float32')
        generate_wav(model, text='本文です。', control='', reference_audio='voice.wav',
                     cfg_value=2, inference_timesteps=10, normalize=True, seed=42)
        self.assertNotIn('prompt_text', model.generate.call_args.kwargs)
