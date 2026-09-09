import json
import os
import tempfile
import unittest
from unittest.mock import patch

from vvc.rtc.api import _find_value
from vvc.rtc.token import create_rtc_token
from vvc.rtc.voice_chat import build_start_request, format_subtitle
from vvc.shared.voiceprint_registry import VoiceprintRegistry


RTC_ENV = {
    "RTC_APP_ID": "rtc-app",
    "RTC_BOT_USER_ID": "assistant",
    "DOUBAO_ASR_APP_ID": "asr-app",
    "DOUBAO_ASR_ACCESS_TOKEN": "asr-token",
    "DOUBAO_SPEECH_RESOURCE_ID": "volc.seedasr.sauc.duration",
    "DOUBAO_TTS_APP_ID": "tts-app",
    "DOUBAO_TTS_ACCESS_TOKEN": "tts-token",
    "DOUBAO_TTS_VOICE_TYPE": "voice",
    "DOUBAO_TTS_RESOURCE_ID": "volc.service_type.10029",
    "ARK_ENDPOINT_ID": "ark-endpoint",
}


class VoiceprintRegistryTests(unittest.TestCase):
    def test_saves_and_resolves_voiceprint(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = VoiceprintRegistry(os.path.join(directory, "voices.json"))
            registry.save("张三", "vp-1")
            registry.save("李四", "vp-2")

            self.assertEqual(registry.name_for("vp-1"), "张三")
            self.assertEqual(registry.voiceprint_ids(), ["vp-1", "vp-2"])


class RTCVoiceChatTests(unittest.TestCase):
    @patch.dict(os.environ, RTC_ENV, clear=False)
    def test_builds_multi_person_voiceprint_config(self):
        request = build_start_request(
            "room", "user", "task", ["vp-1", "vp-2"]
        )

        voiceprint = request["AgentConfig"]["VoicePrint"]
        self.assertEqual(voiceprint["Mode"], 2)
        self.assertEqual(voiceprint["IdList"], ["vp-1", "vp-2"])
        self.assertEqual(request["Config"]["ASRConfig"]["Provider"], "volcano")
        self.assertNotIn("Thinking", request["Config"]["LLMConfig"])
        self.assertTrue(request["AgentConfig"]["EnableConversationStateCallback"])

    def test_generates_bound_rtc_token(self):
        token = create_rtc_token(
            "a" * 24, "secret", "voice-room", "user-1", 3600,
            now=1_700_000_000, nonce=123,
        )
        self.assertTrue(token.startswith("001" + "a" * 24))
        self.assertNotIn("secret", token)

    @patch.dict(os.environ, RTC_ENV, clear=False)
    def test_rejects_more_than_three_voiceprints(self):
        with self.assertRaisesRegex(RuntimeError, "最多支持 3 个"):
            build_start_request(
                "room", "user", "task", ["1", "2", "3", "4"]
            )

    def test_formats_subtitle_with_real_name(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = VoiceprintRegistry(os.path.join(directory, "voices.json"))
            registry.save("张三", "vp-1")
            message = {
                "payload": {
                    "Text": "今天天气好吗？",
                    "VoicePrintId": "vp-1",
                }
            }

            self.assertEqual(
                format_subtitle(message, registry),
                "张三：今天天气好吗？",
            )

    def test_finds_nested_voiceprint_id(self):
        payload = json.loads('{"Result":{"VoicePrintId":"vp-1"}}')
        self.assertEqual(_find_value(payload, "VoicePrintId"), "vp-1")

    def test_formats_camel_case_voiceprint_id(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = VoiceprintRegistry(os.path.join(directory, "voices.json"))
            registry.save("张三", "vp-1")
            message = {
                "data": [{
                    "text": "你好",
                    "voiceprintId": "vp-1",
                }],
            }

            self.assertEqual(
                format_subtitle(message, registry),
                "张三：你好",
            )


if __name__ == "__main__":
    unittest.main()
