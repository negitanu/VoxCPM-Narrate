import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

from voxcpm_narrate.harness.content_gate import inspect_audio, local_error, normalize
from voxcpm_narrate.web.jobs import JobManager
from voxcpm_narrate.web.service import ProductionService
from voxcpm_narrate.web.app import Config


class ContentTests(unittest.TestCase):
    def test_window_finds_hallucination_hidden_by_full_asr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'clip.wav'
            sf.write(path, np.ones(16000 * 13) * .1, 16000)
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
            for raw in ['<|zh|>説明します', '']:
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
        self.generate.start()
        self.job = self.service.create('test', '説明します。', 'plain', Config().model_dump())
        self.jid = self.job['id']
        self.sid = self.job['segments'][0]['id']

    def tearDown(self):
        self.generate.stop()
        self.manager.close()
        self.tmp.cleanup()

    def result(self, passed):
        return dict(version='content-v1', passed=passed, reasons=[] if passed else ['不一致'], windows=[])

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
            with self.assertRaises(ValueError):
                self.service.generate(self.jid)
            job = self.manager.get(self.jid)
            self.assertEqual(check.call_count, 4)
            self.assertIsNone(job['segments'][0]['accepted'])
            self.assertIsNone(job['export'])
            with self.assertRaises(ValueError):
                self.service.adopt(self.jid, self.sid, job['segments'][0]['versions'][0]['id'])

    def test_unavailable_stops_without_tts_retries(self):
        with patch('voxcpm_narrate.web.service.inspect_audio', side_effect=RuntimeError('offline')):
            with self.assertRaises(ValueError):
                self.service.generate(self.jid)
        seg = self.manager.get(self.jid)['segments'][0]
        self.assertEqual(len(seg['versions']), 1)
        self.assertTrue(seg['versions'][0]['content_check']['unavailable'])

    def test_failed_regeneration_preserves_existing_export(self):
        with patch('voxcpm_narrate.web.service.inspect_audio', side_effect=lambda *a: self.result(True)):
            self.service.generate(self.jid)
        original = self.manager.get(self.jid)
        with patch('voxcpm_narrate.web.service.inspect_audio', side_effect=lambda *a: self.result(False)):
            with self.assertRaises(ValueError):
                self.service.regenerate(self.jid, self.sid)
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
