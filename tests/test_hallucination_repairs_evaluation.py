"""The hallucination evaluator's integrity; all network I/O is MockTransport."""
import asyncio
from datetime import datetime
import json
import re

import httpx
import pytest

from app.gemini_gateway import render_system_prompt
from scripts import evaluate_hallucination_repairs as evaluator


def deny(*args, **kwargs):
    raise AssertionError("Credential/provider access must not happen")


def reply(text="Synthetic answer requiring separate semantic review.", *, name=None, arguments=None, identity="test-call"):
    message = {"role": "assistant", "content": text, "reasoning_content": "PRIVATE-REASONING"}
    if name:
        message["tool_calls"] = [{"id": identity, "type": "function",
                                  "function": {"name": name, "arguments": json.dumps(arguments or {})},
                                  "extra_content": {"google": {"thought_signature": "PRIVATE-SIGNATURE"}}}]
    return httpx.Response(200, json={"model": evaluator.MODEL,
                                   "choices": [{"message": message, "finish_reason": "tool_calls" if name else "stop"}],
                                   "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})


def invoke(tmp_path, handler, *, cap=40, client_class=httpx.AsyncClient):
    output = tmp_path / "report.json"
    async def go():
        client = client_class(transport=httpx.MockTransport(handler))
        return await evaluator.run("PRIVATE-KEY", output, cap, provider_client=client)
    code = asyncio.run(go())
    return code, output, json.loads(output.read_text(encoding="utf-8"))


def checked(report, name):
    return next(check["passed"] for check in report["cases"][0]["mechanical_checks"] if check["name"] == name)


def successful_handler(scenario, *, correction_recheck=False):
    """Exercise actual typed tools, not fabricated success-result fixtures."""
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        messages = body["messages"]
        assert messages[0] == {"role": "system", "content": render_system_prompt()}
        assert body["model"] == evaluator.MODEL and body["reasoning_effort"] == "low"
        assert not any(key in body for key in ("temperature", "top_p", "top_k"))
        for message in messages:
            if message.get("tool_calls"):
                assert message["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "PRIVATE-SIGNATURE"
        users = [index for index, message in enumerate(messages) if message["role"] == "user"]
        turn = len(users)
        latest = messages[users[-1] + 1:]
        results = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        latest_results = [json.loads(message["content"]) for message in latest if message["role"] == "tool"]
        def call(name, arguments):
            return reply(None, name=name, arguments=arguments, identity=f"call-{turn}-{len(latest_results)}")
        if scenario in {"create_withdraw_hindi", "withdraw_and_hours"}:
            if latest_results:
                return reply()
            return call("request_handoff", {"reason": "Synthetic local request"}) if turn == 1 else call("withdraw_handoff", {"handoff_id": results[0]["id"]})
        if scenario in {"exact_afternoon_time", "exclusive_after_time"}:
            if latest_results:
                return reply()
            day = re.search(r"\d{4}-\d{2}-\d{2}", messages[users[-1]]["content"]).group()
            filter_value = {"exact_time": "14:00"} if scenario == "exact_afternoon_time" else {"start_time": "14:00", "start_inclusive": False}
            return call("search_availability", {"service": "brakes", "date": day, "time_filter": filter_value})
        if scenario == "foreign_request_withdrawal":
            if latest_results:
                assert latest_results[0]["error"] == "handoff_missing"
                return reply()
            foreign_id = re.search(r"handoff_[0-9a-f]+", messages[users[-1]]["content"]).group()
            return call("withdraw_handoff", {"handoff_id": foreign_id})
        if scenario == "correction_then_decline":
            if turn == 1:
                return reply() if latest_results else call("prepare_first_available_booking", {
                    "strategy": "first_available", "service": "oil_change", "customer_name": "Alex Morgan", "phone": "5125550142", "vehicle": "2021 Toyota Camry"})
            if turn == 2:
                if not latest_results:
                    return call("get_session_context", {})
                if len(latest_results) == 1 and correction_recheck:
                    value = datetime.fromisoformat(latest_results[0]["offered_slots"][0]["start"]).astimezone(evaluator.TZ)
                    return call("search_availability", {"service": "oil_change", "date": value.date().isoformat(),
                                                        "time_filter": {"exact_time": value.strftime("%H:%M")}})
                if len(latest_results) == (2 if correction_recheck else 1):
                    return call("prepare_booking", {"service": "oil_change", "slot_id": latest_results[0]["offered_slots"][0]["id"],
                                                      "customer_name": "Alex Morgan", "phone": "5125550142", "vehicle": "2022 Toyota Camry"})
                return reply()
            return reply() if latest_results else call("dismiss_proposal", {"proposal_id": next(result["proposal_id"] for result in reversed(results) if "proposal_id" in result)})
        raise AssertionError("Unexpected scenario")
    return handler, requests


def test_dry_run_has_no_credentials_client_database_or_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(evaluator, "dotenv_values", deny)
    monkeypatch.setattr(evaluator.httpx, "AsyncClient", deny)
    monkeypatch.setattr(evaluator, "Store", deny)
    output = tmp_path / "unused.json"
    assert evaluator.main(["--output", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry_run" and report["provider_requests"] == 0
    assert report["scenario_count"] == 6 and report["caller_turn_count"] == 10
    assert not output.exists()


@pytest.mark.parametrize("arguments", [["--max-requests", "0"], ["--max-requests", "41"], ["--evaluate"]])
def test_cli_guards_before_credentials(monkeypatch, arguments):
    monkeypatch.setattr(evaluator, "dotenv_values", deny)
    with pytest.raises(SystemExit) as error:
        evaluator.main(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize("cap", [0, 41, True])
def test_run_budget_rejects_before_output_or_client(monkeypatch, tmp_path, cap):
    monkeypatch.setattr(evaluator.httpx, "AsyncClient", deny)
    output = tmp_path / "unused.json"
    with pytest.raises(ValueError):
        asyncio.run(evaluator.run("private", output, cap))
    assert not output.exists()


def test_existing_output_is_immutable_before_credentials_or_client(monkeypatch, tmp_path):
    output = tmp_path / "original.json"
    output.write_bytes(b"first failure remains")
    monkeypatch.setattr(evaluator, "dotenv_values", deny)
    monkeypatch.setattr(evaluator.httpx, "AsyncClient", deny)
    with pytest.raises(SystemExit):
        evaluator.main(["--evaluate", "--output", str(output)])
    with pytest.raises(FileExistsError):
        asyncio.run(evaluator.run("private", output))
    assert output.read_bytes() == b"first failure remains"


@pytest.mark.parametrize("scenario", evaluator.SCENARIOS)
def test_real_scheduler_mechanics_and_signature_privacy(monkeypatch, tmp_path, scenario):
    monkeypatch.setattr(evaluator, "SCENARIOS", (scenario,))
    handler, requests = successful_handler(scenario)
    code, output, report = invoke(tmp_path, handler)
    assert code == 0 and report["mechanical_passed"]
    case = report["cases"][0]
    assert case["mechanical_passed"] and case["status"] == "completed"
    assert report["model_request_count"] == len(requests) <= 7
    assert case["final_state"]["business_counts"]["appointments"] == 0
    assert case["prompt_context"]["trusted_facts_supplied"]
    assert case["semantic_review"] == "pending" and report["overall_passed"] is None
    text = output.read_text(encoding="utf-8")
    assert all(secret not in text for secret in ("PRIVATE-", "thought_signature", "reasoning_content", "<trusted_dealership_facts>"))
    assert all(row["status"] == "response_received" for row in case["model_requests"])


def test_broad_morning_search_cannot_pass_exact_time_case(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluator, "SCENARIOS", ("exact_afternoon_time",))
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        return reply(None, name="search_availability", arguments={"service": "brakes", "date": evaluator.next_weekday()}) if count == 1 else reply("2 PM is unavailable.")
    code, _, report = invoke(tmp_path, handler)
    assert code == 1
    assert checked(report, "fixture_afternoon_hidden_behind_first_three")
    assert not checked(report, "requested_constraint_sent_to_tool")
    assert not checked(report, "exact_query_complete_and_correct")


def test_inclusive_lower_bound_cannot_pass_after_case(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluator, "SCENARIOS", ("exclusive_after_time",))
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        return reply(None, name="search_availability", arguments={"service": "brakes", "date": evaluator.next_weekday(), "time_filter": {"start_time": "14:00"}}) if count == 1 else reply("2 or 3 PM.")
    code, _, report = invoke(tmp_path, handler)
    assert code == 1 and not checked(report, "requested_constraint_sent_to_tool")


def test_mixed_question_receipt_only_metadata_is_detected(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluator, "SCENARIOS", ("withdraw_and_hours",))
    handler, _ = successful_handler("withdraw_and_hours")
    def modified(request):
        response = handler(request)
        data = json.loads(response.content)
        calls = data["choices"][0]["message"].get("tool_calls") or []
        if calls and calls[0]["function"]["name"] == "withdraw_handoff":
            args = json.loads(calls[0]["function"]["arguments"])
            args["presentation"] = {"response_scope": "receipt_only", "response_language": "en", "caller_timezone": "dealership"}
            calls[0]["function"]["arguments"] = json.dumps(args)
        return httpx.Response(200, json=data)
    code, _, report = invoke(tmp_path, modified)
    assert code == 1 and not checked(report, "mixed_question_does_not_select_receipt_only")
    assert checked(report, "one_retained_withdrawn_request")


def test_rejected_early_confirm_attempt_is_still_a_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluator, "SCENARIOS", ("foreign_request_withdrawal",))
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        return reply(None, name="confirm_action", arguments={"proposal_id": "missing", "confirmed": True}) if count == 1 else reply()
    code, _, report = invoke(tmp_path, handler)
    assert code == 1 and not checked(report, "no_prohibited_tool_attempt")
    assert checked(report, "zero_appointments_throughout")


def test_correction_may_revalidate_exact_original_slot_and_preserves_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluator, "SCENARIOS", ("correction_then_decline",))
    handler, _ = successful_handler("correction_then_decline", correction_recheck=True)
    code, _, report = invoke(tmp_path, handler)
    assert code == 0
    assert checked(report, "correction_searches_only_revalidate_original_slot")
    assert checked(report, "vehicle_only_correction_preserves_other_identity")
    case = report["cases"][0]
    original = case["state_checkpoints"][0]["state"]["proposal_identity"]
    corrected = case["state_checkpoints"][1]["state"]["proposal_identity"]
    assert original["slot_id"] == corrected["slot_id"]
    assert original["customer_name_sha256"] == corrected["customer_name_sha256"]
    assert original["customer_phone_sha256"] == corrected["customer_phone_sha256"]
    assert original["vehicle"] == "2021 Toyota Camry" and corrected["vehicle"] == "2022 Toyota Camry"


@pytest.mark.parametrize("field", ["slot_id", "customer_name", "phone"])
def test_correcting_vehicle_cannot_silently_change_other_proposal_identity(monkeypatch, tmp_path, field):
    monkeypatch.setattr(evaluator, "SCENARIOS", ("correction_then_decline",))
    handler, _ = successful_handler("correction_then_decline")
    def modified(request):
        response = handler(request)
        data = json.loads(response.content)
        calls = data["choices"][0]["message"].get("tool_calls") or []
        if calls and calls[0]["function"]["name"] == "prepare_booking":
            args = json.loads(calls[0]["function"]["arguments"])
            if field == "slot_id":
                messages = json.loads(request.content)["messages"]
                context = next(json.loads(message["content"]) for message in messages if message["role"] == "tool" and "offered_slots" in json.loads(message["content"]))
                args[field] = context["offered_slots"][1]["id"]
            else:
                args[field] = "Another Name" if field == "customer_name" else "5125550144"
            calls[0]["function"]["arguments"] = json.dumps(args)
        return httpx.Response(200, json=data)
    code, _, report = invoke(tmp_path, modified)
    assert code == 1
    assert checked(report, "correction_creates_fresh_readback")
    assert not checked(report, "vehicle_only_correction_preserves_other_identity")
    assert checked(report, "zero_appointments_throughout")


@pytest.mark.parametrize("mutation", ["service", "date", "time", "range", "returned_slot"])
def test_correction_search_is_not_a_blanket_allowance(mutation):
    original = {"service": "oil_change", "slot_id": "slot_original", "slot_start": "2026-10-19T09:00:00-05:00"}
    event = {"arguments": {"schema_valid": True, "service": "oil_change", "date": "2026-10-19", "time_filter": {"exact_time": "09:00"}},
             "result": {"ok": True, "complete": True, "total_matching": 1, "slot_ids": ["slot_original"]}}
    assert evaluator.correction_revalidation_matches(event, original)
    if mutation == "service": event["arguments"]["service"] = "brakes"
    elif mutation == "date": event["arguments"]["date"] = "2026-10-20"
    elif mutation == "time": event["arguments"]["time_filter"]["exact_time"] = "10:00"
    elif mutation == "range": event["arguments"]["time_filter"] = {"start_time": "09:00"}
    else: event["result"]["slot_ids"] = ["slot_different"]
    assert not evaluator.correction_revalidation_matches(event, original)


@pytest.mark.parametrize("defect,expected", [("timeout", "timeout"), ("http", "http_error"), ("exception", "request_error")])
def test_failed_attempt_is_preserved_and_global_cap_stops_further_calls(tmp_path, defect, expected):
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        if defect == "timeout":
            raise httpx.ReadTimeout("PRIVATE-FAILURE", request=request)
        if defect == "http":
            return httpx.Response(503, text="PRIVATE-FAILURE")
        raise RuntimeError("PRIVATE-FAILURE")
    code, output, report = invoke(tmp_path, handler, cap=1)
    assert code == 1 and count == report["model_request_count"] == 1
    assert report["cases"][0]["model_requests"][0]["status"] == expected
    assert all(case["status"] == "not_run_budget_exhausted" for case in report["cases"][1:])
    assert "PRIVATE-FAILURE" not in output.read_text(encoding="utf-8")


def test_mid_case_budget_retains_successful_effect_without_claiming_case_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluator, "SCENARIOS", ("create_withdraw_hindi",))
    handler, requests = successful_handler("create_withdraw_hindi")
    code, _, report = invoke(tmp_path, handler, cap=1)
    assert code == 1 and len(requests) == 1
    case = report["cases"][0]
    assert case["final_state"]["handoff_status"] == "callback_requested"
    assert case["final_state"]["business_counts"]["handoffs"] == 1
    assert case["status"] == "failed" and not case["mechanical_passed"]


def test_cancellation_checkpoints_attempt_and_partial_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluator, "SCENARIOS", ("create_withdraw_hindi",))
    def handler(request):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        invoke(tmp_path, handler)
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "interrupted" and report["model_request_count"] == 1
    assert report["cases"][0]["status"] == "interrupted"
    assert report["cases"][0]["model_requests"][0]["status"] == "cancelled"
    assert len(report["cases"][0]["turns"]) == 1 and report["finished_at"]


def test_client_cleanup_failure_preserves_completed_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluator, "SCENARIOS", ("exact_afternoon_time",))
    class BrokenClose(httpx.AsyncClient):
        async def aclose(self):
            await super().aclose()
            raise RuntimeError("PRIVATE-CLOSE-FAILURE")
    handler, _ = successful_handler("exact_afternoon_time")
    code, output, report = invoke(tmp_path, handler, client_class=BrokenClose)
    assert code == 1 and report["status"] == "incomplete"
    assert report["cases"][0]["mechanical_passed"] and report["finished_at"]
    assert "PRIVATE-CLOSE-FAILURE" not in output.read_text(encoding="utf-8")
