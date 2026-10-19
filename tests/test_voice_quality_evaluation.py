"""Incident evaluator integrity; MockTransport only, no provider traffic."""
import asyncio
import json

import httpx
import pytest

from app.gemini_gateway import render_system_prompt
from scripts import evaluate_voice_quality as evaluator


def reply(text="Synthetic answer.", *, tool=None, arguments=None, model=evaluator.MODEL):
    message = {"role": "assistant", "content": text,
               "reasoning_content": "PRIVATE-REASONING-DO-NOT-SAVE"}
    if tool:
        message["tool_calls"] = [{"id": "call-" + tool, "type": "function",
                                  "function": {"name": tool, "arguments": json.dumps(arguments or {})},
                                  "extra_content": {"google": {"thought_signature": "PRIVATE-SIGNATURE-DO-NOT-SAVE"}}}]
    return httpx.Response(200, json={"model": model, "choices": [{"message": message,
                                "finish_reason": "tool_calls" if tool else "stop"}],
                                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})


def invoke(tmp_path, handler, *, cap=40, name="report.json", client_class=httpx.AsyncClient):
    output = tmp_path / name
    async def go():
        client = client_class(transport=httpx.MockTransport(handler))
        return await evaluator.run("PRIVATE-KEY-DO-NOT-SAVE", output, cap, provider_client=client)
    code = asyncio.run(go())
    return code, output, json.loads(output.read_text(encoding="utf-8"))


def one_case(monkeypatch, *, two_turns=False):
    monkeypatch.setattr(evaluator, "SCENARIOS", ({"name": "fixture", "language_review": "manual",
                           "turns": ["Information only.", "Still no action."] if two_turns else ["Information only."],
                           "review": "Review text separately."},))


def deny(*args, **kwargs):
    raise AssertionError("Credential/provider access must not happen")


