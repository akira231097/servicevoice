"""Actual evaluator + disposable scheduler under synthetic model responses."""
import asyncio
import json

import httpx
import pytest

from app.domain import Store
from scripts import evaluate_first_available as evaluator
from scripts.evaluate_presentation_metadata import EN, ObservedConversation


@pytest.mark.parametrize("zone,metadata_passes", [
    ("dealership", True), ("America/Chicago", True),
    ("America/New_York", False), (None, False),
])
@pytest.mark.parametrize("language", ["en", "en-US"])
def test_fused_evaluator_accepts_only_eligible_dealer_metadata_with_exact_echo(tmp_path, zone, metadata_passes, language):
    requests = []
    presentation = {**EN, "caller_timezone": zone, "response_language": language} if zone is not None else None

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        messages = body["messages"]
        results = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        tool = None
        if messages[-1]["role"] == "user":
            if not results:
                tool = "prepare_first_available_booking"
                arguments = {"strategy": "first_available", "service": "oil_change",
                             "customer_name": "Alex Morgan", "phone": "5125550142",
                             "vehicle": "2021 Toyota Camry"}
            else:
                tool = "confirm_action"
                arguments = {"proposal_id": results[-1]["proposal_id"], "confirmed": True}
            if presentation is not None:
                arguments["presentation"] = presentation
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"fixture-{len(requests)}", "type": "function",
                "function": {"name": tool, "arguments": json.dumps(arguments)},
                "extra_content": {"google": {"thought_signature": "synthetic-private-signature"}},
            }]}
        else:
            result = results[-1]
            message = {"role": "assistant", "content": result.get("summary", "Synthetic verified result.")}
        return httpx.Response(200, json={"model": evaluator.MODEL,
            "choices": [{"finish_reason": "tool_calls" if tool else "stop", "message": message}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            conversation = ObservedConversation(client, Store(tmp_path / "scheduler.sqlite3"), "synthetic-key", [10])
            case = {"mechanical_checks": []}
            try:
                await evaluator.exercise("earliest_complete", conversation, case)
                return case, conversation.presentation_events
            finally:
                await conversation.harness.close()

    case, events = asyncio.run(run())
    checks = {item["name"]: item["passed"] for item in case["mechanical_checks"]}
    metadata_checks = {"english_receipt_metadata_and_echo", "confirmation_uses_current_english_receipt_metadata"}
    assert len(requests) == 4
    assert all(value for name, value in checks.items() if name not in metadata_checks)
    assert all(checks[name] is metadata_passes for name in metadata_checks)
    assert events[0]["after_tool"] == {"appointments": 0, "committed_proposals": 0}
    assert events[-1]["after_tool"] == {"appointments": 1, "committed_proposals": 1}
    assert all(event["presentation"] == event["result_presentation"] for event in events)
