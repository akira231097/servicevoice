"""Operator tools: the dashboard plan and the HTML report stay content-free and bounded."""
import json
from pathlib import Path

import pytest

from app.live_eval_evidence import VERSION
from evals import call_report, langsmith_dashboard

PROJECT = "aed7dd11-c235-46a1-982f-a1454c4fbf40"


def test_dashboard_charts_filter_on_current_call_reviews_only():
    rows = langsmith_dashboard.charts(PROJECT, "ServiceVoice calls to review")
    kinds = [kind for kind, _ in rows]
    assert kinds[0] == "text" and kinds.count("kpi") == 14 and kinds.count("line") == 2 and kinds.count("bar") == 3
    assert VERSION in rows[0][1]["markdown"] and "ServiceVoice calls to review" in rows[0][1]["markdown"]
    for kind, chart in rows[1:]:
        for series in chart["series"]:
            definition = series["filter_definition"]
            assert definition["project_ids"] == [PROJECT] and 'eq(name, "voice.call.review")' in definition["run_filter"]
            assert f'eq(metadata_value, "{VERSION}")' in definition["run_filter"]
            assert series["metric_definition"]["type"] in {"count", "avg", "sum", "max", "min"}
    keys = {s["metric_definition"].get("params", {}).get("feedback_key") for _, chart in rows[1:] for s in chart["series"]}
    assert {"needs_review", "slow_call", "hallucination_free", "metric_first_response_avg_ms", "metric_answer_p95_ms", "metric_cost_usd"} <= keys
    grouped = [s for _, chart in rows[1:] for s in chart["series"] if s.get("group_by_definitions")]
    assert grouped and grouped[0]["group_by_definitions"] == [{"attribute": "metadata", "path": "ended_reason"}]


def test_dashboard_refuses_invalid_names_and_existing_output(tmp_path, monkeypatch):
    existing = tmp_path / "receipt.json"
    existing.write_text("{}")
    with pytest.raises(SystemExit) as refused:
        langsmith_dashboard.main(["--project", "bad/name"])
    assert "invalid_project_name" in str(refused.value)
    with pytest.raises(SystemExit) as refused:
        langsmith_dashboard.main(["--output", str(existing)])
    assert "output_exists" in str(refused.value)


def sample_report():
    return {"review_version": VERSION, "call_summary": {"channel": "inboundPhoneCall", "ended_reason": "customer-ended-call", "caller_utterances": 2, "assistant_utterances": 3},
            "1 Summary": {"headline": "Inbound phone call · 2 min 0 s · 2 caller turns · 1 appointment saved · ended: caller hung up · checks clear",
                          "lines": ["Every automatic check passed or did not apply."], "what_to_check": ["Nothing flagged. <script>alert(1)</script>"]},
            "2 Scorecard": {"groups": [{"group": "Speed", "checks": [{"check": "answer_within_target", "question": "The real answer started quickly on the server", "result": "PASS", "why": "answers started within target on the server", "evidence": []}]}],
                            "legend": "PASS / FAIL come from evidence."},
            "3 Turn-by-turn timeline": {"turns": ["Turn 1 · caller spoke at 0:05.0 for 1.8 s · spoken answer after 2.60 s on the server"], "details": [],
                                        "speech_synthesis": {"requests": 2, "cached_cue_clips": 1, "first_audio_p95_ms": 210, "failed": 0},
                                        "join_basis": {"artifact_rows_attached_by_ordinal": True, "vapi_turn_latencies_attached_by_ordinal": True, "note": "n"}},
            "4 Latency": {"summary": ["Callers heard a response 1.50 s on average."], "first_response_avg_ms": 1500, "first_response_p95_ms": 1800, "answer_avg_ms": 2250, "answer_p95_ms": 2600, "cue_start_p95_ms": 570,
                          "targets_ms": {"turn_average": 2000, "turn_p95": 3000, "server_answer_p95": 3000}},
            "5 Waiting cues": {"turns_with_a_cue": 1, "caller_turns": 2, "follow_up_cues": 0, "cue_start_p95_ms": 570, "phrases_used": {"Let me check that.": 1}, "turns_without_a_cue_by_reason": {"caller gave private details": 1}, "meaning": "m"},
            "6 Booking and actions": {"appointment_count": 1, "committed_actions": 1, "unresolved_operations": 0, "tools": [{"turn": 2, "name": "confirm_action", "ok": True, "error": None, "duration_ms": 48, "receipt_present": True}],
                                      "provider_cost": {"total_usd": .27, "platform_usd": .25, "speech_recognition_usd": .02}},
            "7 How to read this review": {"what_this_cannot_prove": ["What the caller actually heard."]}}


def test_html_report_escapes_text_and_contains_every_section(tmp_path):
    report = sample_report()
    page = call_report.render(report, run_id="4aa3c2fa-a6a2-4a3f-a2e0-f19b58dd7534")
    assert "<script>" not in page and "&lt;script&gt;" in page
    for needle in ("Inbound phone call", "The real answer started quickly on the server", "Turn 1 · caller spoke", "2.60 s", "Let me check that.", "confirm_action", "$0.2700", "What the caller actually heard.", "4aa3c2fa"):
        assert needle in page
    output = tmp_path / "review.html"
    source = tmp_path / "review.json"
    source.write_text(json.dumps({"outputs": report}), encoding="utf-8")
    assert call_report.main(["--report", str(source), "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8").startswith("<!doctype html>")
    with pytest.raises(SystemExit) as refused:
        call_report.main(["--report", str(source), "--output", str(output)])
    assert "output_exists" in str(refused.value)
    source.write_text(json.dumps({"review_version": VERSION}), encoding="utf-8")
    with pytest.raises(SystemExit) as refused:
        call_report.main(["--report", str(source), "--output", str(tmp_path / "other.html")])
    assert "report_has_no_readable_sections" in str(refused.value)
