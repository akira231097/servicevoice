"""Consent evaluator tests with synthetic HTTP and disposable schedulers only."""
import asyncio
import json

import httpx
import pytest

from scripts import evaluate_consent_boundaries as evaluator


def provider(*, defect=None):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        messages = body["messages"]
        latest = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]

        def respond(*, text=None, tool=None, arguments=None):
            message = {"role": "assistant", "content": text}
            if tool:
                message["tool_calls"] = [{"id": f"fixture-{len(requests)}", "type": "function",
                    "function": {"name": tool, "arguments": json.dumps(arguments)},
                    "extra_content": {"google": {"thought_signature": "SYNTHETIC-PRIVATE-SIGNATURE"}}}]
            return httpx.Response(200, json={"model": evaluator.MODEL, "choices": [{
                "finish_reason": "tool_calls" if tool else "stop", "message": message}]})

        def confirm():
            proposal = next(result for result in reversed(results) if "proposal_id" in result)
            return respond(tool="confirm_action", arguments={"proposal_id": proposal["proposal_id"], "confirmed": True})

        def prepare(vehicle):
            offer = next(result for result in results if "slots" in result)
            return respond(tool="prepare_booking", arguments={"service": "oil_change", "slot_id": offer["slots"][0]["id"],
                "customer_name": "Alex Morgan", "phone": "5125550142", "vehicle": vehicle})

        if not results:
            return respond(tool="begin_service_request", arguments={"service": "oil_change"})
        if messages[-1]["role"] == "user":
            scenario = "conditional_warranty" if "only if" in latest else \
                "yes_but_vehicle_correction" if "vehicle is wrong" in latest else \
                "quoted_friend_yes" if "friend said" in latest else \
                "declined_then_unrelated_then_yes" if latest == "Yes." else None
            if defect is not None and scenario == defect:
                return confirm()
            if scenario == "yes_but_vehicle_correction":
                return prepare("2022 Toyota Camry")
            if "changed my mind" in latest:
                proposal = next(result for result in reversed(results) if "proposal_id" in result)
                return respond(tool="dismiss_proposal", arguments={"proposal_id": proposal["proposal_id"]})
            if "opening hours" in latest:
                return respond(text="Monday through Friday,8AM to5PM Central; closed weekends.")
            if scenario == "conditional_warranty":
                return respond(text="Warranty coverage needs advisor verification. Nothing is booked.")
            if scenario == "quoted_friend_yes":
                return respond(text="Understood. I will wait for your decision; nothing is booked.")
            return respond(text="What are you agreeing to? Your earlier proposal remains declined.")
        last_tool = next(m for m in reversed(messages) if m.get("tool_calls"))["tool_calls"][0]["function"]["name"]
        if last_tool == "begin_service_request":
            return prepare("2021 Toyota Camry")
        if last_tool == "prepare_booking":
            if defect == "early_commit":
                return confirm()
            return respond(text=results[-1]["summary"] + " Would you like to book those details?")
        if last_tool == "dismiss_proposal":
            return respond(text="The proposal is withdrawn; nothing is booked.")
        if last_tool == "confirm_action":
            return respond(text="Synthetic result.")
        raise AssertionError("Unexpected synthetic stage")

    return handler, requests


def invoke(tmp_path, handler, *, cap=40, filename="report.json"):
    output = tmp_path / filename

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await evaluator.run("SYNTHETIC-PRIVATE-KEY", output, cap, provider_client=client)
        finally:
            await client.aclose()

    code = asyncio.run(run())
    return code, output, json.loads(output.read_text(encoding="utf-8"))


def test_all_four_boundaries_preserve_zero_effects_with_corrected_pending_proposal(tmp_path):
    handler, requests = provider()
    code, output, report = invoke(tmp_path, handler)
    assert code == 0 and report["mechanical_passed"]
    assert report["model_request_count"] == len(requests) == 20
    assert len(report["cases"]) == 4
    for case in report["cases"]:
        assert case["mechanical_passed"] and len(case["turns"]) <= 6
        assert case["final_state"]["counts"]["appointments"] == case["final_state"]["committed_proposals"] == 0
        assert all(check["passed"] for check in case["mechanical_checks"])
    correction = report["cases"][1]
    assert correction["corrected_proposal"]["customer"]["vehicle"] == "2022 Toyota Camry"
    assert correction["corrected_proposal"]["id"] != correction["initial_proposal"]["id"]
    assert report["cases"][-1]["original_proposal_after_boundary"]["state"] == "dismissed"
    assert report["semantic_review"] == "pending_separate_review"
    assert "SYNTHETIC-PRIVATE" not in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("defect", list(evaluator.CASES))
