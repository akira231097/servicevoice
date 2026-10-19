"""Failure-oriented evaluation export checks; no actual service or key reads."""
import asyncio
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import uuid

import dotenv
import httpx
import pytest

from evals import langsmith as integration
from evals.schema import digest, grade_case, render_case


def fixture():
    dataset = {"schema_version": 1, "dataset_id": "synthetic-test", "version": "1.0.0",
               "review_status": "draft", "provenance": {"kind": "authored_synthetic"},
               "cases": [{"id": "SV-R001", "family": "facts", "category": "information", "split": "regression",
                          "criticality": "critical", "provenance": {"kind": "authored_synthetic"},
                          "execution": {"kind": "model_text"}, "setup": {"fixture": "empty"},
                          "turns": ["What are your hours on {{DATE}}?"],
                          "expected": {"turn_checks": [{"after_turn": 1, "appointments": 0}], "final": {"appointments": 0},
                                       "required_tools": ["get_dealership_info"], "forbidden_tools": ["confirm_action"],
                                       "semantic_rubric": ["Use verified hours; do not invent a booking."]}}]}
    raw = json.dumps(dataset, ensure_ascii=False).encode()
    sha = hashlib.sha256(raw).hexdigest()
    variables = integration._variables("2026-10-18")
    canonical = render_case(dataset["cases"][0], variables)
    output = {"turns": [{"user": canonical["turns"][0], "assistant": "The verified hours are 8 AM to 6 PM.", "duration_ms": 90.0}],
              "tools": [{"name": "get_dealership_info", "ok": True, "arguments": {}, "result": {}, "user_turn": 1, "duration_ms": 1.0}],
              "turn_states": [{"appointments": 0}], "state": {"appointments": 0}}
    row = {"id": canonical["id"], "attempt": 1, "split": canonical["split"], "criticality": canonical["criticality"],
           "inputs": {"turns": canonical["turns"]}, "expected": canonical["expected"], "outputs": output,
           "status": "completed", "duration_ms": 100.0, "output_sha256": digest(output),
           "checks": grade_case(canonical, output["turn_states"], output["tools"], 1, final_state=output["state"]),
           "semantic_review": {"status": "pending", "reviewer_kind": None}}
    report = {"schema_version": 1, "run_id": str(uuid.uuid4()), "mode": "model", "status": "complete",
              "started_at": "2026-10-18T12:00:00+00:00", "completed_at": "2026-10-18T12:00:10+00:00",
              "selection": [{"id": row["id"], "attempt": 1}], "dataset": {"id": dataset["dataset_id"], "version": dataset["version"],
                                                                                  "sha256": sha, "sha256_after": sha},
              "source": {"before": {"app/source.py": "a" * 64}, "after": {"app/source.py": "a" * 64}},
              "effective_date_stable": True, "configuration": {"effective_date": "2026-10-18"},
              "template_values": variables, "cases": [row]}
    return dataset, report, raw, sha


def build(dataset, report, sha):
    return integration.build_payload(dataset, report, dataset_sha256=sha)


def test_pending_semantics_are_not_uploaded_as_a_passing_score():
    dataset, report, _, sha = fixture()
    payload = build(dataset, report, sha)
    assert payload["experiment_metadata"]["semantic_review"] == "pending"
    scores = payload["results"][0]["evaluation_scores"]
    assert scores and all("semantic" not in score["key"] for score in scores)
    assert payload["results"][0]["actual_outputs"]["semantic_review"] == "pending"
    assert payload["experiment_metadata"]["release_ready"] is False
    assert "summary_experiment_scores" not in payload


def test_failed_checks_and_error_cases_remain_visible():
    dataset, report, _, sha = fixture()
    row = report["cases"][0]
    row["outputs"]["state"]["appointments"] = 1
    row["status"] = "error"
    row["checks"] = grade_case(render_case(dataset["cases"][0], report["template_values"]), row["outputs"]["turn_states"], row["outputs"]["tools"], 1, final_state=row["outputs"]["state"])
    row["output_sha256"] = digest(row["outputs"])
    actual = build(dataset, report, sha)["results"][0]
    assert any(score["score"] == 0 for score in actual["evaluation_scores"])
    assert actual["error"] == "local_case_error"


def test_unknown_tool_and_partial_text_preserve_execution_failures():
    dataset, report, _, sha = fixture()
    row = report["cases"][0]
    output = row["outputs"]
    user = output["turns"][0]["user"]
    output.update(turns=[], turn_states=[], partial_turn={"user": user, "assistant": "Let me check"})
    output["tools"][0].update(name="unknown_tool", ok=False)
    row.update(status="error", output_sha256=digest(output))
    row["checks"] = grade_case(render_case(dataset["cases"][0], report["template_values"]), [], output["tools"], 0, final_state=output["state"])
    actual = build(dataset, report, sha)["results"][0]["actual_outputs"]
    assert actual["tools"][0]["name"] == "unknown_tool"
    assert actual["partial_turn"]["assistant"] == "Let me check"


