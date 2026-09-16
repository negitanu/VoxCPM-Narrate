"""Regression tests: no downloads, accelerator, or paid API calls."""

import io
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

from voxcpm_narrate.console import log_error
from voxcpm_narrate.extract import split_into_segments
from voxcpm_narrate.harness.judge import LlmJudge, _parse_judgement
from voxcpm_narrate.synthesize import prepare_reference_wav
from voxcpm_narrate.web import app as web
from voxcpm_narrate.web.jobs import Conflict, JobManager
from voxcpm_narrate.web.service import ProductionService, parse_script, version


class FakeModel:
    class tts_model:
        sample_rate = 16000

    def generate(self, **kwargs):
        return np.sin(np.arange(1600) * 0.1).astype("float32") * 0.1


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = JobManager(Path(self.tmp.name))
        self.service = ProductionService(self.manager)
        self.service._model = FakeModel()
        self.service._model_key = ("openbmb/VoxCPM2", "auto")
        self.patchers = [
            patch.object(web, "manager", self.manager),
            patch.object(web, "service", self.service),
            patch.object(web, "DEFAULT_OUT", Path(self.tmp.name)),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.client = TestClient(web.app)

    def tearDown(self):
        self.manager.close()
        for patcher in self.patchers:
            patcher.stop()
        self.tmp.cleanup()

    def create(self, script="最初の文です。\n\n次の文です。"):
        response = self.client.post("/api/jobs", data={"script": script, "mode": "plain"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def run_all(self, jid):
        res = self.client.post(f"/api/jobs/{jid}/generate", json={"request_id": "generate"})
        self.assertEqual(res.status_code, 200, res.text)
        self.manager._queue.join()
        job = self.manager.get(jid)
        self.assertEqual(job["status"], "done", job)
        return job

    def test_error_logging_does_not_raise(self):
        log_error("expected test error")

    def test_generation_records_prepared_input_and_float_audio(self):
        job = self.run_all(self.create("消費税は10%です。")['id'])
        seg = job["segments"][0]
        candidate = version(seg)
        self.assertIn("じゅうパーセント", candidate["model_input"])
        self.assertIn("speaking Japanese", candidate["model_input"])
        self.assertEqual(candidate["text_preparation"], "japanese-readings")
        self.assertEqual(sf.info(str(self.service.audio_path(job["id"], seg["id"]))).subtype,
                         "FLOAT")

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed")
    def test_finished_zip_chapters_match_finished_full_and_preserve_raw(self):
        import zipfile

        with patch.object(self.service._model, "generate", return_value=(
                np.sin(np.arange(64000) * .1).astype("float32") * .1)):
            job = self.run_all(self.create()["id"])
        jid = job["id"]
        raw = self.client.get(f"/api/jobs/{jid}/download").content
        finished = self.client.get(f"/api/jobs/{jid}/download?normalize=true")
        self.assertEqual(finished.status_code, 200, finished.text[:200] if finished.status_code != 200 else "")
        result = self.client.get(f"/api/jobs/{jid}/archive?normalize=true")
        self.assertEqual(result.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
            self.assertEqual(archive.read("originals/full.wav"), raw)
            self.assertEqual(archive.read("full.wav"), finished.content)
            full, sr = sf.read(io.BytesIO(archive.read("full.wav")))
            chapter, chapter_sr = sf.read(io.BytesIO(archive.read("chapters/01.wav")))
            self.assertEqual(sr, chapter_sr)
            np.testing.assert_array_equal(full, chapter)
            quality = json.loads(archive.read("quality.json"))
            self.assertLessEqual(abs(quality["after"]["integrated_lufs"] + 16), 1)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed")
    def test_short_audio_finishing_explains_error_but_raw_is_available(self):
        job = self.run_all(self.create("短文です。")["id"])
        result = self.client.get(f"/api/jobs/{job['id']}/download?normalize=true")
        self.assertEqual(result.status_code, 400)
        self.assertIn("3秒", result.json()["detail"])
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/download").status_code, 200)

    def test_split_preserves_text_and_enforces_bound(self):
        for text in ["あ" * 80, "長い文章、" * 30, "A sentence, with words. " * 10]:
            parts = split_into_segments(text, max_chars=20)
            self.assertTrue(all(len(part) <= 20 for part in parts))
            self.assertEqual("".join(parts).replace(" ", ""), text.replace(" ", ""))
        with self.assertRaises(ValueError):
            split_into_segments("test", max_chars=0)

    def test_ssml_alias_pause_and_control(self):
        jobs = parse_script(
            '<speak><p><sub alias="エーアイ">AI</sub>です。<break time="700ms"/><prosody rate="slow">説明します。</prosody></p></speak>',
            "ssml",
            120,
            "明瞭に",
        )
        self.assertIn("エーアイ", jobs[0]["text"])
        self.assertEqual(jobs[1]["pause_before_sec"], 0.7)
        self.assertIn("ゆっくり", jobs[1]["control"])

    def test_reference_wav_is_owned_copy(self):
        src = Path(self.tmp.name) / "input.wav"
        sf.write(src, np.zeros(100), 16000)
        dst = prepare_reference_wav(src, Path(self.tmp.name) / "run/reference.wav")
        src.unlink()
        self.assertTrue(dst.is_file())

    def test_preview_validation(self):
        self.assertEqual(
            self.client.post(
                "/api/preview", json={"script": "<speak>", "mode": "ssml"}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/preview", json={"script": "hi", "config": {"max_chars": 0}}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/api/jobs", data={"script": "hello", "config": '{"cfg_value":100}'}
            ).status_code,
            400,
        )

    def test_create_does_not_generate_or_require_reference(self):
        job = self.create()
        self.assertEqual(job["status"], "draft")
        self.assertEqual(job["current"], 0)
        self.assertEqual(len(job["segments"]), 2)
        self.assertIsNone(job["download_url"])

    def test_resume_skips_completed_segments(self):
        job = self.create()
        jid = job["id"]
        first = job["segments"][0]["id"]
        self.service.generate(jid, [first])
        before = self.manager.get(jid)["segments"][0]["accepted"]
        self.manager.update(jid, status="running")
        self.manager.close()
        self.manager = JobManager(Path(self.tmp.name))
        web.manager = self.manager
        self.service.manager = self.manager
        restored = self.manager.get(jid)
        self.assertEqual(restored["status"], "interrupted")
        done = self.run_all(jid)
        self.assertEqual(done["segments"][0]["accepted"], before)
        self.assertEqual(len(done["segments"][0]["versions"]), 1)
        self.assertIsNotNone(done["export"])

    def test_regeneration_failure_preserves_audio_and_manifest(self):
        job = self.run_all(self.create()["id"])
        jid = job["id"]
        seg = job["segments"][0]
        old_path = self.service.audio_path(jid, seg["id"])
        old_bytes = old_path.read_bytes()
        edit = dict(seg["draft"], text="修正した文です。")
        self.service.edit(jid, seg["id"], seg["revision"], edit)
        with patch.object(
            self.service._model, "generate", side_effect=RuntimeError("injected failure")
        ):
            self.manager.submit(
                jid,
                "regenerate",
                lambda key: self.service.regenerate(key, seg["id"]),
                request_id="regen",
            )
            self.manager._queue.join()
        after = self.manager.get(jid)
        self.assertEqual(after["status"], "error")
        self.assertEqual(after["segments"][0]["accepted"], seg["accepted"])
        self.assertEqual(old_path.read_bytes(), old_bytes)
        self.assertEqual(after["export"], job["export"])
        self.assertEqual(version(after["segments"][0])["text"], seg["draft"]["text"])

    def test_candidates_are_not_adopted_automatically_and_undo(self):
        job = self.run_all(self.create()["id"])
        jid = job["id"]
        seg = job["segments"][0]
        self.service.edit(jid, seg["id"], seg["revision"], dict(seg["draft"], text="修正後です。"))
        self.service.regenerate(jid, seg["id"])
        after = self.manager.get(jid)
        s = after["segments"][0]
        candidate = s["versions"][-1]
        self.assertEqual(s["accepted"], seg["accepted"])
        self.assertEqual(after["export"], job["export"])
        self.service.adopt(jid, s["id"], candidate["id"])
        adopted = self.manager.get(jid)
        self.assertEqual(version(adopted["segments"][0])["text"], "修正後です。")
        self.assertNotEqual(adopted["export"]["id"], job["export"]["id"])
        self.service.adopt(jid, s["id"], seg["accepted"])
        self.assertEqual(self.manager.get(jid)["export"]["id"], job["export"]["id"])

    def test_assembly_failure_does_not_commit_adoption(self):
        job = self.run_all(self.create()["id"])
        jid = job["id"]
        seg = job["segments"][0]
        self.service.regenerate(jid, seg["id"])
        candidate = self.manager.get(jid)["segments"][0]["versions"][-1]["id"]
        with patch(
            "voxcpm_narrate.web.service.assemble_full_wav", side_effect=OSError("disk failure")
        ):
            with self.assertRaises(OSError):
                self.service.adopt(jid, seg["id"], candidate)
        after = self.manager.get(jid)
        self.assertEqual(after["segments"][0]["accepted"], seg["accepted"])
        self.assertEqual(after["export"], job["export"])

    def test_revision_rejects_stale_edits(self):
        job = self.create()
        s = job["segments"][0]
        self.service.edit(job["id"], s["id"], 0, dict(s["draft"], text="new"))
        with self.assertRaises(Conflict):
            self.service.edit(job["id"], s["id"], 0, s["draft"])

    def test_duplicate_request_and_cancel(self):
        job = self.create()
        jid = job["id"]
        started = threading.Event()
        release = threading.Event()

        def work(key):
            started.set()
            release.wait(3)
            self.manager.checkpoint(key)

        self.manager.submit(jid, "test", work, request_id="same")
        self.assertTrue(started.wait(3))
        self.manager.submit(jid, "test", work, request_id="same")
        with self.assertRaises(Conflict):
            self.manager.submit(jid, "test", work, request_id="different")
        self.client.post(f"/api/jobs/{jid}/cancel")
        release.set()
        self.manager._queue.join()
        self.assertEqual(self.manager.get(jid)["status"], "interrupted")
        self.assertEqual(self.manager.get(jid)["requests"], ["same"])

    def test_unknown_segment_does_not_queue(self):
        job = self.create()
        r = self.client.post(
            f"/api/jobs/{job['id']}/segments/unknown/regenerate", json={"expected_revision": 0}
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.manager.get(job["id"])["status"], "draft")

    def test_download_and_zip_match_adopted_audio(self):
        job = self.run_all(self.create()["id"])
        jid = job["id"]
        r = self.client.get(f"/api/jobs/{jid}/download")
        self.assertEqual(r.status_code, 200)
        wav, sr = sf.read(io.BytesIO(r.content))
        self.assertEqual(sr, 16000)
        self.assertGreater(len(wav), 3200)
        import zipfile

        z = self.client.get(f"/api/jobs/{jid}/archive")
        self.assertEqual(z.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(z.content)) as archive:
            self.assertEqual(archive.read("full.wav"), r.content)
            saved = json.loads(archive.read("production.json"))
            self.assertEqual(saved["export"]["id"], job["export"]["id"])

    def test_dictionary_does_not_mutate_original_or_recurse(self):
        job = self.create("AIについて")
        self.manager.update(job["id"], dictionary={"AI": "エーアイ", "エーアイ": "禁止"})
        done = self.run_all(job["id"])
        seg = done["segments"][0]
        self.assertEqual(seg["original_text"], "AIについて")
        self.assertEqual(version(seg)["spoken_text"], "エーアイについて")

    def test_llm_failure_is_unavailable_not_point_five(self):
        for content in ["broken", '{"score":"bad"}', '{"score": NaN}', "{}"]:
            self.assertIsNone(_parse_judgement(content).score)
        self.assertIsNone(
            LlmJudge(api_key="").judge(text="hi", asr_transcript=None, metrics_summary="").score
        )

    def test_audio_feedback_is_bound_to_version_and_cached(self):
        job = self.run_all(self.create()["id"])
        jid = job["id"]
        sid = job["segments"][0]["id"]
        from voxcpm_narrate.harness.audio_judge import AudioJudgeResult

        with patch(
            "voxcpm_narrate.web.service.judge_audio_with_openrouter",
            return_value=AudioJudgeResult("ゆっくり", "修正", "Slow", "test"),
        ) as judge:
            self.service.audio_judge(jid, sid, "test", "secret")
            self.service.audio_judge(jid, sid, "test", "secret")
            self.assertEqual(judge.call_count, 1)
        saved = self.manager.get(jid)
        self.assertEqual(
            saved["segments"][0]["feedback"]["version_id"], saved["segments"][0]["accepted"]
        )
        self.assertNotIn("secret", json.dumps(saved))

    def test_model_is_reused(self):
        with patch("voxcpm_narrate.web.service.load_model", return_value=FakeModel()) as load:
            self.service._model_key = None
            self.run_all(self.create()["id"])
            self.service.regenerate(self.manager.list()[0]["id"], "01_001")
            self.assertEqual(load.call_count, 1)

    def test_non_seed_typeerror_not_retried(self):
        from voxcpm_narrate.synthesize import generate_wav

        model = FakeModel()
        with patch.object(model, "generate", side_effect=TypeError("internal problem")) as mock:
            with self.assertRaises(TypeError):
                generate_wav(
                    model,
                    text="test",
                    control="",
                    reference_audio=None,
                    cfg_value=2,
                    inference_timesteps=10,
                    normalize=True,
                    seed=42,
                )
            self.assertEqual(mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
