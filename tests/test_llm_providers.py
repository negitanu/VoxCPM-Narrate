"""Provider routing tests; no external API requests."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voxcpm_narrate.harness.audio_judge import judge_audio_with_openrouter
from voxcpm_narrate.harness.judge import (
    LlmJudge,
    azure_v1_base_url,
    chat_completion_request,
    default_llm_api_key,
    default_llm_model,
)


def response(content):
    body = {"choices": [{"message": {"content": json.dumps(content)}}]}
    return io.BytesIO(json.dumps(body).encode())


def headers(request):
    return {key.lower(): value for key, value in request.header_items()}


class ProviderTests(unittest.TestCase):
    def test_azure_endpoint_normalization_and_validation(self):
        base = "https://example.openai.azure.com/openai/v1"
        self.assertEqual(azure_v1_base_url("https://example.openai.azure.com/"), base)
        self.assertEqual(azure_v1_base_url(base + "/"), base)
        for value in ("http://example.openai.azure.com", "https://example.openai.azure.com/other",
                      "https://user:pass@example.openai.azure.com/", base + "?api-version=1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                azure_v1_base_url(value)

    def test_provider_keys_and_deployments_do_not_mix(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "router-key",
                                     "AZURE_OPENAI_API_KEY": "azure-key",
                                     "AZURE_OPENAI_TEXT_DEPLOYMENT": "text-deployment"}):
            self.assertEqual(default_llm_api_key("openrouter"), "router-key")
            self.assertEqual(default_llm_api_key("azure"), "azure-key")
            self.assertEqual(default_llm_model("azure"), "text-deployment")

    def test_azure_request_uses_api_key_and_deployment(self):
        request = chat_completion_request(
            provider="azure", base_url="https://example.openai.azure.com/",
            api_key="azure-key", payload={"model": "text-deployment", "messages": []},
            app_title="test",
        )
        self.assertEqual(request.full_url,
                         "https://example.openai.azure.com/openai/v1/chat/completions")
        self.assertEqual(headers(request)["api-key"], "azure-key")
        self.assertNotIn("authorization", headers(request))
        self.assertNotIn("http-referer", headers(request))
        self.assertEqual(json.loads(request.data)["model"], "text-deployment")

    def test_openrouter_request_still_uses_bearer(self):
        request = chat_completion_request(
            provider="openrouter", base_url="https://openrouter.ai/api/v1",
            api_key="router-key", payload={"model": "model-id", "messages": []},
            app_title="test",
        )
        self.assertEqual(headers(request)["authorization"], "Bearer router-key")
        self.assertNotIn("api-key", headers(request))

    def test_azure_text_judge_uses_v1_without_temperature(self):
        judge = LlmJudge(provider="azure", base_url="https://example.openai.azure.com/",
                         model="text-deployment", api_key="azure-key")
        with patch("voxcpm_narrate.harness.judge.urllib.request.urlopen",
                   return_value=response({"score": 0.8, "awkward": False, "reason": "natural"})) as urlopen:
            result = judge.judge(text="本文", asr_transcript=None, metrics_summary="metrics")
        self.assertEqual(result.score, 0.8)
        self.assertEqual(headers(urlopen.call_args.args[0])["api-key"], "azure-key")
        self.assertNotIn("temperature", json.loads(urlopen.call_args.args[0].data))

    def test_azure_audio_judge_skips_openrouter_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "sample.wav"
            audio.write_bytes(b"RIFF" + b"\0" * 40)
            with patch("voxcpm_narrate.harness.audio_judge.fetch_openrouter_models",
                       side_effect=AssertionError("OpenRouter catalog queried")):
                with patch("voxcpm_narrate.harness.audio_judge.urllib.request.urlopen",
                           return_value=response({"feedback": "自然", "revised_text": "本文",
                                                  "voice_design_prompt": "Natural voice"})) as urlopen:
                    result = judge_audio_with_openrouter(
                        audio_path=audio, original_text="本文", model="audio-deployment",
                        api_key="azure-key", base_url="https://example.openai.azure.com/",
                        provider="azure",
                    )
            request = urlopen.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(result.feedback, "自然")
            self.assertEqual(payload["model"], "audio-deployment")
            self.assertNotIn("temperature", payload)
            self.assertEqual(payload["messages"][1]["content"][1]["type"], "input_audio")
            self.assertEqual(headers(request)["api-key"], "azure-key")


if __name__ == "__main__":
    unittest.main()
