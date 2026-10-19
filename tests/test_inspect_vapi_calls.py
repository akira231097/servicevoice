"""Offline privacy, scope and evidence tests. No .env or account requests."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3

import httpx

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inspect_vapi_calls.py"
spec = importlib.util.spec_from_file_location("inspect_vapi_calls", SCRIPT)
inspector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspector)
ASSISTANT = "11111111-1111-4111-8111-111111111111"
CALL = "22222222-2222-4222-8222-222222222222"
SINCE = datetime(2026, 10, 17, tzinfo=timezone.utc)


def sample():
    return {
        "id": CALL, "assistantId": ASSISTANT, "createdAt": "2026-10-17T01:00:00Z",
        "startedAt": "2026-10-17T01:01:00Z", "endedAt": "2026-10-17T01:02:30Z",
        "status": "ended", "type": "webCall", "endedReason": "customer-ended-call", "cost": 0.25,
        "customer": {"name": "PRIVATE-CUSTOMER", "number": "+15555551234"},
        "transcript": "PRIVATE-TRANSCRIPT", "endedMessage": "PRIVATE-ERROR",
        "assistantOverrides": {"variableValues": {"session_token": "PRIVATE-TOKEN"}},
        "transport": {"sdp": "PRIVATE-SDP"}, "recordingUrl": "https://example.com/PRIVATE-AUDIO",
        "artifact": {"performanceMetrics": {"turnLatencyAverage": 1.2, "numUserInterrupted": 0}, "messages": [
            {"role": "tool_calls", "toolCalls": [{"id": "tool-1", "function": {"name": "confirm_action", "arguments": "PRIVATE-ARGS"}}]},
            {"role": "tool_call_result", "toolCallId": "tool-1", "result": json.dumps({"ok": True, "customer": "PRIVATE-RESULT"})},
        ]},
    }


def test_scope_and_privacy(tmp_path):
    raw = sample()
    other = {**sample(), "assistantId": "another-assistant"}
    old = {**sample(), "createdAt": "2026-10-16T01:00:00Z"}
    report = inspector.build_report([raw, other, old], ASSISTANT, SINCE, 20, False, tmp_path / "missing.db")
    encoded = json.dumps(report)
    assert "PRIVATE-" not in encoded and "+15555551234" not in encoded
    assert report["scope"]["reported_count"] == 1
    assert report["scope"]["discarded_out_of_scope_or_invalid_count"] == 2
    observed = report["calls"][0]
    assert observed["provider"]["duration_seconds"] == 90
    assert observed["provider_tools"]["result_counts"]["ok_true"] == 1
    assert observed["local_scheduler"]["available"] is False
    assert observed["transport_evidence"]["audible_audio_verified"] is None


def test_missing_is_unknown_and_empty_batch_produces_no_report(tmp_path):
    assert inspector.build_report([], ASSISTANT, SINCE, 20, False, tmp_path / "missing.db") is None
    raw = sample()
    del raw["artifact"]
    del raw["cost"]
    report = inspector.build_report([raw], ASSISTANT, SINCE, 1, True, tmp_path / "missing.db")
    observed = report["calls"][0]
    assert report["scope"]["possibly_truncated"] is True
    assert observed["provider"]["cost_usd"] is None
    assert observed["performance"]["available"] is False
    assert observed["provider_tools"]["available"] is False


def test_correlated_local_database_only(tmp_path):
    path = tmp_path / "local.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
          CREATE TABLE sessions(id TEXT,provider_call_id TEXT,tenant TEXT);
          CREATE TABLE events(session_id TEXT,name TEXT,status TEXT);
          CREATE TABLE appointments(session_id TEXT,tenant TEXT,status TEXT,customer TEXT);
          CREATE TABLE proposals(session_id TEXT,tenant TEXT,kind TEXT,state TEXT);
        """)
        db.execute("INSERT INTO sessions VALUES (?,?,?)", ("local-session", CALL, "cedar-demo"))
        db.execute("INSERT INTO events VALUES (?,?,?)", ("local-session", "confirm_action", "success"))
        db.execute("INSERT INTO appointments VALUES (?,?,?,?)", ("local-session", "cedar-demo", "confirmed", "PRIVATE-CUSTOMER"))
        db.execute("INSERT INTO proposals VALUES (?,?,?,?)", ("local-session", "cedar-demo", "book", "committed"))
    result = inspector.local_evidence(path, CALL)
    assert result["session_binding"] == "matched_provider_call_id"
    assert result["committed_actions"] == {"book": 1}
    assert result["appointment_count"] == 1
    assert "PRIVATE" not in json.dumps(result) and "local-session" not in json.dumps(result)
    assert inspector.local_evidence(path, "different-call")["session_binding"] == "not_found"


def test_fetch_scopes_request_with_offline_mock():
    def handle(request):
        assert request.method == "GET"
        assert request.url.path == "/call"
        assert request.url.params["assistantId"] == ASSISTANT
        assert request.url.params["createdAtGe"] == SINCE.isoformat()
        assert request.url.params["limit"] == "20"
        return httpx.Response(200, json=[sample()])
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert inspector.fetch_calls(client, ASSISTANT, SINCE, 20)[0]["id"] == CALL


def test_conflicting_result_does_not_count_as_success():
    raw = sample()
    raw["artifact"]["messages"].append({"toolCallId": "tool-1", "result": '{"ok":false}'})
    counts = inspector.provider_tools(raw)["result_counts"]
    assert counts["ok_true"] == 0
    assert counts["conflicting_results"] == 1


def test_fused_service_tool_name_is_preserved():
    raw = sample()
    raw["artifact"]["messages"][0]["toolCalls"][0]["function"]["name"] = "begin_service_request"
    assert inspector.provider_tools(raw)["calls_by_name"] == {"begin_service_request": 1}
