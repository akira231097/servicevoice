"""Bounded text-only language/incident regressions; dry-run unless --evaluate.

Real evaluation consumes Gemini tokens, uses a disposable scheduler and the
shared authoritative prompt, and preserves failures. It does not test ASR, TTS,
SSE, microphone audio or Vapi. No model judge or automatic retries are used.
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
import time
import unicodedata

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.domain import Store
from app.tools import DEFINITIONS, SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION
from scripts.evaluate_gemini import Conversation, EvaluationFailure, MODEL

SCENARIOS = (
    {"name": "hindi_information", "language_review": "Hindi",
     "turns": ["कृपया हिंदी में जवाब दें। अभी सिर्फ जानकारी चाहिए, कोई अपॉइंटमेंट या सलाहकार अनुरोध मत बनाइए।",
               "आपकी सर्विस सूची में कौन-कौन से काम शामिल हैं?"],
     "review": "Respond in Hindi; list each supported service once, without a fabricated language restriction or scheduling claim."},
    {"name": "telugu_service_correction", "language_review": "Telugu",
     "turns": ["తెలుగులో చెప్పండి: బ్రేకుల తనిఖీ చేస్తారా? నాకు సమాచారం మాత్రమే కావాలి; బుకింగ్ చేయవద్దు.",
               "క్షమించండి, బ్రేకులు కాదు, టైర్ల రొటేషన్ గురించి అడుగుతున్నాను. సమాచారం మాత్రమే చెప్పండి, అపాయింట్‌మెంట్ వద్దు."],
     "review": "Understand the correction from brakes to tire rotation; reply in Telugu without booking, duplicate service names or invented details."},
    {"name": "spanish_services", "language_review": "Spanish",
     "turns": ["¿Ofrecen revisión de frenos y rotación de neumáticos? Respóndame en español; solo consulto, no quiero reservar ni contactar a un asesor."],
     "review": "Answer the two service questions in Spanish, without a booking pitch or restricting supported conversation languages."},
    {"name": "arabic_hours", "language_review": "Arabic",
     "turns": ["أجب بالعربية من فضلك. هل مركز الخدمة مفتوح يوم الأحد؟ أريد معلومات فقط، ولا تحجز موعدًا أو ترسل طلبًا لمستشار."],
     "review": "Answer in Arabic that Sunday is closed, without claiming any action or inventing location details."},
    {"name": "japanese_information", "language_review": "Japanese",
     "turns": ["日本語でお願いします。オイル交換とタイヤの点検はできますか。今は情報だけ必要です。予約や担当者への連絡はしないでください。"],
     "review": "Answer the service question in Japanese; make no appointment, contact claim or invented language restriction."},
    {"name": "incident_corrupted_asr", "language_review": "unclear; clarification required",
     "turns": ["Dira Hindi.", "A Teluguomutler"],
     "review": "Treat corrupted text as uncertain. Ask a concise clarification when needed; do not invent an English/Telugu-only capability, booking, or repetitive catalogue."},
    {"name": "english_service_list", "language_review": "English",
     "turns": ["Give me your service options once, in plain language. I'm comparing shops, not making an appointment or asking anyone to call me."],
     "review": "List the four supported services once each, without reading internal IDs, duplicate synonyms or a booking success claim."},
    {"name": "code_switch_negative_correction", "language_review": "mixed Spanish and English; no fixed-language assumption",
     "turns": ["I only want the service names—no booking. ¿Hacen inspecciones de frenos?",
               "No, I meant tire rotation, not brakes. Solo información; do not schedule anything or request a callback."],
     "review": "Preserve negation and correction across the language switch; no scheduling or handoff, and no claim that only named languages are supported."},
)
ALLOWED_TOOLS = {"get_dealership_info"}
COUNT_TABLES = ("appointments", "proposals", "handoffs")
NUMBER_WORDS = {"zero", "oh", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"}
REQUEST_DEADLINE_SECONDS = 35
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def checkpoint(file, report):
    file.seek(0)
    json.dump(report, file, indent=2, ensure_ascii=False, allow_nan=False)
    file.write("\n")
    file.truncate()
    file.flush()


def repetition_flags(text):
    """Advisory only; never alter text or make a linguistic pass/fail claim."""
    tokens, token = [], []
    for char in text.casefold() + " ":
        if unicodedata.category(char)[0] in {"L", "M", "N"}:
            token.append(char)
        elif token:
            tokens.append("".join(token)); token = []
    flags = []
    for index in range(1, len(tokens)):
        word = tokens[index]
        if word == tokens[index - 1] and not any(char.isnumeric() for char in word) and word not in NUMBER_WORDS:
            flags.append({"kind": "adjacent_word_repeat", "token": word, "token_index": index})
    for width in (2, 3, 4):
        for index in range(width, len(tokens) - width + 1):
            phrase = tokens[index:index + width]
            if phrase == tokens[index - width:index] and not all(word in NUMBER_WORDS or any(char.isnumeric() for char in word) for word in phrase):
                flags.append({"kind": "adjacent_phrase_repeat", "tokens": phrase, "token_index": index})
    return flags[:30]


def state_counts(conversation):
    with conversation.store.connect() as db:
        return {table: db.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (conversation.sid,)).fetchone()[0]
                for table in COUNT_TABLES}


def safe_metric(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000_000 else None


class RecordingClient:
    """Wrap the actual client; record every attempt without request bodies/keys."""
    def __init__(self, client, rows, changed):
        self.client, self.rows, self.changed = client, rows, changed
        self.turn_number = lambda: 0

    async def post(self, *args, **kwargs):
        row = {"request_index": len(self.rows) + 1, "user_turn": self.turn_number(),
               "status": "started", "http_status": None, "elapsed_ms": None,
               "model_returned": None, "finish_reason": None, "usage": {}}
        self.rows.append(row)
        self.changed()
        start = time.perf_counter()
        try:
            # The shared Conversation uses post(); adapt it to bounded streaming
            # without changing its message/signature or real tool behavior.
            async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
                async with self.client.stream("POST", *args, **kwargs) as streamed:
                    row["http_status"] = streamed.status_code
                    body = bytearray()
                    async for chunk in streamed.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ValueError("Response byte limit exceeded.")
                    # aiter_bytes() already decoded any transfer compression.
                    response = httpx.Response(streamed.status_code, content=bytes(body), request=streamed.request)
            row["http_status"] = response.status_code
            row["status"] = "response_received" if response.is_success else "http_error"
            if response.is_success:
                try:
                    data = response.json()
                    model = data.get("model")
                    row["model_returned"] = model if model == MODEL else "unexpected_or_missing"
                    finish = data.get("choices", [{}])[0].get("finish_reason")
                    row["finish_reason"] = finish if finish in {"stop", "tool_calls", "length", "content_filter"} else "unknown_or_missing"
                    usage = data.get("usage") or {}
                    row["usage"] = {name: safe_metric(usage.get(name)) for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    row["status"] = "invalid_response_contract"
            return response
        except asyncio.CancelledError:
            row["status"] = "cancelled"
            raise
        except (httpx.TimeoutException, TimeoutError):
            row["status"] = "timeout"
            raise
        except Exception:
            row["status"] = "request_error"
            raise
        finally:
            row["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 2)
            self.changed()


class ObservedConversation(Conversation):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tool_events = []
        original = self.harness.run

        async def observed(sid, name, arguments, tool_id):
            event = {"user_turn": len(self.turns), "name": name if isinstance(name, str) and name in DEFINITIONS else "unknown_tool",
                     "allowed_information_tool": name in ALLOWED_TOOLS if isinstance(name, str) else False,
                     "ok": None}
            self.tool_events.append(event)
            try:
                result = await original(sid, name, arguments, tool_id)
                event["ok"] = result.get("ok") is True
                return result
            finally:
                event["business_counts_after"] = state_counts(self)

        self.harness.run = observed


def export_turns(conversation):
    # Opaque signatures, reasoning, prompts and raw tool arguments stay in memory.
    return [{"user": turn["user"], "assistant": turn["assistant"],
             "repetition_advisories": repetition_flags(turn["assistant"])} for turn in conversation.turns]


async def run(key, output, max_requests=40, *, provider_client=None):
    if not 1 <= max_requests <= 40:
        raise ValueError("Request cap must be 1 through 40.")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x+", encoding="utf-8") as file:
        budget = [max_requests]
        report = {"schema_version": 1, "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
                  "evidence_kind": "direct_gemini_text_and_disposable_scheduler_not_voice",
                  "model_requested": MODEL, "reasoning_effort": "low", "prompt_version": SYSTEM_PROMPT_VERSION,
                  "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
                  "maximum_requests": max_requests, "model_request_count": 0, "cases": [],
                  "semantic_review": "pending_separate_review", "overall_passed": None,
                  "limitations": ["Does not test ASR, TTS, SSE, Vapi, microphone, audible repetition or the runtime database.",
                                  "Small authored text corpus; language fluency and meaning require independent fluent review.",
                                  "Mechanical success does not establish correct language, truthful spoken claims or general safety.",
                                  "Repetition flags are advisory, preserve original text and ignore numeric tokens and English digit words; other legitimate repeats can be flagged.",
                                  "HTTP duration is not acoustic response latency; no automatic retries or model judge."]}
        checkpoint(file, report)
        client = None
        try:
            client = provider_client or httpx.AsyncClient(timeout=30, follow_redirects=False)
            with tempfile.TemporaryDirectory(prefix="servicevoice-language-eval-") as directory:
                for scenario in SCENARIOS:
                    case = {"scenario": scenario["name"], "language_review": scenario["language_review"],
                            "semantic_review_criteria": scenario["review"], "semantic_review": "pending",
                            "status": "not_run", "mechanical_passed": False, "mechanical_checks": [],
                            "turns": [], "model_requests": [], "tool_events": [], "state_checkpoints": []}
                    report["cases"].append(case)
                    if budget[0] <= 0:
                        case["status"] = "not_run_budget_exhausted"
                        checkpoint(file, report)
                        continue
                    wrapper = RecordingClient(client, case["model_requests"], lambda: checkpoint(file, report))
                    conversation = ObservedConversation(wrapper, Store(Path(directory) / (scenario["name"] + ".sqlite3")), key, budget)
                    wrapper.turn_number = lambda: len(conversation.turns)
                    case["prompt_context"] = conversation.prompt_metadata
                    case["tool_events"] = conversation.tool_events
                    try:
                        for utterance in scenario["turns"]:
                            try:
                                await conversation.say(utterance)
                            finally:
                                case["turns"] = export_turns(conversation)
                                case["state_checkpoints"].append({"after_user_turn": len(conversation.turns), "business_counts": state_counts(conversation)})
                                checkpoint(file, report)
                        case["status"] = "completed"
                    except EvaluationFailure:
                        case.update(status="failed", failure="Model identity, output, tool-round or global request limit failed; raw response withheld.")
                    except asyncio.CancelledError:
                        case.update(status="interrupted", failure="Evaluation interrupted.")
                        raise
                    except Exception:
                        case.update(status="failed", failure="Transport, response-contract or local evaluation failure; raw error withheld.")
                    finally:
                        try:
                            await conversation.harness.close()
                        except asyncio.CancelledError:
                            case.update(status="interrupted", failure="Evaluation interrupted during cleanup.")
                            raise
                        except Exception:
                            case.update(status="failed", failure="Local harness cleanup failed; raw error withheld.")
                        finally:
                            case["turns"] = export_turns(conversation)
                            final_counts = state_counts(conversation)
                            case["final_business_counts"] = final_counts
                            case["tool_counts"] = dict(Counter(event["name"] for event in case["tool_events"]))
                            all_counts = [final_counts] + [item["business_counts"] for item in case["state_checkpoints"]] + [event["business_counts_after"] for event in case["tool_events"] if "business_counts_after" in event]
                            checks = [("all_expected_turns_completed", case["status"] == "completed" and len(case["turns"]) == len(scenario["turns"])),
                                      ("nonempty_assistant_replies", bool(case["turns"]) and all(turn["assistant"].strip() for turn in case["turns"])),
                                      ("no_prohibited_tool_attempt", all(event["allowed_information_tool"] for event in case["tool_events"]))]
                            checks.extend(("zero_" + table + "_throughout", all(count[table] == 0 for count in all_counts)) for table in COUNT_TABLES)
                            case["mechanical_checks"] = [{"name": name, "passed": bool(ok)} for name, ok in checks]
                            case["mechanical_passed"] = all(item["passed"] for item in case["mechanical_checks"])
                            report["model_request_count"] = max_requests - budget[0]
                            checkpoint(file, report)
                    print(json.dumps({"scenario": case["scenario"], "status": case["status"], "mechanical_passed": case["mechanical_passed"]}), flush=True)
            report["status"] = "completed" if all(case["status"] == "completed" for case in report["cases"]) else "incomplete"
        except asyncio.CancelledError:
            report["status"] = "interrupted"
            raise
        except Exception:
            report.update(status="incomplete", failure="Evaluation infrastructure failure; raw error withheld.")
        except BaseException:
            report["status"] = "interrupted"
            raise
        finally:
            try:
                if client is not None:
                    await client.aclose()
            except asyncio.CancelledError:
                report["status"] = "interrupted"
                raise
            except Exception:
                report.update(status="incomplete", failure="Provider client cleanup failed; raw error withheld.")
            finally:
                report.update(finished_at=datetime.now(timezone.utc).isoformat(), model_request_count=max_requests - budget[0],
                              mechanical_passed=len(report["cases"]) == len(SCENARIOS) and all(case["mechanical_passed"] for case in report["cases"]))
                checkpoint(file, report)
    print("Saved text-only evidence; semantic review remains pending: " + str(output))
    return 0 if report["status"] == "completed" and report["mechanical_passed"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluate", action="store_true", help="Explicitly allow paid direct-model requests.")
    parser.add_argument("--max-requests", type=int, default=40)
    parser.add_argument("--output", type=Path, help="New report file; required with --evaluate; existing files are refused.")
    args = parser.parse_args(argv)
    if not 1 <= args.max_requests <= 40:
        parser.error("Request cap must be 1 through 40.")
    if not args.evaluate:
        print(json.dumps({"mode": "dry_run", "provider_requests": 0, "reads_credentials": False,
                          "maximum_requests": args.max_requests, "scenario_count": len(SCENARIOS),
                          "caller_turn_count": sum(len(item["turns"]) for item in SCENARIOS), "scenarios": SCENARIOS}, ensure_ascii=False, indent=2))
        return 0
    if args.output is None:
        parser.error("--evaluate requires --output pointing to a new report file.")
    output = args.output if args.output.is_absolute() else ROOT / args.output
    if output.exists():
        parser.error("Existing report refused; no credentials read or requests made.")
    key = (dotenv_values(ROOT / ".env").get("GEMINI_API_KEY") or "").strip()
    if not key:
        parser.error("GEMINI_API_KEY is missing; no requests made.")
    return asyncio.run(run(key, output, args.max_requests))


if __name__ == "__main__":
    raise SystemExit(main())