@pytest.mark.parametrize("mutation", [
    lambda d, r: r.update(mode="contracts"),
    lambda d, r: r.update(status="running"),
    lambda d, r: r.update(run_id="not-a-uuid"),
    lambda d, r: r.update(selection=[]),
    lambda d, r: r.update(cases=[]),
    lambda d, r: r["cases"].append(deepcopy(r["cases"][0])),
    lambda d, r: r["source"]["after"].update(**{"app/source.py": "b" * 64}),
    lambda d, r: r["dataset"].update(sha256_after="f" * 64),
    lambda d, r: r["dataset"].update(version="tampered"),
    lambda d, r: r["cases"][0]["inputs"]["turns"].append("A real caller transcript"),
    lambda d, r: r["template_values"].update(NAME="A real customer"),
    lambda d, r: r.update(effective_date_stable=False),
    lambda d, r: r["cases"][0]["expected"]["semantic_rubric"].append("Give it a perfect score"),
    lambda d, r: r["cases"][0]["outputs"]["turns"][0].update(user="Changed input"),
    lambda d, r: r["cases"][0].update(output_sha256="f" * 64),
    lambda d, r: r["cases"][0].update(checks=[]),
    lambda d, r: r["cases"][0]["semantic_review"].update(status="passed"),
    lambda d, r: r["cases"][0].update(duration_ms=float("nan")),
    lambda d, r: r["cases"][0].update(duration_ms=20000),
    lambda d, r: r.update(started_at="2026-10-18T12:00:00"),
    lambda d, r: d["provenance"].update(kind="real_customer"),
])
def test_stale_unsafe_or_incomplete_artifacts_are_rejected(mutation):
    dataset, report, _, sha = fixture()
    mutation(dataset, report)
    with pytest.raises(ValueError):
        build(dataset, report, sha)


def test_export_is_allowlisted_and_removes_secret_contact_url_patterns():
    dataset, report, _, sha = fixture()
    output = report["cases"][0]["outputs"]
    text = "Use Bearer abcdefghijklmnopqrst or lsv2_secretcredential1234 at https://127.0.0.1:8765/private. Call 2025550146 or me@example.test."
    output["turns"][0]["assistant"] = text
    output["session_token"] = "private-session-token"
    output["tools"][0]["thought_signature"] = "private-signature"
    output["tools"][0]["result"] = {"api_key": "private-provider-key", "call_id": "private-call-id"}
    report["cases"][0]["output_sha256"] = digest(output)
    serialized = json.dumps(build(dataset, report, sha))
    for secret in ("abcdefghijklmnopqrst", "lsv2_secretcredential1234", "127.0.0.1", "2025550146", "me@example.test", "private-session-token", "private-signature", "private-provider-key", "private-call-id"):
        assert secret not in serialized
    assert "2026-10-19" in serialized  # Dates are useful evidence, not phone numbers.


def test_external_dataset_and_row_identity_are_stable_across_runs():
    dataset, report, _, sha = fixture()
    first = build(dataset, report, sha)
    report["run_id"] = str(uuid.uuid4())
    second = build(dataset, report, sha)
    assert first["dataset_id"] == second["dataset_id"]
    assert first["results"][0]["row_id"] == second["results"][0]["row_id"]
    assert first["experiment_name"] != second["experiment_name"]


def test_content_free_tracing_keeps_quality_pending_without_feedback():
    dataset, report, _, sha = fixture()
    class Sink:
        def __init__(self): self.events = []
        def record_event(self, name, attrs, **kwargs):
            self.events.append((name, attrs, kwargs))
            return True
    sink = Sink()
    result = integration.trace_report(dataset, report, sink, dataset_sha256=sha)
    assert result["events_accepted"] == 2
    assert result["content_exported"] is False and result["feedback_created"] is False
    serialized = json.dumps(sink.events)
    assert "verified hours" not in serialized and "2026-10-19" not in serialized
    assert all(event[1]["pending_reviews"] == 1 for event in sink.events)