def test_default_dry_run_does_not_read_env_create_client_or_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(evaluator, "dotenv_values", deny)
    monkeypatch.setattr(evaluator.httpx, "AsyncClient", deny)
    output = tmp_path / "unused.json"
    assert evaluator.main(["--output", str(output)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "dry_run" and data["provider_requests"] == 0
    assert data["scenario_count"] == 8 and data["caller_turn_count"] == 12
    assert not output.exists()


@pytest.mark.parametrize("arguments", [["--max-requests", "0"], ["--max-requests", "41"], ["--evaluate"]])
def test_invalid_cli_is_rejected_before_credentials(monkeypatch, arguments):
    monkeypatch.setattr(evaluator, "dotenv_values", deny)
    with pytest.raises(SystemExit) as error:
        evaluator.main(arguments)
    assert error.value.code == 2


def test_existing_output_cli_and_run_refuse_overwrite_before_provider(monkeypatch, tmp_path):
    output = tmp_path / "existing.json"
    output.write_text("immutable first failure", encoding="utf-8")
    monkeypatch.setattr(evaluator, "dotenv_values", deny)
    monkeypatch.setattr(evaluator.httpx, "AsyncClient", deny)
    with pytest.raises(SystemExit):
        evaluator.main(["--evaluate", "--output", str(output)])
    with pytest.raises(FileExistsError):
        asyncio.run(evaluator.run("private", output))
    assert output.read_text(encoding="utf-8") == "immutable first failure"


def test_full_mock_run_uses_shared_prompt_preserves_signature_history_not_report(tmp_path):
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["messages"][0] == {"role": "system", "content": render_system_prompt()}
        assert body["model"] == evaluator.MODEL and body["reasoning_effort"] == "low"
        assert all(field not in body for field in ("temperature", "top_p", "top_k"))
        tools = [message for message in body["messages"] if message.get("tool_calls")]
        if not tools:
            return reply(None, tool="get_dealership_info")
        assert tools[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "PRIVATE-SIGNATURE-DO-NOT-SAVE"
        return reply("Synthetic service answer; linguistic correctness is not asserted by this fixture.")
    code, output, report = invoke(tmp_path, handler)
    assert code == 0 and report["mechanical_passed"]
    assert report["model_request_count"] == len(requests) == 20
    assert len(report["cases"]) == 8
    for case in report["cases"]:
        assert case["prompt_context"]["trusted_facts_supplied"]
        assert case["final_business_counts"] == {"appointments": 0, "proposals": 0, "handoffs": 0}
        assert case["tool_counts"] == {"get_dealership_info": 1}
        assert all(row["status"] == "response_received" and row["model_returned"] == evaluator.MODEL for row in case["model_requests"])
        assert case["semantic_review"] == "pending"
    assert report["overall_passed"] is None and report["semantic_review"] == "pending_separate_review"
    text = output.read_text(encoding="utf-8")
    assert "PRIVATE-" not in text and "thought_signature" not in text


@pytest.mark.parametrize("tool,arguments,expected_handoffs", [
    ("request_handoff", {"reason": "Unrequested synthetic handoff"}, 1),
    ("confirm_action", {"proposal_id": "does-not-exist", "confirmed": True}, 0),
    ("unknown_untrusted_tool", {"untrusted": "PRIVATE-ARGUMENT"}, 0),
])
def test_unrequested_effect_or_rejected_attempt_cannot_pass(monkeypatch, tmp_path, tool, arguments, expected_handoffs):
    one_case(monkeypatch)
    requests = []
    def handler(request):
        requests.append(request)
        return reply(None, tool=tool, arguments=arguments) if len(requests) == 1 else reply()
    code, output, report = invoke(tmp_path, handler)
    case = report["cases"][0]
    assert code == 1 and not case["mechanical_passed"]
    assert case["final_business_counts"]["handoffs"] == expected_handoffs
    assert next(item for item in case["mechanical_checks"] if item["name"] == "no_prohibited_tool_attempt")["passed"] is False
    assert "PRIVATE-ARGUMENT" not in output.read_text(encoding="utf-8")


def test_created_then_dismissed_proposal_is_detected_despite_no_pending_proposal(monkeypatch, tmp_path):
    one_case(monkeypatch)
    def handler(request):
        body = json.loads(request.content)
        results = [json.loads(message["content"]) for message in body["messages"] if message["role"] == "tool"]
        if not results:
            return reply(None, tool="prepare_first_available_booking", arguments={"strategy": "first_available", "service": "oil_change",
                         "customer_name": "Synthetic Caller", "phone": "5125550142", "vehicle": "2021 Toyota Camry"})
        if len(results) == 1:
            assert results[0]["ok"] is True
            return reply(None, tool="dismiss_proposal", arguments={"proposal_id": results[0]["proposal_id"]})
        return reply("No booking was made.")
    code, _, report = invoke(tmp_path, handler)
    case = report["cases"][0]
    assert code == 1 and case["final_business_counts"] == {"appointments": 0, "proposals": 1, "handoffs": 0}
    assert all(event["ok"] for event in case["tool_events"])
    assert next(item for item in case["mechanical_checks"] if item["name"] == "zero_proposals_throughout")["passed"] is False


@pytest.mark.parametrize("defect,expected_status", [("http", "http_error"), ("timeout", "timeout"), ("unexpected", "request_error"), ("malformed", "invalid_response_contract")])
def test_request_failures_preserve_attempt_and_global_cap_without_raw_errors(tmp_path, defect, expected_status):
    requests = []
    def handler(request):
        requests.append(request)
        if defect == "http": return httpx.Response(503, text="PRIVATE-PROVIDER-ERROR")
        if defect == "timeout": raise httpx.ReadTimeout("PRIVATE-TRANSPORT-ERROR", request=request)
        if defect == "unexpected": raise RuntimeError("PRIVATE-TRANSPORT-ERROR")
        return httpx.Response(200, text="PRIVATE-NON-JSON")
    code, output, report = invoke(tmp_path, handler, cap=1)
    assert code == 1 and len(requests) == report["model_request_count"] == 1
    case = report["cases"][0]
    assert case["status"] == "failed" and case["model_requests"][0]["status"] == expected_status
    assert case["model_requests"][0]["elapsed_ms"] >= 0
    assert all(case["status"] == "not_run_budget_exhausted" for case in report["cases"][1:])
    assert "PRIVATE-" not in output.read_text(encoding="utf-8")


def test_unexpected_model_identity_fails_without_serializing_provider_string(monkeypatch, tmp_path):
    one_case(monkeypatch)
    code, output, report = invoke(tmp_path, lambda request: reply(model="PRIVATE-MODEL-IDENTITY"))
    assert code == 1 and report["cases"][0]["model_requests"][0]["model_returned"] == "unexpected_or_missing"
    assert "PRIVATE-MODEL-IDENTITY" not in output.read_text(encoding="utf-8")


def test_mid_case_budget_exhaustion_preserves_partial_turns(monkeypatch, tmp_path):
    one_case(monkeypatch, two_turns=True)
    requests = []
    def handler(request):
        requests.append(request)
        return reply()
    code, _, report = invoke(tmp_path, handler, cap=1)
    case = report["cases"][0]
    assert code == 1 and len(requests) == report["model_request_count"] == 1
    assert len(case["turns"]) == 2 and case["turns"][0]["assistant"] and case["turns"][1]["assistant"] == ""
    assert case["status"] == "failed" and not case["mechanical_passed"]


def test_cancellation_writes_attempt_and_interrupted_report(monkeypatch, tmp_path):
    one_case(monkeypatch)
    output = tmp_path / "cancelled.json"
    async def handler(request):
        raise asyncio.CancelledError()
    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await evaluator.run("PRIVATE-KEY", output, provider_client=client)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(go())
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "interrupted" and report["model_request_count"] == 1
    assert report["cases"][0]["status"] == "interrupted"
    assert report["cases"][0]["model_requests"][0]["status"] == "cancelled"
    assert report["overall_passed"] is None and not report["mechanical_passed"]


def test_cleanup_failure_still_checkpoints_evidence(monkeypatch, tmp_path):
    one_case(monkeypatch)
    class BadClose(httpx.AsyncClient):
        async def aclose(self):
            await super().aclose()
            raise RuntimeError("PRIVATE-CLEANUP-ERROR")
    code, output, report = invoke(tmp_path, lambda request: reply(), client_class=BadClose)
    assert code == 1 and report["status"] == "incomplete"
    assert report["model_request_count"] == 1 and report["cases"][0]["turns"][0]["assistant"]
    assert "PRIVATE-CLEANUP-ERROR" not in output.read_text(encoding="utf-8")


def test_repetition_is_advisory_preserves_legitimate_digit_repeats(monkeypatch, tmp_path):
    text = "Oil oil. Diagnostic diagnostic. Callback 5 5 5, five five five. No, no."
    flags = evaluator.repetition_flags(text)
    assert {flag.get("token") for flag in flags} >= {"oil", "diagnostic"}
    assert all(flag.get("token") not in {"5", "five"} for flag in flags)
    one_case(monkeypatch)
    code, _, report = invoke(tmp_path, lambda request: reply(text))
    assert code == 0  # Mechanical state is safe; prose is explicitly pending review.
    assert report["overall_passed"] is None
    assert report["cases"][0]["turns"][0]["assistant"] == text
    assert report["cases"][0]["turns"][0]["repetition_advisories"] == flags


def test_whole_request_deadline_limits_slow_stream(monkeypatch, tmp_path):
    one_case(monkeypatch)
    monkeypatch.setattr(evaluator, "REQUEST_DEADLINE_SECONDS", 0.01)
    async def handler(request):
        await asyncio.sleep(5)
        return reply()
    code, _, report = invoke(tmp_path, handler)
    assert code == 1 and report["model_request_count"] == 1
    assert report["cases"][0]["model_requests"][0]["status"] == "timeout"


def test_oversized_response_is_not_retained(monkeypatch, tmp_path):
    one_case(monkeypatch)
    monkeypatch.setattr(evaluator, "MAX_RESPONSE_BYTES", 100)
    code, output, report = invoke(tmp_path, lambda request: httpx.Response(200, text="PRIVATE-BODY" * 100))
    assert code == 1 and report["model_request_count"] == 1
    assert report["cases"][0]["model_requests"][0]["status"] == "request_error"
    assert "PRIVATE-BODY" not in output.read_text(encoding="utf-8")


