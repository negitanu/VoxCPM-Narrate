import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

from voxcpm_narrate.harness.content_gate import (
    GATE_VERSION, inspect_audio, inspection_ranges, local_error, normalize, phonetic,
)
from voxcpm_narrate.web.jobs import JobManager
from voxcpm_narrate.web.service import ProductionService
from voxcpm_narrate.web.app import Config


class ContentTests(unittest.TestCase):
    def test_short_tail_is_merged_and_short_clips_need_one_asr_call(self):
        for seconds in (6.04, 6.167, 8):
            self.assertEqual(inspection_ranges(round(seconds * 16000)),
                             [(0, round(seconds * 16000))])
        self.assertEqual(inspection_ranges(13 * 16000),
                         [(0, 208000), (0, 96000), (96000, 208000)])

    def test_date_and_loanword_readings_match_despite_language_tag(self):
        source = '2026年8月26日、デジタルの説明です。'
        reading = 'にせんにじゅうろくねんはちがつにじゅうろくにち、でじたるのせつめいです。'
        self.assertEqual(phonetic(source), phonetic(reading))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'clip.wav'
            sf.write(path, np.ones(16000 * 6) * .1, 16000)
            asr = Mock(recognize=Mock(return_value='<|en|>' + reading))
            report = inspect_audio(path, [source], asr)
            self.assertTrue(report['passed'], report)
            self.assertEqual(report['windows'][0]['reading_error'], 0)
            self.assertTrue(report['warnings'])
            asr.recognize.assert_called_once()

    def test_numeric_disagreement_is_visible_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'clip.wav'
            sf.write(path, np.ones(16000) * .1, 16000)
            source = '開催日は2026年8月26日から27日までです。'
            asr = Mock(recognize=Mock(return_value='<|ja|>' + source.replace('8月', '4月')))
            report = inspect_audio(path, [source], asr)
            self.assertTrue(report['passed'], report)
            self.assertTrue(any('日付・数値' in w for w in report['warnings']))

    def test_window_finds_hallucination_hidden_by_full_asr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'clip.wav'
            sf.write(path, np.ones(16000 * 19) * .1, 16000)
            asr = Mock()
            asr.recognize.side_effect = [
                '<|ja|>今回は説明します。', '<|ja|>今回は',
                '<|ja|>説明します', '<|ja|>メプロティですジャラム']
            report = inspect_audio(path, ['今回は説明します。'], asr)
            self.assertFalse(report['passed'])
            self.assertIn('12.0', report['reasons'][0])

    def test_valid_speech_and_silent_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'clip.wav'
            sf.write(path, np.concatenate([np.ones(16000 * 6) * .1, np.zeros(16000)]), 16000)
            asr = Mock()
            asr.recognize.side_effect = ['<|ja|>説明します', '<|ja|>説明します', '']
            self.assertTrue(inspect_audio(path, ['説明します'], asr)['passed'])

    def test_foreign_and_empty_full_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'clip.wav'
            sf.write(path, np.ones(16000) * .1, 16000)
            for raw in ['<|zh|>今天天气很好我们去公园', '']:
                self.assertFalse(inspect_audio(path, ['説明します'],
                                              Mock(recognize=Mock(return_value=raw)))['passed'])

    def test_short_unwanted_prefix_is_not_diluted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'clip.wav'
            sf.write(path, np.ones(16000) * .1, 16000)
            reference = '今回は単に参加したセッションの内容を紹介します'
            asr = Mock(recognize=Mock(return_value='<|ja|>まあレベがね' + reference))
            report = inspect_audio(path, [reference], asr)
            self.assertFalse(report['passed'])
            self.assertIn('冒頭', report['reasons'][0])

    def test_normalization_and_local_alignment(self):
        self.assertEqual(normalize('ＡＩ・カタカナ。'), 'aiかたかな')
        self.assertEqual(local_error('今回は説明します', '説明します'), 0)
        self.assertGreater(local_error('今回は説明します', 'ジャラム'), .3)


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = JobManager(Path(self.tmp.name))
        self.service = ProductionService(self.manager)
        model = Mock()
        model.tts_model.sample_rate = 16000
        model.revision = None
        self.service.model = Mock(return_value=model)
        self.generate = patch('voxcpm_narrate.web.service.generate_wav',
                              return_value=np.ones(16000, dtype='float32') * .1)
        self.generate_mock = self.generate.start()
        self.job = self.service.create('test', '説明します。', 'plain', Config().model_dump())
        self.jid = self.job['id']
        self.sid = self.job['segments'][0]['id']

    def tearDown(self):
        self.generate.stop()
        self.manager.close()
        self.tmp.cleanup()

    def result(self, passed):
        return dict(version=GATE_VERSION, passed=passed, reasons=[] if passed else ['不一致'], windows=[])

    def test_retry_then_cache_on_adopt_and_export(self):
        with patch('voxcpm_narrate.web.service.inspect_audio',
                   side_effect=[self.result(False), self.result(True)]) as check:
            self.service.generate(self.jid)
            job = self.manager.get(self.jid)
            self.assertEqual(len(job['segments'][0]['versions']), 2)
            self.assertEqual(job['segments'][0]['accepted'], job['segments'][0]['versions'][1]['id'])
            self.service.export_audio(self.jid)
            self.assertEqual(check.call_count, 2)

    def test_retries_bounded_and_manual_adoption_blocked(self):
        with patch('voxcpm_narrate.web.service.inspect_audio',
                   side_effect=lambda *a: self.result(False)) as check:
            message = self.service.generate(self.jid)
            job = self.manager.get(self.jid)
            self.assertIn('音声検査未合格 1 件', message)
            self.assertEqual(check.call_count, 3)
            self.assertEqual(self.generate_mock.call_count, 3)
            self.assertTrue(all(call.kwargs['retry_badcase'] is False
                                for call in self.generate_mock.call_args_list))
            self.assertIsNone(job['segments'][0]['accepted'])
            self.assertIsNone(job['export'])
            self.assertEqual(job['segments'][0]['inspection_issue'], '不一致')
            with self.assertRaises(ValueError):
                self.service.adopt(self.jid, self.sid, job['segments'][0]['versions'][0]['id'])

    def test_unavailable_stops_without_tts_retries(self):
        with patch('voxcpm_narrate.web.service.inspect_audio', side_effect=RuntimeError('offline')):
            self.service.generate(self.jid)
        seg = self.manager.get(self.jid)['segments'][0]
        self.assertEqual(len(seg['versions']), 1)
        self.assertTrue(seg['versions'][0]['content_check']['unavailable'])

    def test_retry_after_failed_inspection_clears_review_issue(self):
        with patch('voxcpm_narrate.web.service.inspect_audio',
                   side_effect=lambda *a: self.result(False)):
            self.service.generate(self.jid)
        with patch('voxcpm_narrate.web.service.inspect_audio',
                   side_effect=lambda *a: self.result(True)):
            self.service.generate(self.jid)
        job = self.manager.get(self.jid)
        self.assertIsNotNone(job['segments'][0]['accepted'])
        self.assertIsNone(job['segments'][0]['inspection_issue'])
        self.assertIsNotNone(job['export'])

    def test_failed_inspection_continues_to_next_segment(self):
        job = self.service.create('batch', '最初です。\n\n次です。', 'plain', Config().model_dump())
        first, second = [seg['id'] for seg in job['segments']]

        def inspect(path, *_args):
            return self.result(path.parent.name != first)

        with patch('voxcpm_narrate.web.service.inspect_audio', side_effect=inspect):
            self.manager.submit(job['id'], 'generate', self.service.generate, request_id='batch')
            self.manager._queue.join()
        saved = self.manager.get(job['id'])
        self.assertEqual(saved['status'], 'done')
        self.assertIn('音声検査未合格 1 件', saved['message'])
        self.assertEqual(len(saved['segments'][0]['versions']), 3)
        self.assertIsNone(saved['segments'][0]['accepted'])
        self.assertIsNotNone(saved['segments'][1]['accepted'])
        self.assertIsNone(saved['export'])

    def test_regenerate_all_inspects_every_segment_before_holding_adoption(self):
        job = self.service.create('batch', '最初です。\n\n次です。', 'plain', Config().model_dump())
        with patch('voxcpm_narrate.web.service.inspect_audio',
                   side_effect=lambda *a: self.result(True)):
            self.service.generate(job['id'])
        original = self.manager.get(job['id'])
        first = original['segments'][0]['id']

        def inspect(path, *_args):
            return self.result(path.parent.name != first)

        with patch('voxcpm_narrate.web.service.inspect_audio', side_effect=inspect):
            message = self.service.regenerate_all(job['id'])
        saved = self.manager.get(job['id'])
        self.assertIn('音声検査未合格 1 件', message)
        self.assertEqual([s['accepted'] for s in saved['segments']],
                         [s['accepted'] for s in original['segments']])
        self.assertEqual(saved['export'], original['export'])
        self.assertEqual(len(saved['segments'][0]['versions']), 4)
        self.assertEqual(len(saved['segments'][1]['versions']), 2)

    def test_failed_regeneration_preserves_existing_export(self):
        with patch('voxcpm_narrate.web.service.inspect_audio', side_effect=lambda *a: self.result(True)):
            self.service.generate(self.jid)
        original = self.manager.get(self.jid)
        with patch('voxcpm_narrate.web.service.inspect_audio', side_effect=lambda *a: self.result(False)):
            message = self.service.regenerate(self.jid, self.sid)
        self.assertIn('音声検査未合格', message)
        job = self.manager.get(self.jid)
        self.assertEqual(job['export'], original['export'])
        self.assertEqual(job['segments'][0]['accepted'], original['segments'][0]['accepted'])

    def test_legacy_audio_is_checked_on_export(self):
        with patch('voxcpm_narrate.web.service.inspect_audio', side_effect=lambda *a: self.result(True)):
            self.service.generate(self.jid)
        self.manager.mutate(self.jid, lambda j: j['segments'][0]['versions'][0].pop('content_check'))
        with patch('voxcpm_narrate.web.service.inspect_audio', side_effect=lambda *a: self.result(False)) as check:
            with self.assertRaises(ValueError):
                self.service.export_audio(self.jid)
            self.assertEqual(check.call_count, 1)

    def test_changed_audio_invalidates_cache(self):
        with patch('voxcpm_narrate.web.service.inspect_audio', side_effect=lambda *a: self.result(True)):
            self.service.generate(self.jid)
        path = self.service.audio_path(self.jid, self.sid)
        sf.write(path, np.ones(16000) * .2, 16000)
        with patch('voxcpm_narrate.web.service.inspect_audio', side_effect=lambda *a: self.result(False)) as check:
            with self.assertRaises(ValueError):
                self.service.export_audio(self.jid)
            self.assertEqual(check.call_count, 1)

    def test_legacy_pace_failure_does_not_block_or_retry(self):
        self.manager.mutate(self.jid, lambda j: j['config'].update(pace_mode='fixed', target_mora_rate=7))
        with patch('voxcpm_narrate.web.service.inspect_audio', side_effect=lambda *a: self.result(True)), \
             patch('voxcpm_narrate.speech_rate.adjust_rate', side_effect=lambda wav, *a: (wav, {
                 'passed': False, 'reason': '話速が目標から大きく外れています'})):
            self.service.generate(self.jid)
        seg = self.manager.get(self.jid)['segments'][0]
        self.assertEqual(len(seg['versions']), 1)
        self.assertIsNotNone(seg['accepted'])
        self.assertIn('話速', seg['versions'][0]['content_check']['warnings'][0])
        path = self.service.audio_path(self.jid, self.sid, seg['versions'][0]['id'])
        self.assertTrue(path.with_suffix('.raw.wav').is_file())
