"""Offline isolation/routing contract for the separate speech candidate."""
from pathlib import Path
import re
import unittest

HERE = Path(__file__).resolve().parent


def active(path):
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))


class SpeechCandidateTests(unittest.TestCase):
    def test_business_image_is_immutable_and_cannot_build_pull_or_publish(self):
        compose = active(HERE / "compose.speech-candidate.yaml")
        business = compose.split("  servicevoice-candidate:\n", 1)[1].split("  servicevoice-speech-candidate:\n", 1)[0]
        self.assertIn("image: sha256:e5c6f12d0f1fa2f2be3bb146337c50c6b433c4ddade9816d7484a00817556b3d", business)
        self.assertIn("pull_policy: never", business)
        self.assertNotRegex(compose, r"(?m)^\s*(?:ports|build|network_mode|container_name):")
        self.assertNotIn("--workers", compose)
        self.assertIn('command: ["--mode", "provider"]', business)
        for prohibited in ("SERVICEVOICE_RECEIPT_NARRATION:", "SERVICEVOICE_JEV_FAQ:", "VAPI_MODEL_PROFILE:",
                           "VAPI_TRANSCRIBER_PROFILE:", "VAPI_VOICE_PROFILE:"):
            self.assertNotIn(prohibited, business)

    def test_data_and_secret_files_are_separate_from_main_and_each_other(self):
        compose = active(HERE / "compose.speech-candidate.yaml")
        self.assertIn("name: servicevoice-speech-candidate", compose)
        self.assertIn("candidate-application-data:/data", compose)
        self.assertIn("candidate-speech-data:/data", compose)
        self.assertIn("SERVICEVOICE_CANDIDATE_PRIVATE_ENV_FILE:?", compose)
        self.assertIn("SERVICEVOICE_SPEECH_PRIVATE_ENV_FILE:?", compose)
        self.assertNotIn("servicevoice-lightsail_application-data", compose)
        speech = compose.split("  servicevoice-speech-candidate:\n", 1)[1]
        self.assertIn('entrypoint: ["python", "deploy/tts_server.py"]', speech)
        self.assertIn("SERVICEVOICE_TTS_DIAGNOSTICS_PATH: /data/tts-diagnostics.sqlite3", speech)
        self.assertIn("name: servicevoice-lightsail_application", speech)
        template = active(HERE / "speech-candidate-tts.env.example")
        self.assertEqual({line.split("=", 1)[0] for line in template.splitlines() if line},
                         {"GEMINI_API_KEY", "GEMINI_TTS_BEARER_SECRET"})

    def test_edge_only_forwards_three_exact_candidate_post_routes(self):
        snippet = active(HERE / "speech-candidate.caddy")
        paths = {path for line in snippet.splitlines() if line.strip().startswith("path ")
                 for path in line.strip().split()[1:]}
        self.assertEqual(paths, {"/speech-candidate/webhooks/vapi/inbound",
                                "/speech-candidate/llm/chat/completions", "/speech-candidate/tts/vapi"})
        self.assertEqual(re.findall(r"(?m)^\s*method\s+(\w+)\s*$", snippet), ["POST", "POST"])
        self.assertEqual(snippet.count("uri strip_prefix /speech-candidate"), 2)
        self.assertIn("reverse_proxy servicevoice-candidate:8000", snippet)
        self.assertIn("reverse_proxy servicevoice-speech-candidate:8000", snippet)
        self.assertNotIn("flush_interval", snippet)
        self.assertNotIn("lb_retries", snippet)
        self.assertNotIn("*", snippet)

    def test_main_manifest_and_candidate_have_distinct_project_and_state(self):
        candidate = active(HERE / "compose.speech-candidate.yaml")
        main = active(HERE / "compose.yaml")
        self.assertIn("name: servicevoice-lightsail", main)
        self.assertNotIn("name: servicevoice-lightsail\n", candidate)
        self.assertNotIn("application-data:/data", candidate.replace("candidate-application-data:/data", ""))
        self.assertIn("external: true", candidate)


if __name__ == "__main__":
    unittest.main(verbosity=2)
