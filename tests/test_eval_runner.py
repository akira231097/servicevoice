"""Independent failure-oriented checks of the evaluation system itself.

MockTransport supplies model responses; actual ToolHarness/Store effects remain
real and disposable. No provider, live server or existing data is contacted.
"""
import asyncio
from copy import deepcopy
from datetime import date
import json
from pathlib import Path

import dotenv
import httpx
import pytest

from runtime_sqlite import ensure_sqlite
ensure_sqlite()
from evals import run, schema
from scripts.evaluate_gemini import MODEL


def source_report():
    return {"mode": "model", "selection": [{"id": "case", "attempt": 1}],
            "source": {"before": {"source.py": "a" * 64}, "after": {"source.py": "a" * 64}},
            "dataset": {"sha256": "b" * 64, "sha256_after": "b" * 64},
            "cases": [{"id": "case", "attempt": 1, "status": "completed",
                       "checks": [{"name": "invariant", "passed": True}], "semantic_review": {"status": "pending"}}]}


def dataset():
    return json.loads(run.DATASET.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(selection=[]),
    lambda r: r.update(cases=[]),
    lambda r: r["cases"].append(deepcopy(r["cases"][0])),
    lambda r: (r["selection"].append(deepcopy(r["selection"][0])), r["cases"].append(deepcopy(r["cases"][0]))),
    lambda r: r["selection"][0].update(attempt=True),
    lambda r: r.update(dataset={}),
    lambda r: r["dataset"].update(sha256="not-a-hash", sha256_after="not-a-hash"),
    lambda r: r["dataset"].update(sha256_after="c" * 64),
    lambda r: r["source"].update(before={}, after={}),
    lambda r: r["source"].update(before={"a": True}, after={"a": True}),
    lambda r: r["source"]["after"].update(other="c" * 64),
    lambda r: r["cases"][0].update(checks=[]),
    lambda r: r["cases"][0].update(checks=[{"passed": "true"}]),
    lambda r: r["cases"][0].update(status="not_run"),
])
def test_missing_duplicate_changed_or_malformed_evidence_cannot_green(mutation):
    report = source_report()
    mutation(report)
    assert schema.gate(report)["mechanical_checks_passed"] is False


def test_mechanical_success_never_completes_semantic_or_release_gate():
    result = schema.gate(source_report())
    assert result["mechanical_checks_passed"] is True
    assert result["semantic_review_pending_or_failed"] == ["case#1"]
    assert result["release_ready"] is False


def test_holdout_requires_explicit_split_and_ids_cannot_silently_duplicate():
    data = dataset()
    held = next(c["id"] for c in data["cases"] if c["split"] == "holdout")
    assert all(c["split"] == "regression" for c in schema.select_cases(data))
    with pytest.raises(ValueError):
        schema.select_cases(data, ids=[held])
    assert schema.select_cases(data, split="holdout", ids=[held])[0]["id"] == held
    first = schema.select_cases(data)[0]["id"]
    for ids in ([], [first, first], ["missing"]):
        with pytest.raises(ValueError):
            schema.select_cases(data, ids=ids)


@pytest.mark.parametrize("mutation", [
    lambda c: c["expected"]["final"].update(unimplemented_check=0),
    lambda c: c["expected"]["final"].update(appointments=False),
    lambda c: c["expected"].update(forbidden_tools=["unknown_misspelled_tool"]),
    lambda c: c["turns"].append("Use {{SECRET}}"),
    lambda c: c["turns"].append("Use {{DATE"),
    lambda c: c.update(execution=[]),
    lambda c: c.update(family=None),
])
def test_unknown_expectations_and_templates_fail_validation(mutation):
    data = dataset()
    mutation(data["cases"][0])
    with pytest.raises(ValueError):
        schema.validate_dataset(data)


def test_missing_template_value_is_not_sent_as_literal_placeholder():
    with pytest.raises(ValueError):
        schema.render_case({"turns": ["On {{DATE}}"]}, {})
    with pytest.raises(ValueError):
        schema.render_case({"turns": ["{{NAME}}"]}, {"NAME": "{{DATE}}"})


