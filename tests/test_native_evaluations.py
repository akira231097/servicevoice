"""Failure-oriented native evaluation integration tests; no provider network."""
import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from evals.native import build_native_payload, code_feedback, judge_feedback
from evals.review import make_review_template, DIMENSIONS
from evals.schema import digest
from test_eval_langsmith import fixture


def bundle():
    dataset, report, raw, sha = fixture()
    report["configuration"].update(prompt_version="2026-10-18-gemini-agentic-18", model="gemini-3.8-flash",
        transport="streaming_gateway", trusted_dealership_facts={"hours": "Weekdays 8 AM to 5 PM Central"})
    report["cases"][0]["family"] = "verified_facts"
    review = make_review_template(report)
    for case in review["cases"]:
        for value in case["dimensions"].values():
            value.update(verdict="pass", evidence="turn:1: Recorded synthetic response.")
    result = {"run_id": report["run_id"], "report_sha256": digest(report), "dataset_sha256": sha,
        "model_requested": "gemini-3.8-flash", "cases": [{"id": "SV-R001", "attempt": 1, "status": "judged_advisory",
            "output_sha256": report["cases"][0]["output_sha256"]}]}
    return dataset, report, review, result, sha


def payload(bundle_args, label="servicevoice-v18-unit-test"):
    d, r, v, j, s = bundle_args
    return build_native_payload(d, r, v, j, dataset_sha256=s, label=label)


def test_native_comparison_shares_dataset_examples_but_labels_versions():
    args = bundle()
    old = payload(args, "servicevoice-v17-unit-test")
    new = payload(args)
    assert old["dataset_id"] == new["dataset_id"]
    assert old["results"][0]["row_id"] == new["results"][0]["row_id"]
    assert old["experiment_name"] != new["experiment_name"]
    assert new["experiment_metadata"]["release_ready"] is False
    scores = {s["key"]: s for s in new["results"][0]["evaluation_scores"]}
    assert scores["code_mechanical_contract"]["score"] == 1
    assert scores["judge_factual_grounding"]["feedback_source"]["type"] == "model"


@pytest.mark.parametrize("verdict", ["fail", "uncertain"])
def test_judge_non_pass_never_becomes_a_passing_score(verdict):
    args = bundle()
    args[2]["cases"][0]["dimensions"]["factual_grounding"]["verdict"] = verdict
    scores = payload(args)["results"][0]["evaluation_scores"]
    item = next(s for s in scores if s["key"] == "judge_factual_grounding")
    assert item["score"] == 0 and item["value"] == verdict


def test_missing_judgment_is_explicit_not_silently_omitted():
    args = bundle()
    args[3]["cases"] = []
    scores = payload(args)["results"][0]["evaluation_scores"]
    item = next(s for s in scores if s["key"] == "judge_factual_grounding")
    assert item["score"] == 0 and item["value"] == "unavailable"


@pytest.mark.parametrize("mutation", [
    lambda j: j.update(run_id="wrong"),
    lambda j: j.update(report_sha256="a" * 64),
    lambda j: j.update(dataset_sha256="b" * 64),
])
def test_stale_or_other_run_judgment_is_rejected(mutation):
    args = bundle()
    mutation(args[3])
    with pytest.raises(ValueError):payload(args)


def test_raw_provider_fields_do_not_enter_native_tool_evidence():
    args = bundle()
    case = args[1]["cases"][0]
    case["outputs"]["tools"][0].update(thought_signature="DO-NOT-EXPORT-SIGNATURE", headers={"private": "DO-NOT-EXPORT-HEADER"})
    case["outputs"]["tools"][0]["result"] = {"api_key": "DO-NOT-EXPORT-KEY", "message": "Verified synthetic hours."}
    case["output_sha256"] = digest(case["outputs"])
    args[3]["report_sha256"] = digest(args[1])
    args[2]["report_sha256"] = digest(args[1])
    args[3]["cases"][0]["output_sha256"] = case["output_sha256"]
    raw = json.dumps(payload(args))
    assert all(text not in raw for text in ("DO-NOT-EXPORT-SIGNATURE", "DO-NOT-EXPORT-HEADER", "DO-NOT-EXPORT-KEY"))


@pytest.mark.parametrize("spoken,expected", [("CSV-1234ABCD", 1), ("CSV-FFFFFFFF", 0)])
def test_fabricated_reference_detection_recomputes_evidence(spoken, expected):
    args = bundle()
    case = args[1]["cases"][0]
    case["outputs"]["turns"][0]["assistant"] = "Your reference is " + spoken
    tool = case["outputs"]["tools"][0]
    tool.update(result={"appointment": {"reference": "local_object_1"}}, result_reference_sha256=hashlib.sha256(b"CSV-1234ABCD").hexdigest())
    # A saved bogus success label must have no effect on the evaluator.
    case["outputs"]["reference_readback"] = [{"status": "match"}]
    score = next(s for s in code_feedback(case) if s["key"] == "code_no_fabricated_reference")
    assert score["score"] == expected


def test_reference_before_any_returned_reference_fails():
    args = bundle()
    case = args[1]["cases"][0]
    case["outputs"]["turns"][0]["assistant"] = "Your booking reference is CSV-FFFFFFFF"
    score = next(s for s in code_feedback(case) if s["key"] == "code_no_fabricated_reference")
    assert score["score"] == 0


def test_interrupted_case_cannot_pass_execution_or_grounding_contract():
    case = bundle()[1]["cases"][0]
    case["status"] = "error"
    scores = {s["key"]: s for s in code_feedback(case)}
    assert scores["code_execution_complete"]["score"] == 0
    assert scores["code_mechanical_contract"]["score"] == 0
    assert scores["code_no_fabricated_reference"]["score"] == 0


def test_disposable_gateway_adapter_uses_real_policy_and_bounded_upstream(tmp_path):
    from app.domain import Store
    from evals.gateway_transport import GatewayConversation
    from test_gemini_gateway import frame
    from scripts.evaluate_voice_quality import RecordingClient
    async def scenario():
        seen = []
        def respond(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                content=frame({"content": "Verified synthetic response."}) + frame(reason="stop") + b"data: [DONE]\n\n")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            rows, budget = [], [2]
            recording = RecordingClient(client, rows, lambda: None)
            conv = GatewayConversation(recording, Store(tmp_path / "disposable.sqlite3"), "synthetic-key", budget)
            try:
                turn = await conv.say("Please clarify what you need; do not take any action.")
                assert turn["assistant"] == "Verified synthetic response."
                assert budget == [1] and len(rows) == 1
                assert conv.generations[0]["gateway"]["route"] == "gemini"
                assert seen[0]["model"] == "gemini-3.8-flash"
                assert "trusted_dealership_facts" in json.dumps(seen[0]["messages"])
                assert conv.snapshot()["appointment"] is None
            finally:
                await conv.close()
                await conv.close()
            assert not client.is_closed
            second = GatewayConversation(recording, Store(tmp_path / "second.sqlite3"), "synthetic-key", budget)
            try:
                assert (await second.say("Please clarify, without taking an action."))["assistant"] == "Verified synthetic response."
                assert budget == [0] and len(rows) == 2
                assert second.sid != conv.sid
            finally:
                await second.close()
            assert not client.is_closed
    asyncio.run(scenario())