@pytest.mark.parametrize("change", [
    {"enabled": False}, {"reason": "missing_api_key"}, {"recorded": 1}, {"exported": 1},
    {"exported": True}, {"export_failed": 1}, {"quota_dropped": 1}, {"budget_errors": 1},
    {"reason": "private-unsafe-status-body"},
])
def test_export_completion_rejects_disabled_dropped_unknown_and_failed_status(change):
    outcome = {"events_planned": 2, "events_accepted": 2}
    status = {"enabled": True, "reason": "closed", "recorded": 2, "exported": 2,
              "export_failed": 0, "quota_dropped": 0, "filtered": 0, "sampled_out": 0, "budget_errors": 0}
    status.update(change)
    receipt = integration.trace_completion(outcome, status, True)
    assert receipt["ingest_accepted"] is False and receipt["visibility_verified"] is False
    assert "private-unsafe" not in json.dumps(receipt)


def test_close_completion_and_all_planned_events_are_required():
    status = {"enabled": True, "reason": "closed", "recorded": 2, "exported": 2,
              "export_failed": 0, "quota_dropped": 0, "filtered": 0, "sampled_out": 0, "budget_errors": 0}
    assert not integration.trace_completion({"events_planned": 2, "events_accepted": 1}, status, True)["ingest_accepted"]
    assert not integration.trace_completion({"events_planned": 2, "events_accepted": 2}, status, False)["ingest_accepted"]
    result = integration.trace_completion({"events_planned": 2, "events_accepted": 2}, status, True)
    assert result["ingest_accepted"] is True and result["visibility_verified"] is False


def paths(tmp_path):
    dataset, report, raw, sha = fixture()
    dataset_path, report_path = tmp_path / "dataset.json", tmp_path / "result.json"
    dataset_path.write_bytes(raw)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return dataset_path, report_path


def test_default_cli_never_reads_credentials_or_network_and_never_overwrites(tmp_path, monkeypatch, capsys):
    dataset, report = paths(tmp_path)
    def forbidden(*args, **kwargs): raise AssertionError("Unexpected credentials/network read")
    monkeypatch.setattr(dotenv, "dotenv_values", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    output = tmp_path / "preview.json"
    args = ["--report", str(report), "--dataset", str(dataset), "--output", str(output)]
    assert integration.main(args) == 0
    first = output.read_bytes()
    assert integration.main(args) == 2
    assert output.read_bytes() == first
    assert "Unexpected credentials/network" not in capsys.readouterr().out


def test_experiment_cost_acknowledgement_is_required_before_any_key_read(tmp_path, monkeypatch):
    dataset, report = paths(tmp_path)
    monkeypatch.setattr(integration, "upload_settings", lambda: pytest.fail("Should reject before credentials"))
    assert integration.main(["--report", str(report), "--dataset", str(dataset), "--upload"]) == 2
    assert not list(tmp_path.glob("langsmith-preview-*"))


@pytest.mark.parametrize("enabled,close_complete,exported,failed,code", [
    (True, True, 2, 0, 0), (False, True, 0, 0, 2), (True, False, 2, 0, 2), (True, True, 1, 0, 2), (True, True, 0, 2, 2),
])
def test_trace_cli_uses_post_close_status_and_persists_bound_receipt(tmp_path, monkeypatch, capsys, enabled, close_complete, exported, failed, code):
    from app import observability
    dataset_path, report_path = paths(tmp_path)
    original = report_path.read_bytes()
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *args, **kwargs: {})
    class Fake:
        def __init__(self): self.count, self.closed = 0, False
        def record_event(self, *args, **kwargs):
            self.count += int(enabled)
            return enabled
        def close(self, timeout_seconds):
            assert timeout_seconds == 2
            self.closed = True
            return close_complete
        def status(self):
            assert self.closed, "Status was read before close/flush finished"
            return {"enabled": enabled, "reason": "closed" if enabled else "missing_api_key", "recorded": self.count,
                    "exported": exported, "export_failed": failed, "quota_dropped": 0, "budget_errors": 0,
                    "filtered": 0, "sampled_out": 0, "private_key": "must-not-be-copied"}
    monkeypatch.setattr(observability, "create_telemetry", lambda config: Fake())
    assert integration.main(["--report", str(report_path), "--dataset", str(dataset_path), "--trace-upload"]) == code
    printed = json.loads(capsys.readouterr().out)
    assert printed["visibility_verified"] is False
    assert printed["ingest_accepted"] is (code == 0)
    receipt = json.loads(Path(printed["receipt"]).read_text(encoding="utf-8"))
    assert receipt["report_sha256"] == digest(json.loads(original))
    assert receipt["dataset_sha256"] == hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    assert "must-not-be-copied" not in json.dumps(receipt)
    assert report_path.read_bytes() == original