def test_repeat_reliability_keeps_every_trial_and_missing_is_unknown():
    selection = [{"id": "x", "attempt": n} for n in (1, 2, 3)]
    cases = [{"id": "x", "attempt": n, "status": "completed", "checks": [{"passed": n != 2}]} for n in (1, 2, 3)]
    row = run.reliability(cases, selection)["cases"][0]
    assert row["mechanical_successes"] == 2
    assert row["mechanical_pass_power_k"] == {"1": 2 / 3, "2": 1 / 3, "3": 0}
    incomplete = run.reliability(cases[:-1], selection)["cases"][0]
    assert incomplete["coverage_complete"] is False
    assert all(v is None for v in incomplete["mechanical_pass_power_k"].values())


def test_final_oracle_uses_post_drain_state_and_strict_value_types():
    case = {"turns": ["No booking."], "expected": {"turn_checks": [{"after_turn": 1, "appointments": 0}],
            "final": {"appointments": 0}, "required_tools": [], "forbidden_tools": []}}
    checks = schema.grade_case(case, [{"appointments": 0}], [], 1, final_state={"appointments": 1})
    assert next(c for c in checks if c["name"] == "turn_1.appointments")["passed"] is True
    assert next(c for c in checks if c["name"] == "final.appointments")["passed"] is False
    checks = schema.grade_case(case, [{"appointments": False}], [], 1)
    assert not all(c["passed"] for c in checks)


def test_same_turn_preparation_confirmation_is_not_allowed_by_successful_effects():
    case = {"turns": ["Book."], "expected": {"turn_checks": [], "final": {"appointments": 1}, "required_tools": [], "forbidden_tools": []}}
    tools = [{"name": "prepare_booking", "ok": True, "user_turn": 1, "result": {"proposal_id": "p"}},
             {"name": "confirm_action", "ok": True, "user_turn": 1, "arguments": {"proposal_id": "p"}}]
    checks = schema.grade_case(case, [{"appointments": 1}], tools, 1)
    assert next(c for c in checks if c["name"].startswith("confirmation_requires_prior_turn"))["passed"] is False


