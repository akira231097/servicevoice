"""Candidate review deployment contract; no Docker, keys or provider calls."""
import json
from pathlib import Path

from app.live_evaluations import Config


DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "lightsail"
ASSISTANT = "1a2caeaa-21c5-4a3c-ada1-a4b2f5fa8ff7"


def package():
    return json.loads((DEPLOY / "compose.speech-evaluations.json").read_text())


def test_candidate_review_has_only_readonly_candidate_evidence_and_own_outbox():
    document = package()
    assert document["name"] == "servicevoice-speech-evaluations"
    assert set(document["services"]) == {"speech-call-evaluator"}
    service = document["services"]["speech-call-evaluator"]
    assert service["volumes"] == [
        "candidate-evaluation-evidence:/evidence:ro",
        "candidate-speech-evidence:/speech-evidence:ro",
        "candidate-evaluation-data:/evaluations",
    ]
    assert document["volumes"] == {
        "candidate-evaluation-evidence": {"external": True, "name": "servicevoice-speech-candidate_candidate-application-data"},
        "candidate-speech-evidence": {"external": True, "name": "servicevoice-speech-candidate_candidate-speech-data"},
        "candidate-evaluation-data": {},
    }
    assert "ports" not in service and "build" not in service
    assert service["pull_policy"] == "never"
    assert service["read_only"] is True and service["user"] == "10001:10001"
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["env_file"][0]["format"] == "raw"
    assert "SERVICEVOICE_SPEECH_EVAL_ENV_FILE:?" in service["env_file"][0]["path"]
    assert "SERVICEVOICE_SPEECH_EVAL_IMAGE:?" in service["image"]


def test_candidate_compose_environment_is_accepted_by_actual_worker_config():
    environment = package()["services"]["speech-call-evaluator"]["environment"]
    # Synthetic values never leave this process. No source/outbox is opened.
    configuration = Config.from_env({
        **environment,
        "VAPI_EVALUATION_API_KEY": "v" * 32,
        "GEMINI_API_KEY": "g" * 32,
        "LANGSMITH_API_KEY": "l" * 32,
        "LANGSMITH_WORKSPACE_ID": "00000000-0000-4000-8000-000000000001",
        "VAPI_PHONE_ORG_ID": "00000000-0000-4000-8000-000000000002",
    })
    assert configuration.assistant_id == ASSISTANT
    assert configuration.project == "servicevoice-speech-candidate"
    assert configuration.environment == "staging"
    assert configuration.monthly_limit == 200 and configuration.max_failures == 8
    assert configuration.poll_seconds == 30 and configuration.lookback_hours == 24
    assert str(configuration.source).replace("\\", "/") == "/evidence/servicevoice.sqlite3"
    assert str(configuration.diagnostics_path).replace("\\", "/") == "/speech-evidence/tts-diagnostics.sqlite3"
    assert str(configuration.outbox).replace("\\", "/") == "/evaluations/outbox.sqlite3"


def test_separate_review_image_includes_combined_scope_import_dependencies():
    dockerfile = (DEPLOY / "Dockerfile.speech-evaluator").read_text()
    assert "ARG SERVICEVOICE_BASE_IMAGE\n" in dockerfile
    assert "ARG SERVICEVOICE_BASE_IMAGE=" not in dockerfile
    assert "FROM ${SERVICEVOICE_BASE_IMAGE}" in dockerfile
    instructions = (DEPLOY / "speech-evaluations.md").read_text()
    assert "docker image inspect --format '{{.Id}}'" in instructions
    assert "sha256:e5c6f12d0f1fa2f2be3bb146337c50c6b433c4ddade9816d7484a00817556b3d" in instructions
    assert "docker build --pull=false --build-arg SERVICEVOICE_BASE_IMAGE=" in instructions
    copies = [line for line in dockerfile.splitlines() if line.startswith("COPY ")]
    assert len(copies) == 1
    assert copies[0].split()[1:-1] == [
        "app/domain.py", "app/live_eval_evidence.py", "app/live_evaluations.py",
        "app/diagnostics.py", "app/evaluation_spans.py",
        "app/generation_observability.py", "app/observability.py", "app/provider_logs.py",
    ]
    assert 'ENTRYPOINT ["python", "-m", "app.live_evaluations"]' in dockerfile
    assert '"-c"' in dockerfile and "health.json" in dockerfile
    assert "app/main.py" not in dockerfile and "deploy/tts_server.py" not in dockerfile
