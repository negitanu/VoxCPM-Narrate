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
from voxcpm_narrate.harness.content_gate import GATE_VERSION
from voxcpm_narrate.synthesize import prepare_reference_wav
from voxcpm_narrate.web import app as web
from voxcpm_narrate.web.jobs import Conflict, JobManager, safe_job_snapshot
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
            patch("voxcpm_narrate.web.service.inspect_audio", return_value={
                "version": GATE_VERSION, "passed": True, "reasons": [], "windows": []}),
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

    def test_continuation_requires_confirmation(self):
        wav = io.BytesIO()
        sf.write(wav, np.ones(16000, dtype="float32") * .1, 16000, format="WAV")
        data = {"script": "本文です。", "reference_transcript": "録音の内容です。"}
        files = {"reference": ("recording.wav", wav.getvalue(), "audio/wav")}
        rejected = self.client.post("/api/jobs", data=data, files=files)
        self.assertEqual(rejected.status_code, 400)
        data["transcript_confirmed"] = "true"
        accepted = self.client.post("/api/jobs", data=data, files=files)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["config"]["reference_transcript"], "録音の内容です。")
        self.service.generate(accepted.json()["id"])
        job = self.manager.get(accepted.json()["id"])
        self.assertEqual(job["segments"][0]["versions"][0]["conditioning_mode"], "continuation")

    def test_number_reading_option_reaches_model_and_survives_adoption(self):
        text = '開催日は2026年8月26日です。'
        created = self.client.post('/api/jobs', data={'script': text,
            'config': json.dumps({'convert_numbers': False})}).json()
        job = self.run_all(created['id'])
        seg = job['segments'][0]
        self.assertEqual(seg['versions'][0]['prepared_reading'], text)
        draft = {**seg['draft'], 'convert_numbers': True, 'number_reading_style': 'kanji'}
        self.service.edit(job['id'], seg['id'], seg['revision'], draft)
        self.service.regenerate(job['id'], seg['id'])
        candidate = self.manager.get(job['id'])['segments'][0]['versions'][-1]
        self.assertEqual(candidate['prepared_reading'],
                         '開催日は二千二十六年八月二十六日です。')
        self.service.adopt(job['id'], seg['id'], candidate['id'])
        self.assertTrue(self.manager.get(job['id'])['segments'][0]['draft']['convert_numbers'])
        self.assertEqual(self.manager.get(job['id'])['segments'][0]['draft']['number_reading_style'], 'kanji')

    def test_number_reading_preview_toggle_and_calendar_formats(self):
        text = '開催日は2026/08/26です。'
        for enabled in (True, False):
            response = self.client.post('/api/preview', json={'script': text,
                'config': {'convert_numbers': enabled}})
            self.assertEqual(response.status_code, 200, response.text)
            reading = response.json()['segments'][0]['prepared_reading']
            self.assertEqual(reading, '開催日は二千二十六年八月二十六日です。'
                             if enabled else text)

    def test_recheck_legacy_candidate_without_generating_or_changing_audio(self):
        job = self.run_all(self.create('説明します。')['id'])
        seg = job['segments'][0]
        vid = seg['versions'][0]['id']
        path = self.service.audio_path(job['id'], seg['id'], vid)
        original = path.read_bytes()
        self.manager.mutate(job['id'], lambda j: j['segments'][0]['versions'][0].update(
            content_check={'version': 'content-v1', 'passed': False, 'reasons': ['話速']}))
        url = f"/api/jobs/{job['id']}/segments/{seg['id']}/recheck"
        with patch.object(self.service, '_generate') as generate:
            response = self.client.post(url, json={'version_id': vid,
                'expected_revision': seg['revision'], 'request_id': 'recheck'})
            self.assertEqual(response.status_code, 200, response.text)
            self.manager._queue.join()
            generate.assert_not_called()
        checked = self.manager.get(job['id'])['segments'][0]['versions'][0]['content_check']
        self.assertEqual(checked['version'], GATE_VERSION)
        self.assertTrue(checked['passed'])
        self.assertEqual(path.read_bytes(), original)
        rejected = self.client.post(url, json={'version_id': vid,
            'expected_revision': seg['revision'] + 1, 'request_id': 'stale'})
        self.assertEqual(rejected.status_code, 409)

    def test_reference_pace_is_calculated_once_and_saved(self):
        wav = io.BytesIO()
        sf.write(wav, np.ones(16000 * 4, dtype="float32") * .1, 16000, format="WAV")
        result = self.client.post("/api/jobs", data={"script": "本文です。",
            "config": json.dumps({"pace_mode": "reference"}),
            "reference_transcript": "あいうえおあいうえおあいうえおあいうえお",
            "transcript_confirmed": "true"},
            files={"reference": ("recording.wav", wav.getvalue(), "audio/wav")})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertAlmostEqual(result.json()["config"]["target_mora_rate"], 5)

    def test_reference_pace_requires_transcript(self):
        result = self.client.post("/api/jobs", data={"script": "本文です。",
            "config": json.dumps({"pace_mode": "reference"})})
        self.assertEqual(result.status_code, 400)

    def test_reference_transcription_does_not_create_job(self):
        wav = io.BytesIO()
        sf.write(wav, np.ones(16000, dtype="float32") * .1, 16000, format="WAV")
        with patch.object(self.service._content_asr, "transcribe", return_value="実際の発話"):
            result = self.client.post("/api/reference-transcription", files={
                "reference": ("recording.wav", wav.getvalue(), "audio/wav")})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["text"], "実際の発話")
        self.assertEqual(self.manager.list(), [])

    def test_recorded_reference_keeps_script(self):
        wav = io.BytesIO()
        sf.write(wav, np.ones(16000, dtype="float32") * .1, 16000, format="WAV")
        result = self.client.post("/api/jobs", data={"script": "制作する台本です。",
            "reference_script": "録音時の例文です。"},
            files={"reference": ("recording.wav", wav.getvalue(), "audio/wav")})
        self.assertEqual(result.status_code, 200, result.text)
        job = self.manager.get(result.json()["id"])
        self.assertEqual(job["config"]["reference_script"], "録音時の例文です。")
        self.assertEqual(job["segments"][0]["draft"]["text"], "制作する台本です。")
        self.assertTrue((self.manager.job_dir(job["id"]) / "reference.wav").is_file())

    def test_reference_script_requires_audio(self):
        result = self.client.post("/api/jobs", data={"script": "本文です。",
                                                     "reference_script": "録音の例文"})
        self.assertEqual(result.status_code, 400)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
    def test_browser_webm_reference_is_converted(self):
        import subprocess
        path = Path(self.tmp.name) / "recording.webm"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", "-c:a", "libopus", str(path)], check=True)
        result = self.client.post("/api/jobs", data={"script": "本文です。"},
                                 files={"reference": (path.name, path.read_bytes(), "audio/webm")})
        self.assertEqual(result.status_code, 200, result.text)
        prepared = self.manager.job_dir(result.json()["id"]) / "reference.wav"
        self.assertEqual(sf.info(prepared).channels, 1)
        self.assertEqual(sf.info(prepared).samplerate, 16000)

    def test_regenerate_all_atomically_adopts_and_keeps_history(self):
        job = self.run_all(self.create()["id"])
        before = [s["accepted"] for s in job["segments"]]
        url = f"/api/jobs/{job['id']}/regenerate-all"
        response = self.client.post(url, json={"request_id": "all-again"})
        self.assertEqual(response.status_code, 200, response.text)
        self.manager._queue.join()
        after = self.manager.get(job["id"])
        self.assertEqual(after["status"], "done", after)
        for seg, prior in zip(after["segments"], before):
            self.assertNotEqual(seg["accepted"], prior)
            self.assertIn(prior, seg["history"])
            self.assertEqual(len(seg["versions"]), 2)
        self.assertNotEqual(after["export"]["id"], job["export"]["id"])
        self.client.post(url, json={"request_id": "all-again"})
        self.manager._queue.join()
        self.assertEqual(len(self.manager.get(job["id"])["segments"][0]["versions"]), 2)

    def test_regenerate_all_failure_does_not_partially_adopt(self):
        job = self.run_all(self.create()["id"])
        original = self.service._checked_generate
        calls = []
        def generate(jid, sid, **kwargs):
            calls.append(sid)
            if len(calls) == 2:
                raise ValueError("failed second segment")
            return original(jid, sid, **kwargs)
        with patch.object(self.service, "_checked_generate", side_effect=generate):
            with self.assertRaises(ValueError):
                self.service.regenerate_all(job["id"])
        after = self.manager.get(job["id"])
        self.assertEqual([s["accepted"] for s in after["segments"]],
                         [s["accepted"] for s in job["segments"]])
        self.assertEqual(after["export"], job["export"])
        self.assertEqual(len(after["segments"][0]["versions"]), 2)

    def test_regenerate_all_cancel_does_not_adopt(self):
        from voxcpm_narrate.web.jobs import Cancelled
        job = self.run_all(self.create()["id"])
        original = self.service._checked_generate
        def generate(jid, sid, **kwargs):
            result = original(jid, sid, **kwargs)
            self.manager.update(jid, cancel_requested=True)
            return result
        with patch.object(self.service, "_checked_generate", side_effect=generate):
            with self.assertRaises(Cancelled):
                self.service.regenerate_all(job["id"])
        self.assertEqual(self.manager.get(job["id"])["export"], job["export"])

    def test_trash_hides_audio_and_can_restore_after_restart(self):
        job = self.run_all(self.create("本文です。")["id"])
        jid = job["id"]
        sid = job["segments"][0]["id"]
        response = self.client.delete(f"/api/jobs/{jid}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/jobs").json()["jobs"], [])
        self.assertEqual(self.client.get("/api/jobs?deleted=true").json()["jobs"][0]["id"], jid)
        self.assertEqual(self.client.get(f"/api/jobs/{jid}/segments/{sid}").status_code, 404)
        restored_manager = JobManager(Path(self.tmp.name))
        try:
            self.assertEqual(restored_manager.list(), [])
            self.assertEqual(restored_manager.list(deleted=True)[0]["id"], jid)
        finally:
            restored_manager.close()
        self.assertEqual(self.client.post(f"/api/jobs/{jid}/restore").status_code, 200)
        self.assertEqual(self.client.get(f"/api/jobs/{jid}/segments/{sid}").status_code, 200)
        self.assertEqual(self.manager.get(jid)["export"], job["export"])

    def test_trash_rejects_processing_jobs(self):
        job = self.create()
        self.manager.update(job["id"], status="queued")
        self.assertEqual(self.client.delete(f"/api/jobs/{job['id']}").status_code, 409)
        self.manager.update(job["id"], status="done")
        self.manager._running_id = job["id"]
        self.assertEqual(self.client.delete(f"/api/jobs/{job['id']}").status_code, 409)
        self.manager._running_id = None

    def test_empty_trash_deletes_files_but_keeps_live_jobs_and_dictionary(self):
        removed = self.create("削除する制作です。")
        kept = self.create("保持する制作です。")
        folder = self.manager.job_dir(removed["id"])
        (folder / "audio.wav").write_bytes(b"test audio")
        self.manager.learn_reading(kept["id"], "用語", "ヨウゴ", shared=True, expected_revision=0)
        self.manager.delete(removed["id"])
        result = self.client.post("/api/trash/empty", json={
            "job_ids": [removed["id"]], "confirmed": True})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["deleted_ids"], [removed["id"]])
        self.assertFalse(folder.exists())
        self.assertEqual(self.manager.list(deleted=True), [])
        self.assertEqual(self.manager.get(kept["id"])["id"], kept["id"])
        self.assertEqual(self.manager.lexicon()["entries"]["用語"], "ヨウゴ")
        self.assertEqual(self.client.post(f"/api/jobs/{removed['id']}/restore").status_code, 404)

    def test_empty_trash_requires_confirmation_and_matching_snapshot(self):
        first = self.create()
        self.manager.delete(first["id"])
        result = self.client.post("/api/trash/empty", json={"job_ids": [first["id"]]})
        self.assertEqual(result.status_code, 400)
        second = self.create()
        self.manager.delete(second["id"])
        stale = self.client.post("/api/trash/empty", json={"job_ids": [first["id"]], "confirmed": True})
        self.assertEqual(stale.status_code, 409)
        self.assertTrue(self.manager.job_dir(first["id"]).exists())
        self.assertEqual(len(self.manager.list(deleted=True)), 2)

    def test_empty_trash_failure_can_retry_but_cannot_restore_partial_files(self):
        job = self.create()
        self.manager.delete(job["id"])
        with patch("voxcpm_narrate.web.jobs.shutil.rmtree", side_effect=PermissionError("test")):
            result = self.manager.empty_trash([job["id"]])
        self.assertEqual(result["failed_ids"], [job["id"]])
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/restore").status_code, 409)
        self.assertTrue(self.manager.get(job["id"], include_deleted=True)["purging"])
        result = self.manager.empty_trash([job["id"]])
        self.assertEqual(result["deleted_ids"], [job["id"]])

    def test_empty_trash_unlinks_symlink_without_touching_target(self):
        job = self.create()
        folder = self.manager.job_dir(job["id"])
        folder.rmdir()
        target = Path(self.tmp.name) / "unrelated"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        folder.symlink_to(target, target_is_directory=True)
        self.manager.delete(job["id"])
        self.manager.empty_trash([job["id"]])
        self.assertEqual((target / "keep.txt").read_text(), "keep")
        self.assertFalse(folder.is_symlink())

    def test_rename_preserves_audio_and_updates_library(self):
        job = self.run_all(self.create("本文です。")["id"])
        result = self.client.patch(f"/api/jobs/{job['id']}/title", json={
            "title": "  改訂版ナレーション  ", "expected_title": job["title"]})
        self.assertEqual(result.status_code, 200, result.text)
        changed = result.json()
        self.assertEqual(changed["title"], "改訂版ナレーション")
        self.assertEqual(changed["segments"], job["segments"])
        self.assertEqual(changed["export"], job["export"])
        self.assertEqual(self.client.get("/api/jobs").json()["jobs"][0]["title"], "改訂版ナレーション")
        stale = self.client.patch(f"/api/jobs/{job['id']}/title", json={
            "title": "古い編集", "expected_title": job["title"]})
        self.assertEqual(stale.status_code, 409)

    def test_rename_validation_and_processing(self):
        job = self.create()
        url = f"/api/jobs/{job['id']}/title"
        for title in ["   ", "x" * 151]:
            self.assertIn(self.client.patch(url, json={"title": title,
                "expected_title": job["title"]}).status_code, [400, 422])
        self.manager.update(job["id"], status="running")
        result = self.client.patch(url, json={"title": "処理中の新しい名前", "expected_title": job["title"]})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["status"], "running")
        self.manager.update(job["id"], status="done")
        self.manager.delete(job["id"])
        self.assertEqual(self.client.patch(url, json={"title": "名前", "expected_title": "処理中の新しい名前"}).status_code, 404)

    def test_error_logging_does_not_raise(self):
        log_error("expected test error")

    def test_reading_candidates_use_edited_drafts_and_not_markup(self):
        job = self.create("東京支社です。")
        sid = job["segments"][0]["id"]
        self.service.edit(job["id"], sid, 0,
                          {**job["segments"][0]["draft"], "text": "VoxCPMです。"})
        result = self.client.post(f"/api/jobs/{job['id']}/pronunciation-candidates", json={}).json()
        self.assertEqual([c["term"] for c in result["candidates"]], ["VoxCPM"])
        self.assertEqual(result["candidates"][0]["segment_revision"], 1)
        ssml = self.client.post("/api/jobs", data={"mode": "ssml", "script":
            '<speak><p><sub alias="エーアイ">AI</sub>です。</p></speak>'}).json()
        terms = [c["term"] for c in self.service.pronunciation_candidates(ssml["id"])]
        self.assertNotIn("speak", terms)
        self.assertNotIn("alias", terms)
        self.assertNotIn("AI", terms)

    def test_shared_reading_is_durable_and_is_copied_to_new_jobs(self):
        job = self.create("VoxCPMです。")
        response = self.client.post(f"/api/jobs/{job['id']}/pronunciation-learn", json={
            "term": "VoxCPM", "reading": "ボックスシーピーエム", "shared": True,
            "expected_revision": 0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dictionary"]["VoxCPM"], "ボックスシーピーエム")
        restored = JobManager(Path(self.tmp.name))
        try:
            self.assertEqual(restored.lexicon()["entries"]["VoxCPM"], "ボックスシーピーエム")
        finally:
            restored.close()
        new_job = self.create("VoxCPMです。")
        self.assertEqual(new_job["dictionary"]["VoxCPM"], "ボックスシーピーエム")
        registered = self.client.post(f"/api/jobs/{new_job['id']}/pronunciation-candidates",
                                       json={"include_registered": True}).json()["candidates"]
        self.assertEqual(registered[0]["status"], "known")
        self.assertEqual(registered[0]["reading"], "ボックスシーピーエム")
        stale = self.client.post(f"/api/jobs/{job['id']}/pronunciation-learn", json={
            "term": "VoxCPM", "reading": "ちがうよみ", "shared": True, "expected_revision": 0})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.manager.get(job["id"])["dictionary"]["VoxCPM"], "ボックスシーピーエム")

    def test_dictionary_import_preserves_local_readings(self):
        job = self.create("東京です。")
        self.manager.update(job["id"], dictionary={"東京": "とうきょう"})
        other = self.create("東京です。")
        self.manager.learn_reading(other["id"], "東京", "トーキョー", shared=True, expected_revision=0)
        response = self.client.post(f"/api/jobs/{job['id']}/pronunciation-import")
        self.assertEqual(response.json()["dictionary"]["東京"], "とうきょう")

    def test_reading_preview_uses_queue_without_adopting_or_saving_reading(self):
        job = self.run_all(self.create("VoxCPMを使います。")["id"])
        sid = job["segments"][0]["id"]
        body = {"term": "VoxCPM", "reading": "ボックスシーピーエム", "segment_id": sid,
                "expected_revision": job["segments"][0]["revision"], "request_id": "preview-reading"}
        with patch.object(self.service._model, "generate", wraps=self.service._model.generate) as generate:
            response = self.client.post(f"/api/jobs/{job['id']}/pronunciation-preview", json=body)
            self.assertEqual(response.status_code, 200)
            self.manager._queue.join()
            self.assertIn("ボックスシーピーエム", generate.call_args.kwargs["text"])
        updated = self.manager.get(job["id"])
        self.assertEqual(updated["dictionary"], job["dictionary"])
        self.assertEqual(updated["segments"], job["segments"])
        self.assertEqual(updated["export"], job["export"])
        preview_id = updated["pronunciation_preview"]["id"]
        audio = self.client.get(f"/api/jobs/{job['id']}/pronunciation-preview/{preview_id}")
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/pronunciation-preview/wrong").status_code, 404)
        body["request_id"] = "stale-preview"
        body["expected_revision"] = 99
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/pronunciation-preview", json=body).status_code, 409)

    def test_invalid_reading_and_busy_jobs_do_not_change_dictionaries(self):
        job = self.create("VoxCPMです。")
        url = f"/api/jobs/{job['id']}/pronunciation-learn"
        body = {"term": "VoxCPM", "reading": "English123", "shared": True}
        self.assertEqual(self.client.post(url, json=body).status_code, 400)
        body["reading"] = "ボックスシーピーエム"
        self.manager.update(job["id"], status="running")
        self.assertEqual(self.client.post(url, json=body).status_code, 409)
        self.assertEqual(self.manager.lexicon()["entries"], {})

    def test_public_snapshot_removes_server_paths_and_internal_errors(self):
        private = "/private/example/model"
        snapshot = safe_job_snapshot(
            {
                "config": {"reference_audio": private, "model_id": private},
                "error": f"failed at {private}",
                "segments": [
                    {
                        "error": f"failed at {private}",
                        "versions": [{"runtime": {"model_source": private}}],
                    }
                ],
            }
        )
        encoded = json.dumps(snapshot)
        self.assertNotIn("/private/example", encoded)
        self.assertNotIn("reference_audio", encoded)
        self.assertNotIn("model_source", encoded)
        self.assertEqual(snapshot["config"]["model_id"], "local-model")

    def test_generation_records_prepared_input_and_float_audio(self):
        job = self.run_all(self.create("消費税は10%です。")['id'])
        seg = job["segments"][0]
        candidate = version(seg)
        self.assertIn("十パーセント", candidate["model_input"])
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

    def test_reference_path_is_internal_and_never_returned(self):
        recording = io.BytesIO()
        sf.write(recording, np.zeros(16000 * 6), 16000, format="WAV")
        recording.seek(0)
        response = self.client.post(
            "/api/jobs",
            data={"script": "参照音声を使います。", "mode": "plain"},
            files={"reference": ("voice.wav", recording, "audio/wav")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        public_job = response.json()
        stored = self.manager.get(public_job["id"])
        self.assertNotIn("reference_audio", public_job["config"])
        self.assertEqual(stored["config"]["reference_audio"], "reference.wav")
        self.assertNotIn(str(Path(self.tmp.name).resolve()), response.text)
        self.run_all(public_job["id"])
        archive_response = self.client.get(f"/api/jobs/{public_job['id']}/archive")
        self.assertEqual(archive_response.status_code, 200)
        import zipfile

        with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
            self.assertIn("reference.wav", archive.namelist())
            production = archive.read("production.json").decode()
            self.assertNotIn("reference_audio", production)
            self.assertNotIn(str(Path(self.tmp.name).resolve()), production)

    def test_legacy_absolute_reference_path_is_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = JobManager(root)
            service = ProductionService(manager)
            job = service.create(
                "移行テスト",
                "本文です。",
                "plain",
                {"max_chars": 120, "control": ""},
                [{"id": "01_001", "section": "本文", "text": "本文です。"}],
            )
            reference = manager.job_dir(job["id"]) / "reference.wav"
            reference.write_bytes(b"test")
            manager.update(job["id"], config={**job["config"], "reference_audio": str(reference)})
            manager.close()
            manager = JobManager(root)
            try:
                migrated = manager.get(job["id"])
                self.assertEqual(migrated["config"]["reference_audio"], "reference.wav")
                self.assertNotIn(str(root.resolve()), json.dumps(migrated))
            finally:
                manager.close()

    def test_remote_bind_requires_explicit_opt_in(self):
        with patch.dict(
            web.os.environ,
            {"VOXCPM_WEB_HOST": "0.0.0.0", "VOXCPM_ALLOW_REMOTE": ""},
        ):
            with self.assertRaises(SystemExit):
                web.main()
        with (
            patch.dict(
                web.os.environ,
                {"VOXCPM_WEB_HOST": "0.0.0.0", "VOXCPM_ALLOW_REMOTE": "1"},
            ),
            patch("uvicorn.run") as run,
        ):
            web.main()
        self.assertEqual(run.call_args.kwargs["host"], "0.0.0.0")

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
