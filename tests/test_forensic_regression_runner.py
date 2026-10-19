"""Offline runner checks: synthetic HTTP transport and disposable databases only."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.domain import Store
from scripts import evaluate_forensic_regressions as runner


def test_existing_report_refused_without_provider_contact(tmp_path):
    output = tmp_path / "existing.json"
    output.write_text('{"historical":true}', encoding="utf-8")
    contacted = []
    def provider(request):
        contacted.append(request)
        raise AssertionError("No provider contact allowed")
    async def check():
        client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
        try:
            with pytest.raises(FileExistsError):
                await runner.run("synthetic-key", output, 1, provider_client=client)
        finally:
            await client.aclose()
    asyncio.run(check())
    assert not contacted and output.read_text() == '{"historical":true}'


def test_request_cap_and_mechanical_review_separation_with_no_private_extras(tmp_path):
    output = tmp_path / "bounded.json"
    contacted = []
    def provider(request):
        contacted.append(request)
        body = json.loads(request.content)
        assert body["model"] == runner.MODEL and body["reasoning_effort"] == "low"
        assert not {"temperature", "top_p", "top_k"}.intersection(body)
        return httpx.Response(200, json={"model": runner.MODEL, "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "Your appointment is booked at 123 Invented Street.",
            "extra_content": {"google": {"thought_signature": "SYNTHETIC-PRIVATE-SIGNATURE"}}}}], "usage": {"total_tokens": 3}})
    async def check():
        return await runner.run("SYNTHETIC-PRIVATE-KEY", output, 1,
                                provider_client=httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    assert asyncio.run(check()) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert len(contacted) == report["model_request_count"] == 1
    assert len(report["cases"]) == 10 and report["status"] == "incomplete"
    assert report["cases"][0]["mechanical_passed"] is True  # No effects occurred.
    assert all(case["status"] == "not_run_budget_exhausted" for case in report["cases"][1:])
    assert report["overall_passed"] is None and report["semantic_review"]["status"] == "pending_human_review"
    assert "Invented Street" in report["cases"][0]["turns"][0]["assistant"]  # Human must judge this false statement.
    assert "SYNTHETIC-PRIVATE-KEY" not in output.read_text() and "SYNTHETIC-PRIVATE-SIGNATURE" not in output.read_text()


def test_failed_request_is_preserved_and_not_automatically_retried(tmp_path):
    output = tmp_path / "failure.json"
    contacted = []
    def provider(request):
        contacted.append(request)
        return httpx.Response(500, text="SYNTHETIC-PRIVATE-UPSTREAM-ERROR")
    async def check():
        return await runner.run("synthetic-key", output, 1,
                                provider_client=httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    assert asyncio.run(check()) == 1
    report = json.loads(output.read_text())
    first = report["cases"][0]
    assert len(contacted) == first["model_request_count"] == 1
    assert first["mechanical_passed"] is False and "500" in first["failure"]
    assert "SYNTHETIC-PRIVATE-UPSTREAM-ERROR" not in output.read_text()
    assert first["turns"][0]["user"] and not first["turns"][0]["tools"]


def test_afternoon_fixture_is_scoped_out_of_scenario_counts(tmp_path):
    store = Store(tmp_path / "fixture.sqlite3")
    day = runner.next_business_day()
    fixture = runner.seed_afternoon_fixture(store, day)
    sid = store.create_session("live")["id"]
    available = runner.execute(store, sid, "search_availability", {"service": "diagnostic", "date": day.isoformat()}, "scenario-search")
    assert [slot["id"] for slot in available["slots"]] == [f"slot_{day.isoformat()}_{hour}" for hour in (13, 14, 15)]
    conversation = SimpleNamespace(store=store, sid=sid, snapshot=lambda: store.snapshot(sid))
    state = runner.state_evidence(conversation)
    assert state["counts"]["appointments"] == state["committed_proposals"] == 0
    assert fixture["excluded_from_scenario_counts"] is True
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 3


@pytest.mark.parametrize("selection", [[], ["location", "location"], ["unknown"]])
def test_invalid_selection_rejected_before_output_or_provider_contact(tmp_path, selection):
    output = tmp_path / "invalid.json"
    with pytest.raises(ValueError):
        asyncio.run(runner.run("synthetic-key", output, selected_scenarios=selection))
    assert not output.exists()


def test_focused_selection_preserves_order_and_default_remains_all_cases(tmp_path):
    assert list(runner.resolve_scenarios()) == list(runner.SCENARIOS)
    assert list(runner.resolve_scenarios(["location"], True)) == ["location", "telugu_text_only"]
    assert list(runner.resolve_scenarios(["telugu_text_only"], True)) == ["telugu_text_only"]
    output = tmp_path / "focused.json"
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={"model": runner.MODEL, "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "This fictional demo has no verified physical address."}}]})
    async def check():
        return await runner.run("synthetic-key", output, 2, selected_scenarios=["location"],
                                provider_client=httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    assert asyncio.run(check()) == 0
    report = json.loads(output.read_text())
    assert len(calls) == report["model_request_count"] == 1
    assert report["selected_scenarios"] == ["location"]
    assert [case["scenario"] for case in report["cases"]] == ["location"]
    assert report["mechanical_passed"] is True and report["overall_passed"] is None


def test_transport_timeout_preserves_attempt_once_and_sanitizes_error(tmp_path):
    output = tmp_path / "timeout.json"
    calls = []
    def provider(request):
        calls.append(request)
        raise httpx.ReadTimeout("SYNTHETIC-PRIVATE-TIMEOUT", request=request)
    async def check():
        return await runner.run("synthetic-key", output, 3, selected_scenarios=["location"],
                                provider_client=httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    assert asyncio.run(check()) == 1
    report = json.loads(output.read_text())
    assert len(calls) == report["model_request_count"] == 1
    assert report["status"] == "incomplete" and report["finished_at"]
    assert report["cases"][0]["turns"][0]["user"]
    assert "SYNTHETIC-PRIVATE-TIMEOUT" not in output.read_text()


@pytest.mark.parametrize("interrupted", [False, True])
def test_top_level_failure_always_finishes_checkpoint_without_raw_error(tmp_path, monkeypatch, interrupted):
    output = tmp_path / "infrastructure.json"
    async def fail(*args):
        if interrupted:
            raise asyncio.CancelledError("SYNTHETIC-PRIVATE-INTERRUPTION")
        raise RuntimeError("SYNTHETIC-PRIVATE-INFRASTRUCTURE")
    monkeypatch.setattr(runner, "evaluate_cases", fail)
    if interrupted:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(runner.run("synthetic-key", output, 1, selected_scenarios=["location"]))
    else:
        assert asyncio.run(runner.run("synthetic-key", output, 1, selected_scenarios=["location"])) == 1
    report = json.loads(output.read_text())
    assert report["status"] == ("interrupted" if interrupted else "incomplete")
    assert report["finished_at"] and report["model_request_count"] == 0
    assert report["mechanical_passed"] is False and report["overall_passed"] is None
    assert "SYNTHETIC-PRIVATE" not in output.read_text()


@pytest.mark.parametrize("metadata,tools,expected", [
    ({"trusted_facts_supplied": True}, [], True),
    ({"mode": "tool_fallback", "trusted_facts_supplied": False}, [], False),
    ({"mode": "tool_fallback", "trusted_facts_supplied": False}, [{"name": "get_dealership_info", "ok": True}], True),
    ({"mode": "tool_fallback", "trusted_facts_supplied": False}, [{"name": "get_dealership_info", "ok": False}], False),
])
def test_faq_check_requires_actual_server_facts_metadata_or_info_tool(metadata, tools, expected):
    conversation = SimpleNamespace(prompt_metadata=metadata, turns=[{"tools": tools}])
    report = {}
    case = runner.Case(conversation, report)
    case.static_source_available("faq_authoritative_source_available")
    assert report["mechanical_checks"] == [{"name": "faq_authoritative_source_available", "passed": expected}]
    assert report["prompt_context"] == metadata