def reply(text=None, tool=None, arguments=None, *, index=1):
    message = {"role": "assistant", "content": text,
               "reasoning_content": "PRIVATE-REASONING-NEVER-EXPORT"}
    if tool:
        message["tool_calls"] = [{"id": f"mock-call-{index}", "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)},
                                  "extra_content": {"google": {"thought_signature": "PRIVATE-SIGNATURE-NEVER-EXPORT"}}}]
    return httpx.Response(200, json={"model": MODEL, "choices": [{"message": message, "finish_reason": "tool_calls" if tool else "stop"}],
                                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


def execute(tmp_path, handler, *, cap=8, raw_case=None):
    case = deepcopy(raw_case or dataset()["cases"][0])
    case = schema.render_case(case, run.variables_for(date.today()))
    report = {"cases": []}
    budget = [cap]
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run.execute_case(case, 1, client, "PRIVATE-KEY-NEVER-EXPORT", budget, tmp_path, report)
    row = asyncio.run(invoke())
    return row, report, cap - budget[0]


def advisor_provider(*, wrong_id=False):
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        index = len(requests)
        if index == 1:
            return reply(tool="request_handoff", arguments={"reason": "Synthetic roof-rack question"}, index=index)
        if index == 2:
            return reply("The local request is saved; no one was contacted.")
        if index == 3:
            original = next(json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool")
            return reply(tool="withdraw_handoff", arguments={"handoff_id": "incorrect-object" if wrong_id else original["id"]}, index=index)
        return reply("The local request was withdrawn." if not wrong_id else "I could not confirm withdrawal.")
    return handler, requests


@pytest.mark.parametrize("wrong_id", [False, True])
def test_mock_model_drives_real_tools_correct_and_wrong_arguments(tmp_path, wrong_id):
    handler, requests = advisor_provider(wrong_id=wrong_id)
    row, report, used = execute(tmp_path, handler)
    assert used == len(requests) == 4
    assert row["status"] == "completed"
    assert all(c["passed"] for c in row["checks"]) is (not wrong_id)
    assert row["outputs"]["state"]["handoff_status"] == ("callback_requested" if wrong_id else "withdrawn")
    assert row["semantic_review"]["status"] == "pending"
    serialized = json.dumps(report)
    assert "PRIVATE-" not in serialized
    assert row["outputs"]["tools"][0]["result"]["id"].startswith("local_object_")


def test_budget_exhaustion_preserves_prior_effect_and_partial_next_turn(tmp_path):
    handler, requests = advisor_provider()
    row, report, used = execute(tmp_path, handler, cap=2)
    assert used == len(requests) == 2
    assert row["status"] == "error" and row["error_code"] == "request_budget_exhausted"
    assert len(row["outputs"]["turns"]) == 1
    assert row["outputs"]["partial_turn"]["user"].startswith("Actually, withdraw")
    assert row["outputs"]["state"]["handoff_status"] == "callback_requested"
    assert json.loads((tmp_path / "checkpoint.json").read_text())["cases"][0]["status"] == "error"


def test_last_budgeted_request_timeout_is_timeout_and_partial_text_is_preserved(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return reply("Preparing the local request. ", tool="request_handoff", arguments={"reason": "Synthetic request"})
        raise httpx.ReadTimeout("PRIVATE-TRANSPORT-ERROR", request=request)
    row, report, used = execute(tmp_path, handler, cap=2)
    assert used == len(calls) == 2
    assert row["error_code"] == "timeout"
    assert row["outputs"]["partial_turn"]["assistant"] == "Preparing the local request. "
    assert row["outputs"]["state"]["handoffs"] == 1
    assert row["requests"][-1]["status"] == "timeout"
    assert "PRIVATE-" not in json.dumps(report)


def test_cleanup_error_is_checkpointed_and_cannot_green(tmp_path, monkeypatch):
    from app.harness import ToolHarness
    original = ToolHarness.close
    async def broken(self):
        await original(self)
        raise RuntimeError("PRIVATE-CLEANUP-ERROR")
    monkeypatch.setattr(ToolHarness, "close", broken)
    handler, _ = advisor_provider()
    row, report, _ = execute(tmp_path, handler)
    assert row["status"] == "error" and row["error_code"] == "harness_cleanup_failed"
    assert json.loads((tmp_path / "checkpoint.json").read_text())["cases"][0]["error_code"] == "harness_cleanup_failed"
    assert "PRIVATE-" not in json.dumps(report)


def test_effect_completing_during_drain_is_graded_against_durable_final_state(tmp_path, monkeypatch):
    from app.harness import ToolHarness
    original_run, original_close = ToolHarness.run, ToolHarness.close
    async def pending(self, sid, name, arguments, tool_id):
        if name == "request_handoff":
            self.contract_deferred = (sid, name, arguments, tool_id)
            return {"ok": False, "error": "operation_pending", "operation_id": "synthetic-pending"}
        return await original_run(self, sid, name, arguments, tool_id)
    async def drain_commit(self):
        if hasattr(self, "contract_deferred"):
            await original_run(self, *self.contract_deferred)
        await original_close(self)
    monkeypatch.setattr(ToolHarness, "run", pending)
    monkeypatch.setattr(ToolHarness, "close", drain_commit)
    case = deepcopy(dataset()["cases"][0])
    case["turns"] = ["Information only, do not save a request."]
    case["expected"].update(turn_checks=[{"after_turn": 1, "handoffs": 0}], final={"handoffs": 0}, required_tools=[], forbidden_tools=[])
    requests = []
    def provider(request):
        requests.append(request)
        return reply(tool="request_handoff", arguments={"reason": "Unexpected synthetic request"}) if len(requests) == 1 else reply("No request is confirmed saved.")
    row, _, _ = execute(tmp_path, provider, raw_case=case)
    assert row["outputs"]["turn_states"][-1]["handoffs"] == 0
    assert row["outputs"]["state"]["handoffs"] == 1
    assert next(c for c in row["checks"] if c["name"] == "final.handoffs")["passed"] is False


def test_cancellation_preserves_partial_attempt_and_checkpoint_before_propagating(tmp_path):
    report = {"cases": []}
    calls = []
    def provider(request):
        calls.append(request)
        if len(calls) == 1:
            return reply("Partial reply. ", tool="request_handoff", arguments={"reason": "Synthetic request"})
        raise asyncio.CancelledError()
    case = schema.render_case(dataset()["cases"][0], run.variables_for(date.today()))
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            await run.execute_case(case, 1, client, "PRIVATE-KEY", [3], tmp_path, report)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(invoke())
    saved = json.loads((tmp_path / "checkpoint.json").read_text())["cases"][0]
    assert saved["status"] == "interrupted"
    assert saved["outputs"]["partial_turn"]["assistant"] == "Partial reply. "
    assert saved["outputs"]["state"]["handoffs"] == 1
    assert saved["requests"][-1]["status"] == "cancelled"
    assert saved["checks"] and "PRIVATE-" not in json.dumps(saved)


def test_sanitizer_preserves_exact_availability_metadata_and_excludes_signatures():
    clean = run.TraceSanitizer().clean({"total_matching": 6, "returned_count": 3, "truncated": True, "complete": False,
                                      "thought_signature": "PRIVATE-SIGNATURE", "query": {"date": "2026-10-20", "time_filter": {"exact_time": "14:00"}}})
    assert clean["total_matching"] == 6 and clean["truncated"] is True
    assert clean["query"]["time_filter"] == {"exact_time": "14:00"}
    assert "PRIVATE" not in repr(clean)


@pytest.mark.parametrize("mode", ["validate", "model"])
def test_plan_modes_do_not_read_credentials_create_clients_or_artifacts(tmp_path, monkeypatch, capsys, mode):
    def deny(*args, **kwargs):
        raise AssertionError("Plan accessed credentials or provider")
    monkeypatch.setattr(dotenv, "dotenv_values", deny)
    monkeypatch.setattr(dotenv, "load_dotenv", deny)
    monkeypatch.setattr(httpx, "AsyncClient", deny)
    target = tmp_path / "must-not-exist"
    assert run.main([mode, "--output-root", str(target)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "plan_only" and result["provider_calls"] == 0
    assert not target.exists()


def test_duplicate_cli_case_is_error_before_any_credentials(monkeypatch, capsys):
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **k: pytest.fail("credentials accessed"))
    assert run.main(["model", "--execute", "--case", "SV-R001", "--case", "SV-R001"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "configuration_or_artifact_error"


def test_html_escapes_all_untrusted_dialogue_and_check_values():
    attack = '<script>alert("injected")</script><img src=x onerror=alert(1)>'
    report = source_report()
    report.update(run_id=attack, started_at="now", limitations=[attack], gate=schema.gate(report))
    report["cases"][0].update(outputs={"turns": [{"user": attack, "assistant": attack}], "tools": [{"name": attack}]},
                                checks=[{"name": attack, "passed": False, "actual": attack}])
    output = run.render_report(report)
    assert "<script>" not in output and "<img src=x" not in output
    assert "&lt;script&gt;" in output


def test_finalization_template_error_retains_checkpoint_without_completed_result(tmp_path, monkeypatch):
    from evals import review
    data = dataset()
    directory, report = run.new_run(data, run.DATASET, "model", [{"id": "case", "attempt": 1}], tmp_path)
    report["cases"] = source_report()["cases"]
    def broken(report):
        raise ValueError("PRIVATE-REVIEW-ERROR")
    monkeypatch.setattr(review, "make_review_template", broken)
    with pytest.raises(ValueError, match="artifact_finalization_failed"):
        run.finish(directory, report, run.DATASET)
    assert not (directory / "result.json").exists()
    saved = json.loads((directory / "checkpoint.json").read_text())
    assert saved["status"] == "finalization_failed"
    assert "PRIVATE" not in json.dumps(saved)


def test_failed_companion_publication_cannot_publish_completed_result(tmp_path, monkeypatch):
    data = dataset()
    directory, report = run.new_run(data, run.DATASET, "contracts", [{"id": "case", "attempt": 1}], tmp_path)
    report["cases"] = source_report()["cases"]
    original = run.os.link
    def failing_link(source, target):
        if Path(target).name == "report.html":
            raise OSError("PRIVATE-FILESYSTEM-DETAIL")
        original(source, target)
    monkeypatch.setattr(run.os, "link", failing_link)
    with pytest.raises(ValueError, match="artifact_finalization_failed"):
        run.finish(directory, report, run.DATASET)
    assert not (directory / "result.json").exists()
    assert not list(directory.glob(".finalize-*.tmp"))
    assert json.loads((directory / "checkpoint.json").read_text())["status"] == "finalization_failed"
