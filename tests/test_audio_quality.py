"""Quality regressions, using local synthetic signals and optional FFmpeg."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

from voxcpm_narrate.artifacts import file_hash, write_wav
from voxcpm_narrate.audio_quality import finish, measure, prepare_segments
from voxcpm_narrate.extract import split_into_segments
from voxcpm_narrate.harness.metrics import char_error_rate
from voxcpm_narrate.synthesize import generate_wav
from voxcpm_narrate.text_input import prepare_input
from voxcpm_narrate.japanese import LEGACY_CONTROL, japanese_numbers


class InputTests(unittest.TestCase):
    def test_japanese_numbers_and_controls_are_preserved(self):
        text = "9月16日は、3.5キロを走ります。"
        with patch("voxcpm_narrate.text_input._normalizer") as normalizer:
            prepared = prepare_input(text, "速度は80%、自然に", True)
        normalizer.assert_not_called()
        self.assertIn("speaking Japanese", prepared["model_input"])
        self.assertIn("速度は80%、自然に", prepared["model_input"])
        self.assertEqual(prepared["prepared_reading"], "くがつじゅうろくにちは、さんてんごキロを走ります。")
        self.assertTrue(prepare_input("東京都", None, True)["model_input"].endswith("東京都"))

    def test_ascii_numbers_do_not_select_an_english_or_chinese_normalizer(self):
        fake = Mock()
        fake.normalize.return_value = "three items"
        with patch("voxcpm_narrate.text_input._normalizer", return_value=fake):
            prepared = prepare_input("3 items", "速度80%", True)
        fake.normalize.assert_not_called()
        self.assertEqual(prepared["prepared_reading"], "さん items")
        self.assertIn("速度80%", prepared["model_input"])

    def test_japanese_currency_percentage_time_and_model_names(self):
        self.assertEqual(japanese_numbers("12,800円、１０％、3時30分、4月1日。"),
                         "いちまんにせんはっぴゃくえん、じゅうパーセント、さんじさんじゅっぷん、しがつついたち。")
        self.assertEqual(japanese_numbers("VoxCPM2 v1.23"), "VoxCPM2 v1.23")
        self.assertEqual(japanese_numbers("8分、14分、100分"), "はっぷん、じゅうよんぷん、ひゃく分")

    def test_legacy_default_uses_supported_expressive_japanese_voice_instruction(self):
        prepared = prepare_input("説明します。", LEGACY_CONTROL, True)
        self.assertIn("natural expressive intonation", prepared["model_input"])
        self.assertNotIn(LEGACY_CONTROL, prepared["model_input"])
        self.assertEqual(prepared["output_language"], "ja")

    def test_benchmark_can_pass_already_prepared_input_unchanged(self):
        raw = "(old control)12,800円です。"
        self.assertEqual(prepare_input(raw, None, False, language="auto")["model_input"], raw)

    def test_recorded_input_matches_model_input(self):
        model = Mock()
        model.generate.return_value = np.zeros(10)
        metadata = {}
        generate_wav(model, text="スーパーです。", control="穏やかに", reference_audio=None,
                     cfg_value=2, inference_timesteps=10, normalize=True, seed=None,
                     input_metadata=metadata)
        self.assertEqual(model.generate.call_args.kwargs["text"], metadata["model_input"])
        self.assertFalse(model.generate.call_args.kwargs["normalize"])

    def test_long_vowel_is_not_discarded_by_cer(self):
        self.assertGreater(char_error_rate("スーパー", "スパ"), 0)
        self.assertEqual(char_error_rate("スーパー。", "スーパー"), 0)

    def test_decimal_stays_together_at_sentence_boundary(self):
        parts = split_into_segments("最初です。価格は3.5ドルです。次です。", max_chars=15)
        self.assertTrue(any("3.5" in part for part in parts))
        self.assertTrue(all(len(part) <= 15 for part in parts))


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sr = 24000

    def tearDown(self):
        self.tmp.cleanup()

    def tone(self, seconds=4, amplitude=.1):
        t = np.arange(round(seconds * self.sr)) / self.sr
        # A smooth envelope avoids introducing synthetic discontinuities.
        return (amplitude * np.sin(2 * np.pi * 220 * t) * np.minimum(1, t * 50)
                * np.minimum(1, (seconds - t) * 50)).astype(np.float32)

    def test_float_original_preserves_out_of_range_samples(self):
        path = self.root / "raw.wav"
        write_wav(path, np.array([-1.2, .1, 1.3], dtype=np.float32), self.sr)
        self.assertEqual(sf.info(str(path)).subtype, "FLOAT")
        np.testing.assert_allclose(sf.read(path)[0], [-1.2, .1, 1.3], atol=1e-6)

    def test_invalid_wave_does_not_replace_original(self):
        path = self.root / "raw.wav"
        write_wav(path, self.tone(), self.sr)
        original = file_hash(path)
        for bad in [np.array([]), np.array([np.nan]), np.array([np.inf])]:
            with self.assertRaises(ValueError):
                write_wav(path, bad, self.sr)
        self.assertEqual(file_hash(path), original)

    def test_phrase_gaps_account_for_generated_silence_and_gains_are_bounded(self):
        silence = np.zeros(self.sr)
        for name, amplitude in [("a", .1), ("b", .02)]:
            write_wav(self.root / f"{name}.wav",
                      np.concatenate([silence, self.tone(1, amplitude), silence]), self.sr)
        manifest = [{"id": "a", "pause_before_sec": .2}, {"id": "b", "pause_before_sec": .3}]
        report = prepare_segments(manifest, self.root, self.root / "prepared.wav")
        self.assertAlmostEqual(report["segments"][1]["actual_gap_sec"], .3, places=3)
        self.assertLess(sf.info(str(self.root / "prepared.wav")).duration, 3.2)
        self.assertTrue(all(0 <= item["gain_db"] <= 3 for item in report["segments"]))
        self.assertEqual(report["segments"][-1]["end_sample"],
                         sf.info(str(self.root / "prepared.wav")).frames)

    def test_silent_and_tiny_segments_are_not_amplified(self):
        for amplitude in [0, 1e-6]:
            write_wav(self.root / "a.wav", self.tone(amplitude=amplitude), self.sr)
            with self.assertRaises(ValueError):
                prepare_segments([{"id": "a"}], self.root, self.root / "prepared.wav")
        self.assertFalse((self.root / "prepared.wav").exists())

    def test_missing_ffmpeg_explains_original_is_available(self):
        path = self.root / "raw.wav"
        write_wav(path, self.tone(), self.sr)
        with patch("voxcpm_narrate.audio_quality.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "原音"):
                finish(path, self.root / "out.wav")
        self.assertFalse((self.root / "out.wav").exists())

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed")
    def test_finished_audio_meets_targets_and_retains_original(self):
        source, output = self.root / "raw.wav", self.root / "out.wav"
        write_wav(source, self.tone(), self.sr)
        original = file_hash(source)
        report = finish(source, output)
        self.assertEqual(file_hash(source), original)
        self.assertLessEqual(abs(report["after"]["integrated_lufs"] + 16), 1)
        self.assertLessEqual(report["after"]["true_peak_dbtp"], -1)
        self.assertEqual(sf.info(str(output)).samplerate, self.sr)
        self.assertEqual(sf.info(str(output)).subtype, "PCM_24")
        self.assertEqual(json.loads(output.with_suffix(".quality.json").read_text())[
            "output_sha256"], file_hash(output))

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed")
    def test_already_loud_audio_is_not_reduced_to_the_default_target(self):
        source, output = self.root / "loud.wav", self.root / "out.wav"
        write_wav(source, self.tone(amplitude=.4), self.sr)
        before = measure(source)
        report = finish(source, output)
        self.assertGreater(report["target_lufs"], -16)
        self.assertGreaterEqual(report["after"]["integrated_lufs"], before["integrated_lufs"] - .3)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed")
    def test_silence_measurement_is_null_and_short_audio_cannot_finish(self):
        source = self.root / "raw.wav"
        write_wav(source, np.zeros(self.sr), self.sr)
        report = measure(source)
        self.assertIsNone(report["integrated_lufs"])
        self.assertFalse(report["measurement_valid"])
        json.dumps(report, allow_nan=False)
        write_wav(source, self.tone(.2), self.sr)
        with self.assertRaisesRegex(ValueError, "3秒"):
            finish(source, self.root / "out.wav")


if __name__ == "__main__":
    unittest.main()