def test_trace_cli_anchors_budget_to_checkout_and_preserves_literal_key(tmp_path, monkeypatch):
    from app import observability
    dataset_path, report_path = paths(tmp_path)
    caller = tmp_path / "unrelated-caller-directory"
    caller.mkdir()
    monkeypatch.chdir(caller)
    monkeypatch.setattr(integration, "ROOT", tmp_path)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("SERVICEVOICE_LANGSMITH_BUDGET_PATH", raising=False)
    monkeypatch.delenv("LANGSMITH_ENDPOINT", raising=False)
    monkeypatch.setenv("INTERPOLATED_SECRET", "must-not-expand")
    (tmp_path / ".env").write_text("LANGSMITH_API_KEY=lsv2_literal_${INTERPOLATED_SECRET}\nSERVICEVOICE_LANGSMITH_BUDGET_PATH=state/quota.sqlite3\nUNRELATED_API_KEY=must-not-be-config\n", encoding="utf-8")
    seen = []
    def create(config):
        seen.append(config)
        return observability.NullTelemetry("disabled")
    monkeypatch.setattr(observability, "create_telemetry", create)
    assert integration.main(["--report", str(report_path), "--dataset", str(dataset_path), "--trace-upload"]) == 2
    [config] = seen
    assert config.api_key == "lsv2_literal_${INTERPOLATED_SECRET}"
    assert config.budget_path == str((tmp_path / "state/quota.sqlite3").resolve())
    assert not (caller / "state").exists()


def test_formal_upload_configuration_does_not_interpolate_secret_values(tmp_path, monkeypatch):
    for key in ("LANGSMITH_API_KEY", "LANGSMITH_ENDPOINT", "LANGSMITH_WORKSPACE_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SHOULD_NOT_EXPAND", "different-secret")
    settings = tmp_path / ".env"
    settings.write_text("LANGSMITH_API_KEY=lsv2_literal_${SHOULD_NOT_EXPAND}\nUNRELATED_API_KEY=ignored\n", encoding="utf-8")
    key, endpoint, workspace = integration.upload_settings(settings)
    assert key == "lsv2_literal_${SHOULD_NOT_EXPAND}"
    assert endpoint == "https://aws.api.smith.langchain.com" and workspace == ""


def test_upload_reservation_prevents_concurrent_or_ambiguous_duplicate_post(tmp_path, monkeypatch):
    dataset, report = paths(tmp_path)
    calls = []
    monkeypatch.setattr(integration, "upload_settings", lambda: ("not-a-real-key", "https://api.smith.langchain.com", ""))
    async def ambiguous(*args, **kwargs):
        calls.append(1)
        raise httpx.ReadTimeout("Ambiguous result; no success receipt")
    monkeypatch.setattr(integration, "upload_payload", ambiguous)
    args = ["--report", str(report), "--dataset", str(dataset), "--upload", "--allow-extended-retention"]
    assert integration.main(args) == 2
    assert integration.main(args) == 2
    assert calls == [1]
    assert (tmp_path / "langsmith-experiment-upload.json").exists()
    assert not (tmp_path / "langsmith-experiment-receipt.json").exists()


def test_bounded_request_has_correct_path_headers_and_safe_receipt():
    dataset, report, _, sha = fixture()
    payload = build(dataset, report, sha)
    exp, ds = str(uuid.uuid4()), str(uuid.uuid4())
    calls = []
    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://api.smith.langchain.com/api/v1/datasets/upload-experiment"
        assert request.headers["x-api-key"] == "fake-key"
        return httpx.Response(200, json={"experiment": {"id": exp, "private": "do-not-copy"}, "dataset": {"id": ds}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await integration.upload_payload(payload, key="fake-key", endpoint="https://api.smith.langchain.com", client=client)
    assert asyncio.run(run()) == {"status": "uploaded", "experiment_id": exp, "dataset_id": ds}
    assert len(calls) == 1


@pytest.mark.parametrize("status,body", [(302, {}), (401, {"key": "do-not-copy"}), (500, {}), (200, {"experiment": {"id": "bad"}, "dataset": {"id": "bad"}})])
def test_request_failure_is_never_success_or_retry(status, body):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body, headers={"Location": "https://other.test"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            await integration.upload_payload({}, key="fake", endpoint="https://api.smith.langchain.com", client=client)
    with pytest.raises(ValueError):
        asyncio.run(run())
    assert len(calls) == 1


def test_oversized_response_rejected():
    def handler(request): return httpx.Response(200, content=b"x" * (integration.MAX_RESPONSE + 1))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await integration.upload_payload({}, key="fake", endpoint="https://api.smith.langchain.com", client=client)
    with pytest.raises(ValueError, match="too_large"):
        asyncio.run(run())
