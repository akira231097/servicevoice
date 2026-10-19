"""Bounded real-Gemini presentation-metadata exercise; consumes API tokens.

Direct model + disposable scheduler only. This does not exercise gateway receipt
narration, Vapi, audio or consent provenance. Raw tool arguments and Google extras
are never saved. Every report is unique and existing artifacts are refused.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.domain import Store, TZ
from app.tools import DEFINITIONS, Presentation, SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION
from scripts.evaluate_gemini import Conversation, EvaluationFailure, MODEL
from scripts.evaluate_forensic_regressions import next_business_day, seed_afternoon_fixture, state_evidence

SCENARIOS = ("english_dealership", "pacific_timezone", "latest_assent_telugu", "mixed_assent_warranty")
PRESENTABLE_TOOLS = {name for name, (schema, _) in DEFINITIONS.items() if "presentation" in schema.model_fields}
EN = {"response_scope": "receipt_only", "response_language": "en", "caller_timezone": "dealership"}
PACIFIC = {**EN, "caller_timezone": "America/Los_Angeles"}


class CaseFailure(Exception):
    pass


def safe_presentation(value):
    """Only schema-valid, allowlisted presentation fields may reach the report."""
    if value is None:
        return "omitted", None
    try:
        return "valid", Presentation.model_validate(value).model_dump()
    except (ValidationError, TypeError, ValueError):
        return "invalid", None


def matches_presentation_preferences(value, expected):
    """Accept the explicit dealer zone as equivalent to its scope marker only.

    This is preference comparison, not receipt normalization: callers still
    require an exact echo of the original validated metadata in the result.
    Only the renderer's exact en/en-US tags qualify; other tags stay out.
    Missing context and foreign/equal-offset zones stay out.
    """
    status, actual = safe_presentation(value)
    if status != "valid":
        return False
    if expected["response_language"] == "en":
        zones = {"dealership", TZ.key} if expected == EN else {expected["caller_timezone"]}
        return actual["response_scope"] == expected["response_scope"] and actual["response_language"] in {"en", "en-US"} and \
            actual["caller_timezone"] in zones
    return actual == expected


class ObservedConversation(Conversation):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.presentation_events = []
        original_run = self.harness.run

        async def observed_run(sid, name, arguments, tool_id):
            event = {"user_turn": len(self.turns), "tool": name if name in DEFINITIONS else "unknown_tool",
                     "arguments_valid": False, "presentation_status": "omitted", "presentation": None}
            if name in DEFINITIONS:
                try:
                    validated = DEFINITIONS[name][0].model_validate(arguments).model_dump()
                    event["arguments_valid"] = True
                    event["presentation_status"], event["presentation"] = safe_presentation(validated.get("presentation"))
                except (ValidationError, TypeError, ValueError):
                    event["presentation_status"] = "invalid_arguments"
            self.presentation_events.append(event)
            result = await original_run(sid, name, arguments, tool_id)
            event["result_ok"] = result.get("ok") is True
            event["result_presentation_status"], event["result_presentation"] = safe_presentation(result.get("presentation"))
            state = state_evidence(self)
            event["after_tool"] = {"appointments": state["counts"]["appointments"],
                                   "committed_proposals": state["committed_proposals"]}
            return result

        self.harness.run = observed_run


def checkpoint(file, report):
    file.seek(0)
    json.dump(report, file, indent=2, ensure_ascii=False, allow_nan=False)
    file.write("\n")
    file.truncate()
    file.flush()


def check(case, name, condition, *, required=False):
    case["mechanical_checks"].append({"name": name, "passed": bool(condition)})
    if required and not condition:
        raise CaseFailure(name)


def events(conversation, name):
    return [event for event in conversation.presentation_events if event["tool"] == name]


def echo_matches(event):
    return event["result_ok"] and event["presentation_status"] in {"omitted", "valid"} and \
        event["result_presentation_status"] == event["presentation_status"] and event["result_presentation"] == event["presentation"]


async def exercise(name, conversation, case):
    day = next_business_day()
    if name == "pacific_timezone":
        case["fixture"] = seed_afternoon_fixture(conversation.store, day)
        requested_time = "at 1 PM Pacific time, which is 3 PM Central"
    else:
        requested_time = "at your first available time, using dealership-local time"
    request = (f"I want to book an oil change on {day.isoformat()} {requested_time}. Please respond in English. "
               "My name is Alex Morgan, callback 5125550142, vehicle 2021 Toyota Camry. "
               "Please prepare it and read it back before actually booking.")
    await conversation.say(request)
    for _ in range(2):
        if conversation.snapshot()["proposal"]:
            break
        check(case, "no_early_appointment_while_clarifying", conversation.snapshot()["appointment"] is None, required=True)
        clarification = ("I mean the 1 PM Pacific / 3 PM Central appointment on that date." if name == "pacific_timezone"
                         else "The first available time works, using dealership-local time.")
        await conversation.say(clarification + " Use Alex Morgan, 5125550142, 2021 Toyota Camry. Read back the proposal before booking.")
    before = state_evidence(conversation)
    case["before_assent"] = before
    check(case, "proposal_prepared", before["proposal"] is not None, required=True)
    check(case, "no_confirm_tool_or_commit_before_assent", not events(conversation, "confirm_action") and
          before["counts"]["appointments"] == before["committed_proposals"] == 0 and
          all(event.get("after_tool", {}).get("committed_proposals", 0) == 0 for event in conversation.presentation_events), required=True)
    prepared = events(conversation, "prepare_booking")
    check(case, "preparation_arguments_and_result_valid", bool(prepared) and all(event["arguments_valid"] and echo_matches(event) for event in prepared))
    expected = PACIFIC if name == "pacific_timezone" else EN
    check(case, "preparation_metadata_matches_current_preferences", bool(prepared) and matches_presentation_preferences(prepared[-1]["presentation"], expected))
    if name == "pacific_timezone":
        check(case, "pacific_prepared_correct_slot", before["pending_slot_id"] == f"slot_{day.isoformat()}_15", required=True)
    assent = {
        "english_dealership": "Yes, book that exact appointment, please.",
        "pacific_timezone": "Yes, confirm that exact appointment for 1 PM Pacific, 3 PM Central.",
        "latest_assent_telugu": "Yes, book that exact appointment. Please give the confirmation in Telugu.",
        "mixed_assent_warranty": "Yes, confirm that exact appointment. Also, what does the warranty cover?",
    }[name]
    case["assent_user_turn"] = len(conversation.turns) + 1
    await conversation.say(assent)
    final = state_evidence(conversation)
    confirmations = events(conversation, "confirm_action")
    check(case, "one_correct_committed_booking", final["counts"]["appointments"] == final["committed_proposals"] == 1 and
          final["appointment"] is not None and final["appointment"]["status"] == "confirmed" and
          final["appointment"]["service"] == "oil_change" and final["appointment"]["slot_id"] == before["pending_slot_id"])
    check(case, "confirmation_after_assent_once", len(confirmations) == 1 and
          confirmations[0]["user_turn"] == case["assent_user_turn"])
    check(case, "confirmation_arguments_and_result_valid", bool(confirmations) and all(event["arguments_valid"] and echo_matches(event) for event in confirmations))
    if name in {"english_dealership", "pacific_timezone"}:
        check(case, "confirmation_metadata_matches_current_preferences", bool(confirmations) and matches_presentation_preferences(confirmations[-1]["presentation"], expected))
    elif name == "latest_assent_telugu":
        check(case, "latest_language_does_not_reuse_english_metadata", bool(confirmations) and
              confirmations[-1]["presentation_status"] in {"omitted", "valid"} and
              (confirmations[-1]["presentation"] is None or confirmations[-1]["presentation"]["response_language"].split("-")[0] == "te"))
    else:
        latest = [event for event in conversation.presentation_events if event["user_turn"] == case["assent_user_turn"] and event["tool"] in PRESENTABLE_TOOLS]
        check(case, "mixed_request_omits_receipt_only_metadata", bool(confirmations) and
              all(event["presentation_status"] == "omitted" and event["result_presentation_status"] == "omitted" for event in latest))


async def run(key, output, max_requests=40, *, provider_client=None):
    if not 1 <= max_requests <= 40:
        raise ValueError("Request cap must be1 through40.")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x+", encoding="utf-8") as file:
        budget = [max_requests]
        report = {"schema_version": 1, "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
                  "evidence_kind": "direct_gemini_with_disposable_scheduler_and_validated_presentation_observation",
                  "model_requested": MODEL, "reasoning_effort": "low", "prompt_version": SYSTEM_PROMPT_VERSION,
                  "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
                  "maximum_requests": max_requests, "model_request_count": 0, "cases": [],
                  "semantic_review": "pending_separate_review", "overall_passed": None,
                  "limitations": ["Does not invoke gateway receipt narration, SSE, Vapi, voice, microphone or runtime database.",
                                  "Uses the shared authoritative system-prompt renderer, but all replies come from Gemini in this evaluation.",
                                  "Metadata tests do not prove spoken consent or trustworthy language/intent provenance.",
                                  "Only validated presentation fields are saved from tool arguments; raw arguments and opaque Google extras remain in memory.",
                                  "Text correctness, readback completeness, Telugu language and handling of the warranty qualifier require separate review."]}
        checkpoint(file, report)
        try:
            with tempfile.TemporaryDirectory(prefix="servicevoice-presentation-eval-") as directory:
                async with provider_client or httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                    for name in SCENARIOS:
                        case = {"scenario": name, "status": "not_run", "mechanical_checks": [], "mechanical_passed": False}
                        report["cases"].append(case)
                        if budget[0] <= 0:
                            case["status"] = "not_run_budget_exhausted"
                            checkpoint(file, report)
                            continue
                        conversation = ObservedConversation(client, Store(Path(directory) / (name + ".sqlite3")), key, budget)
                        case["prompt_context"] = conversation.prompt_metadata
                        requests_before = budget[0]
                        try:
                            await exercise(name, conversation, case)
                            case["status"] = "completed"
                            case["mechanical_passed"] = bool(case["mechanical_checks"]) and all(item["passed"] for item in case["mechanical_checks"])
                        except (CaseFailure, EvaluationFailure) as exc:
                            case.update(status="failed", failure=str(exc))
                        except (httpx.HTTPError, ValueError, KeyError, TypeError):
                            case.update(status="failed", failure="Transport or response-contract failure; raw error withheld.")
                        except asyncio.CancelledError:
                            case.update(status="interrupted", failure="Interrupted.")
                            raise
                        finally:
                            try:
                                await conversation.harness.close()
                            finally:
                                case["turns"] = conversation.turns
                                case["presentation_events"] = conversation.presentation_events
                                case["model_requests"] = conversation.metrics
                                case["model_request_count"] = requests_before - budget[0]
                                case["tool_counts"] = dict(Counter(event["tool"] for event in conversation.presentation_events))
                                case["final_state"] = state_evidence(conversation)
                                report["model_request_count"] = max_requests - budget[0]
                                checkpoint(file, report)
                        print(json.dumps({"scenario": name, "status": case["status"], "mechanical_passed": case["mechanical_passed"]}), flush=True)
            report["status"] = "completed" if all(case["status"] == "completed" for case in report["cases"]) else "incomplete"
        except Exception:
            report.update(status="incomplete", failure="Evaluation infrastructure failure; raw error withheld.")
        except BaseException:
            report["status"] = "interrupted"
            raise
        finally:
            report.update(finished_at=datetime.now(timezone.utc).isoformat(), model_request_count=max_requests - budget[0],
                          mechanical_passed=len(report["cases"]) == len(SCENARIOS) and all(case["mechanical_passed"] for case in report["cases"]))
            checkpoint(file, report)
    print("Saved presentation evaluation; separate text review remains required: " + str(output))
    return 0 if report["status"] == "completed" and report["mechanical_passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-requests", type=int, default=40)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 40:
        parser.error("Request cap must be1 through40.")
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    output = args.output or ROOT / "docs" / ("gemini-presentation-evaluation-" + suffix + ".json")
    if not output.is_absolute():
        output = ROOT / output
    if output.exists():
        parser.error("Existing report refused; no requests made.")
    key = (dotenv_values(ROOT / ".env").get("GEMINI_API_KEY") or "").strip()
    if not key:
        parser.error("GEMINI_API_KEY is missing; no requests made.")
    return asyncio.run(run(key, output, args.max_requests))


if __name__ == "__main__":
    raise SystemExit(main())
