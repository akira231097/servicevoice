"""Direct-evaluation integrity using synthetic HTTP responses; no paid requests."""
import asyncio
import json
import re

import httpx
import pytest

from scripts import evaluate_presentation_metadata as evaluator


def mock_provider(*, defect=None, dealer_timezone="dealership", language="en"):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        messages = body["messages"]
        user = [message["content"] for message in messages if message["role"] == "user"]
        initial, latest = user[0], user[-1]
        pacific = "1 PM Pacific" in initial
        scenario = "pacific_timezone" if pacific else "latest_assent_telugu" if "Telugu" in latest else \
                   "mixed_assent_warranty" if "warranty" in latest else "english_dealership"
        metadata = evaluator.PACIFIC if pacific else (
            {**evaluator.EN, "caller_timezone": dealer_timezone} if dealer_timezone is not None else None)
        if metadata is not None:
            metadata = {**metadata, "response_language": language}
        for message in messages:
            for call in message.get("tool_calls", []):
                assert call["extra_content"]["google"]["thought_signature"] == "SYNTHETIC-PRIVATE-SIGNATURE"
        tool_messages = [message for message in messages if message["role"] == "tool"]
        tool_results = [json.loads(message["content"]) for message in tool_messages]

        def response(text=None, tool=None, arguments=None):
            message = {"role": "assistant", "content": text}
            if tool:
                message["tool_calls"] = [{"id": f"synthetic-tool-{len(calls)}", "type": "function",
                    "function": {"name": tool, "arguments": json.dumps(arguments)},
                    "extra_content": {"google": {"thought_signature": "SYNTHETIC-PRIVATE-SIGNATURE"}}}]
            return httpx.Response(200, json={"model": evaluator.MODEL, "choices": [{
                "finish_reason": "tool_calls" if tool else "stop", "message": message}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})

        def confirm(presentation):
            prepared = next(item for item in reversed(tool_results) if "proposal_id" in item)
            arguments = {"proposal_id": prepared["proposal_id"], "confirmed": True}
            if presentation is not None:
                arguments["presentation"] = presentation
            return response(tool="confirm_action", arguments=arguments)

        if not tool_messages:
            day = re.search(r"\d{4}-\d{2}-\d{2}", initial).group()
            return response(tool="begin_service_request", arguments={"service": "oil_change", "date": day})
        if messages[-1]["role"] == "user":
            if scenario == "latest_assent_telugu":
                presentation = evaluator.EN if defect == scenario else {**evaluator.EN, "response_language": "te"}
            elif scenario == "mixed_assent_warranty":
                presentation = evaluator.EN if defect == scenario else None
            else:
                presentation = metadata
            return confirm(presentation)
        last_call = next(message for message in reversed(messages) if message.get("tool_calls"))["tool_calls"][0]
        tool = last_call["function"]["name"]
        result = tool_results[-1]
        if tool == "begin_service_request":
            slot = next(item for item in result["slots"] if item["id"].endswith("_15")) if pacific else result["slots"][0]
            return response(tool="prepare_booking", arguments={"service": "oil_change", "slot_id": slot["id"],
                "customer_name": "Alex Morgan", "phone": "5125550142", "vehicle": "2021 Toyota Camry", "presentation": metadata})
        if tool == "prepare_booking":
            if defect == "early_commit" and not pacific:
                return confirm(metadata)
            return response(text=result["summary"] + " Would you like me to book it?")
        if tool == "confirm_action":
            # Deliberately simple prose: these tests validate measurements, not Gemini semantics.
            return response(text="Synthetic saved receipt. " + ("Warranty needs advisor verification." if scenario == "mixed_assent_warranty" else ""))
        raise AssertionError("Unexpected mock-provider stage")

    return handler, calls


def invoke(tmp_path, handler, *, cap=40, output_name="report.json"):
    output = tmp_path / output_name
    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await evaluator.run("SYNTHETIC-PRIVATE-KEY", output, cap, provider_client=client)
        finally:
            await client.aclose()
    code = asyncio.run(run())
    return code, output, json.loads(output.read_text(encoding="utf-8"))


def test_four_scenarios_use_real_schema_and_disposable_scheduler_without_saving_private_extras(tmp_path):
    handler, calls = mock_provider()
    code, output, report = invoke(tmp_path, handler)
    assert code == 0 and report["mechanical_passed"] is True
    assert report["model_request_count"] == len(calls) == 20
    metrics = [metric for case in report["cases"] for metric in case["model_requests"]]
    assert [metric["max_output_tokens"] for metric in metrics] == [request["max_tokens"] for request in calls]
    assert [case["scenario"] for case in report["cases"]] == list(evaluator.SCENARIOS)
    for case in report["cases"]:
        assert case["before_assent"]["counts"]["appointments"] == 0
        assert case["final_state"]["counts"]["appointments"] == 1
        assert case["final_state"]["committed_proposals"] == 1
        assert case["assent_user_turn"] == 2
        assert all(event["arguments_valid"] for event in case["presentation_events"])
        assert all("arguments" not in event and "tool_call_id" not in event for event in case["presentation_events"])
    assert report["overall_passed"] is None and report["semantic_review"] == "pending_separate_review"
    assert "SYNTHETIC-PRIVATE" not in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("defect,check_name", [
    ("latest_assent_telugu", "latest_language_does_not_reuse_english_metadata"),
    ("mixed_assent_warranty", "mixed_request_omits_receipt_only_metadata"),
])
def test_stale_or_inappropriate_receipt_metadata_fails_even_when_booking_commits(tmp_path, defect, check_name):
    handler, calls = mock_provider(defect=defect)
    code, _, report = invoke(tmp_path, handler)
    assert code == 1 and report["mechanical_passed"] is False
    case = next(case for case in report["cases"] if case["scenario"] == defect)
    assert case["final_state"]["counts"]["appointments"] == 1
    assert next(item for item in case["mechanical_checks"] if item["name"] == check_name)["passed"] is False
    assert report["model_request_count"] == len(calls) == 20


def test_early_commit_is_detected_and_preserved_not_laundered_as_success(tmp_path):
    handler, _ = mock_provider(defect="early_commit")
    code, _, report = invoke(tmp_path, handler)
    case = report["cases"][0]
    assert code == 1 and case["status"] == "failed"
    assert case["final_state"]["counts"]["appointments"] == 1
    assert any(event["tool"] == "confirm_action" and event["user_turn"] == 1 for event in case["presentation_events"])
    assert not case["mechanical_passed"]


def test_request_cap_transport_failure_and_existing_artifact_are_preserved(tmp_path):
    calls = []
    def failed(request):
        calls.append(request)
        return httpx.Response(500, text="SYNTHETIC-PRIVATE-PROVIDER-ERROR")
    code, output, report = invoke(tmp_path, failed, cap=1)
    assert code == 1 and len(calls) == report["model_request_count"] == 1
    assert report["cases"][0]["status"] == "failed"
    assert all(case["status"] == "not_run_budget_exhausted" for case in report["cases"][1:])
    assert "SYNTHETIC-PRIVATE" not in output.read_text(encoding="utf-8")
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        invoke(tmp_path, failed, cap=1)
    assert len(calls) == 1 and output.read_bytes() == original


def test_invalid_presentation_is_not_serialized():
    status, presentation = evaluator.safe_presentation({**evaluator.EN, "session_token": "SYNTHETIC-PRIVATE"})
    assert status == "invalid" and presentation is None
    assert evaluator.safe_presentation(None) == ("omitted", None)


@pytest.mark.parametrize("dealer_timezone,expected_code", [
    ("America/Chicago", 0), ("America/New_York", 1), (None, 1),
])
def test_dealer_zone_equivalence_keeps_foreign_and_missing_metadata_out(tmp_path, dealer_timezone, expected_code):
    handler, calls = mock_provider(dealer_timezone=dealer_timezone)
    code, _, report = invoke(tmp_path, handler)
    assert code == expected_code
    assert report["model_request_count"] == len(calls) == 20
    case = next(case for case in report["cases"] if case["scenario"] == "english_dealership")
    assert case["final_state"]["counts"]["appointments"] == 1
    checks = {item["name"]: item["passed"] for item in case["mechanical_checks"]}
    assert checks["preparation_metadata_matches_current_preferences"] is (expected_code == 0)
    assert checks["confirmation_metadata_matches_current_preferences"] is (expected_code == 0)
    assert checks["preparation_arguments_and_result_valid"]
    assert checks["confirmation_arguments_and_result_valid"]


@pytest.mark.parametrize("metadata", [
    None, {**evaluator.EN, "response_language": "en-GB"},
    {**evaluator.EN, "response_language": "en-us"},
    {**evaluator.EN, "response_language": "en-Latn-US"},
    {**evaluator.EN, "response_language": "te"},
    {**evaluator.EN, "caller_timezone": "US/Central"},
    {**evaluator.EN, "caller_timezone": "America/Mexico_City"},
    {**evaluator.EN, "response_scope": "other"},
    {**evaluator.EN, "unexpected": "data"},
])
def test_dealer_equivalence_requires_exact_eligible_locale_scope_and_schema(metadata):
    assert not evaluator.matches_presentation_preferences(metadata, evaluator.EN)


def test_dealer_equivalence_does_not_weaken_exact_receipt_echo():
    event = {"result_ok": True, "presentation_status": "valid", "presentation": evaluator.EN,
             "result_presentation_status": "valid",
             "result_presentation": {**evaluator.EN, "caller_timezone": "America/Chicago"}}
    assert evaluator.matches_presentation_preferences(event["result_presentation"], evaluator.EN)
    assert not evaluator.echo_matches(event)


def test_en_us_matches_supported_renderer_for_dealer_and_pacific_preferences(tmp_path):
    handler, calls = mock_provider(language="en-US", dealer_timezone="America/Chicago")
    code, _, report = invoke(tmp_path, handler)
    assert code == 0 and report["mechanical_passed"]
    assert len(calls) == 20
    pacific = next(case for case in report["cases"] if case["scenario"] == "pacific_timezone")
    assert all(event["presentation"] == event["result_presentation"] for event in pacific["presentation_events"])
    assert not evaluator.matches_presentation_preferences({**evaluator.PACIFIC, "response_language": "en-US", "caller_timezone": "America/New_York"}, evaluator.PACIFIC)


def test_equivalent_english_tag_never_normalizes_unequal_receipt_echo():
    event = {"result_ok": True, "presentation_status": "valid", "presentation": evaluator.EN,
             "result_presentation_status": "valid", "result_presentation": {**evaluator.EN, "response_language": "en-US"}}
    assert evaluator.matches_presentation_preferences(event["result_presentation"], evaluator.EN)
    assert not evaluator.echo_matches(event)
