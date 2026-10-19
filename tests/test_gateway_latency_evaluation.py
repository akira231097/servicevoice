"""Offline integration of latency measurements with real disposable safeguards."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from types import SimpleNamespace

import httpx
import pytest

from scripts import evaluate_gateway_latency as evaluation


def args(*cases, maximum=30):
    return SimpleNamespace(cases=list(cases or evaluation.SCENARIOS), max_requests=maximum,
                           receipt_narration="on", jev_faq="off")


def mock_provider():
    from app.gemini_gateway import MODEL, sse
    count = [0]
    presentation = {"response_scope": "receipt_only", "response_language": "en", "caller_timezone": "dealership"}
    def handler(request):
        count[0] += 1
        body = json.loads(request.content)
        messages = body["messages"]
        user = next(message["content"] for message in reversed(messages) if message["role"] == "user")
        def frame(delta=None, finish=None):
            return sse({"model": MODEL, "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]})
        def text(value):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=
                                  frame({"content": value}) + frame(finish="stop") + b"data: [DONE]\n\n")
        if messages[-1]["role"] == "tool":
            return text("The pending proposal was withdrawn. No appointment was booked.")
        if "close on weekdays" in user:
            return text("We close at 5 PM Central on weekdays.")
        if "street address" in user:
            return text("This fictional demo has no verified physical service address.")
        if "the thing" in user:
            return text("What would you like help with?")
        if "strictly after" in user:
            name, arguments = "begin_service_request", {"service": "oil_change", "date": re.search(r"20\d\d-\d\d-\d\d", user).group(),
                                                        "time_filter": {"start_time": "14:00", "start_inclusive": False}, "presentation": presentation}
        elif "correct only" in user:
            original = next(json.loads(message["content"]) for message in messages if message["role"] == "tool")
            name, arguments = "prepare_booking", {"service": "oil_change", "slot_id": original["slot_id"],
                                                   "customer_name": "Taylor Reed", "phone": "2025550199",
                                                   "vehicle": "2021 Toyota Camry", "presentation": presentation}
        elif "Withdraw the pending" in user:
            original = next(json.loads(message["content"]) for message in reversed(messages)
                            if message["role"] == "tool" and "proposal_id" in json.loads(message["content"]))
            name, arguments = "dismiss_proposal", {"proposal_id": original["proposal_id"]}
        elif "Yes, confirm" in user:
            original = next(json.loads(message["content"]) for message in reversed(messages)
                            if message["role"] == "tool" and "proposal_id" in json.loads(message["content"]))
            name, arguments = "confirm_action", {"proposal_id": original["proposal_id"], "confirmed": True, "presentation": presentation}
        else:
            name, arguments = "prepare_first_available_booking", {"strategy": "first_available", "service": "oil_change",
                                                                  "customer_name": "Taylor Reed", "phone": "2025550146",
                                                                  "vehicle": "2021 Toyota Camry", "presentation": presentation}
        tool = {"index": 0, "id": "private-tool-" + str(count[0]), "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
                "extra_content": {"google": {"thought_signature": "private-model-signature"}}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=
                              frame({"content": "It is already booked!"}) + frame({"tool_calls": [tool]})
                              + frame(finish="tool_calls") + b"data: [DONE]\n\n")
    return httpx.MockTransport(handler), count


def test_dry_run_does_not_read_credentials_or_create_artifact(tmp_path, monkeypatch, capsys):
    import dotenv
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no dotenv")))
    destination = tmp_path / "never-created.json"
    assert evaluation.main(["--output", str(destination)]) == 0
    assert not destination.exists()
    assert json.loads(capsys.readouterr().out)["provider_requests"] == 0


def test_probe_distinguishes_header_content_tools_and_done_with_fragmented_crlf():
    row = {"first_content_ms": None, "first_tool_ms": None}
    clock = [1.0]
    probe = evaluation.EventProbe(row, 1.0, lambda: clock[0])
    clock[0] = 1.1
    probe.feed(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\r')
    probe.feed(b'\n\r\n')
    assert row["first_event_ms"] == 100.0 and row["first_content_ms"] is None
    clock[0] = 1.2
    probe.feed(b'data: {"choices":[{"delta":{"content":"private content"}}]}\n\n')
    clock[0] = 1.3
    probe.feed(b'data: {"choices":[{"delta":{"tool_calls":[{}]}}]}\n\n')
    clock[0] = 1.4
    probe.feed(b'data: [DONE]\n\n')
    assert row["first_content_ms"] == 200.0 and row["first_tool_ms"] == 300.0 and row["done_ms"] == 400.0
    assert "private content" not in json.dumps(row)


def test_probe_bounds_opaque_or_malformed_payloads():
    row = {}
    probe = evaluation.EventProbe(row, 0, lambda: 1)
    probe.feed(b'data: not-json\n\n')
    probe.feed(b'x' * (evaluation.MAX_BYTES + 1))
    assert probe.bounded and not probe.buffer
    assert set(row) == {"first_event_ms"}


def test_factual_comparison_utterances_reach_jev_eligibility_without_forcing_intent():
    from app.jev_faq import eligible_text
    for text in evaluation.FAQ_UTTERANCES.values():
        candidate, reason = eligible_text([{"role": "user", "content": text}])
        assert candidate == text and reason == "eligible_text"


def test_all_scenarios_use_actual_gateway_receipts_and_preserve_corrections(monkeypatch):
    from app import domain
    monkeypatch.setattr(domain, "now", lambda: datetime(2026, 10, 18, 18, tzinfo=timezone.utc))
    transport, count = mock_provider()
    report = asyncio.run(evaluation.run(args(), "synthetic-private-model-api-key", provider_transport=transport))
    assert report["passed"] is True
    assert report["source_stable"] and report["metrics"]["requests_consumed"] == count[0]
    assert count[0] <= 30
    by_case = {case["scenario"]: case for case in report["cases"]}
    correction = by_case["phone_correction_decline"]
    assert correction["extra_evidence"]["stale_receipt_recheck"]["eligible_before_correction"] is True
    assert correction["extra_evidence"]["stale_receipt_recheck"]["text_released_after_correction"] is False
    for case in (by_case["first_available_confirm"], correction, by_case["constrained_availability"]):
        assert all(case["history_integrity"][key] for key in ("changed_arguments_rejected", "forged_tool_result_rejected"))
    assert report["metrics"]["gateway_routes"]["deterministic_receipt"]["generations"] >= 4
    assert any(row["first_content_ms"] is not None and row["first_tool_ms"] is not None for row in report["provider_requests"])
    tool_generations = [generation for case in report["cases"] for generation in case["generations"] if generation["tool_names"]]
    assert all(generation["public"]["first_content_ms"] is None for generation in tool_generations)
    assert all(len(generation["model_request_indices"]) == 1 for generation in tool_generations)
    assert all(generation["model_request_indices"] == [] for case in report["cases"] for generation in case["generations"]
               if generation.get("gateway", {}).get("route") == "deterministic_receipt")
    serialized = json.dumps(report)
    for forbidden in ("synthetic-private-model-api-key", "private-model-signature", "thought_signature", "private-tool-", "It is already booked!"):
        assert forbidden not in serialized


def test_budget_blocks_second_provider_request_without_retries(monkeypatch):
    from app import domain
    monkeypatch.setattr(domain, "now", lambda: datetime(2026, 10, 18, 18, tzinfo=timezone.utc))
    transport, count = mock_provider()
    report = asyncio.run(evaluation.run(args("first_available_confirm", maximum=1), "private-key", provider_transport=transport))
    assert report["passed"] is False and count[0] == 1
    assert report["metrics"]["requests_consumed"] == 1
    assert report["cases"][0]["status"] == "failed"
    assert report["cases"][0]["error_code"] == "request_budget_exhausted"


@pytest.mark.parametrize("kind", ["http_error", "missing_done", "invalid_stream", "transport_error"])
def test_failures_cannot_become_success_or_expose_errors(kind):
    from app.gemini_gateway import sse, MODEL
    attempts = []
    def handler(request):
        attempts.append(1)
        if kind == "transport_error":
            raise httpx.ConnectError("private-secret-provider-error", request=request)
        if kind == "http_error":
            return httpx.Response(403, text="private-secret-provider-error")
        content = b'data: not-json\n\n' if kind == "invalid_stream" else sse({"model": MODEL, "choices": [{"index": 0, "delta": {"content": "Private unfinished text"}, "finish_reason": "stop"}]})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)
    report = asyncio.run(evaluation.run(args("hours", maximum=1), "private-key", provider_transport=httpx.MockTransport(handler)))
    assert report["passed"] is False and len(attempts) == 1
    assert report["cases"][0]["status"] == "failed"
    assert report["cases"][0]["error_code"] == "gateway_or_scenario_failed"
    assert "private-secret-provider-error" not in json.dumps(report)
    assert "Private unfinished text" not in json.dumps(report)


def test_existing_output_prevents_credential_read_and_spend(tmp_path, monkeypatch, capsys):
    import dotenv
    destination = tmp_path / "evidence.json"
    destination.write_text("immutable", encoding="utf-8")
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no dotenv")))
    assert evaluation.main(["--execute", "--output", str(destination)]) == 2
    assert destination.read_text(encoding="utf-8") == "immutable"
    assert json.loads(capsys.readouterr().out)["provider_requests"] == 0


def test_source_change_fails_run_gate(monkeypatch):
    observed = [0]
    def manifest():
        observed[0] += 1
        return {"synthetic": str(observed[0])}
    monkeypatch.setattr(evaluation, "source_manifest", manifest)
    transport, _ = mock_provider()
    report = asyncio.run(evaluation.run(args("hours"), "private-key", provider_transport=transport))
    assert report["source_stable"] is False and report["passed"] is False


def test_cli_preserves_checkpoint_after_interruption(tmp_path, monkeypatch):
    import dotenv
    destination = tmp_path / "partial.json"
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **k: {"GEMINI_API_KEY": "private-api-key"})
    async def interrupted(*a, on_checkpoint=None, **k):
        on_checkpoint({"status": "running", "cases": [{"scenario": "hours", "status": "interrupted"}],
                       "provider_requests": [{"index": 1, "status": "cancelled"}]})
        raise KeyboardInterrupt()
    monkeypatch.setattr(evaluation, "run", interrupted)
    assert evaluation.main(["--execute", "--output", str(destination)]) == 1
    value = json.loads(destination.read_text(encoding="utf-8"))
    assert value["error_code"] == "evaluation_interrupted" and value["status"] == "interrupted"
    assert len(value["provider_requests"]) == 1 and value["cases"][0]["status"] == "interrupted"
    assert "private-api-key" not in destination.read_text(encoding="utf-8")