def test_wrong_confirmation_attempt_is_preserved_and_stops_later_cases(tmp_path, defect):
    handler, requests = provider(defect=defect)
    code, _, report = invoke(tmp_path, handler)
    index = list(evaluator.CASES).index(defect)
    failed = report["cases"][index]
    assert code == 1 and not report["mechanical_passed"]
    assert failed["status"] == "failed"
    assert any(not check["passed"] for check in failed["mechanical_checks"])
    assert all(case["status"] == "not_run" for case in report["cases"][index + 1:])
    assert report["model_request_count"] == len(requests)
    if defect != "declined_then_unrelated_then_yes":
        assert failed["final_state"]["counts"]["appointments"] == 1
    else:
        # The scheduler blocks revival; the evaluator still rejects the attempted confirm.
        assert failed["final_state"]["counts"]["appointments"] == 0


def test_commit_during_initial_fixture_is_not_mistaken_for_valid_prior_consent(tmp_path):
    handler, requests = provider(defect="early_commit")
    code, _, report = invoke(tmp_path, handler)
    assert code == 1 and report["cases"][0]["final_state"]["counts"]["appointments"] == 1
    assert all(case["status"] == "not_run" for case in report["cases"][1:])
    assert len(requests) == 4


def test_budget_and_existing_file_stop_before_extra_paid_requests(tmp_path):
    handler, requests = provider()
    code, output, report = invoke(tmp_path, handler, cap=1)
    assert code == 1 and len(requests) == report["model_request_count"] == 1
    assert all(case["status"] == "not_run" for case in report["cases"][1:])
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        invoke(tmp_path, handler)
    assert len(requests) == 1 and output.read_bytes() == original


def test_truncated_model_response_is_failed_without_executing_partial_tool_output(tmp_path):
    requests = []

    def truncated(request):
        requests.append(request)
        return httpx.Response(200, json={"model": evaluator.MODEL, "choices": [{"finish_reason": "length",
            "message": {"role": "assistant", "content": "SYNTHETIC-PRIVATE-TRUNCATED"}}]})

    code, output, report = invoke(tmp_path, truncated)
    assert code == 1 and len(requests) == 1
    assert report["cases"][0]["final_state"]["counts"]["appointments"] == 0
    assert report["cases"][0]["model_requests"][0]["finish_reason"] == "length"
    assert "SYNTHETIC-PRIVATE" not in output.read_text(encoding="utf-8")


def test_shared_case_enforces_six_user_turn_limit_before_seventh_request(tmp_path):
    requests = []

    def plain(request):
        requests.append(request)
        return httpx.Response(200, json={"model": evaluator.MODEL, "choices": [{"finish_reason": "stop",
            "message": {"role": "assistant", "content": "Synthetic response."}}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(plain)) as client:
            c = evaluator.Conversation(client, evaluator.Store(tmp_path / "cap.sqlite3"), "synthetic-key", [40])
            case = evaluator.Case(c, {})
            try:
                for _ in range(6):
                    await case.say("Synthetic input.")
                with pytest.raises(evaluator.CaseFailure, match="scenario_turn_cap_reached"):
                    await case.say("A seventh input must not be sent.")
            finally:
                await c.harness.close()

    asyncio.run(run())
    assert len(requests) == 6


def test_default_dry_run_uses_no_credentials_network_or_scheduler(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Dry run accessed external or scheduler capability")
    monkeypatch.setattr(evaluator, "dotenv_values", forbidden)
    monkeypatch.setattr(evaluator, "Store", forbidden)
    monkeypatch.setattr(evaluator.httpx, "AsyncClient", forbidden)
    assert evaluator.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["provider_requests"] == 0 and output["scheduler_created"] is False
    assert len(output["cases"]) == 4


@pytest.mark.parametrize("arguments", [["--evaluate"], ["--max-requests", "41"], ["--output", "unused.json"]])
def test_invalid_or_implicit_paid_configuration_never_reads_credentials(monkeypatch, arguments):
    monkeypatch.setattr(evaluator, "dotenv_values", lambda *a, **k: pytest.fail("credentials accessed"))
    with pytest.raises(SystemExit) as exc:
        evaluator.main(arguments)
    assert exc.value.code == 2


def test_cli_existing_output_is_refused_before_credentials(monkeypatch, tmp_path):
    output = tmp_path / "retained.json"
    output.write_text("preserved", encoding="utf-8")
    monkeypatch.setattr(evaluator, "dotenv_values", lambda *a, **k: pytest.fail("credentials accessed"))
    with pytest.raises(SystemExit) as exc:
        evaluator.main(["--evaluate", "--output", str(output)])
    assert exc.value.code == 2 and output.read_text(encoding="utf-8") == "preserved"
