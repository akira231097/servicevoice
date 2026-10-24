"""Bounded real-model text/tool acceptance exercise using a disposable scheduler.

This consumes Gemini API tokens, never makes a voice call, and never writes the
running scheduler. Only synthetic conversation text and allowlisted metrics are
saved. Gemini's returned assistant messages/signatures stay intact in memory.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.domain import Store, today
from app.harness import ToolHarness
from app.gemini_gateway import render_system_prompt, MAX_OUTPUT_TOKENS
from app.tools import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION, TOOL_SCHEMAS

URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
MODEL = "gemini-3.8-flash"


class EvaluationFailure(Exception):
    pass


class Conversation:
    def __init__(self, client, store, key, budget, *, model=MODEL, reasoning_effort="low"):
        self.client, self.store, self.key, self.budget = client, store, key, budget
        self.model, self.reasoning_effort = model, reasoning_effort
        self.sid = store.create_session("live")["id"]
        self.harness = ToolHarness(store)
        prompt = render_system_prompt()
        block_start, block_end = "<trusted_dealership_facts>", "</trusted_dealership_facts>"
        supplied = block_start in prompt and block_end in prompt
        facts = json.loads(prompt.split(block_start, 1)[1].split(block_end, 1)[0]) if supplied else None
        if supplied and not isinstance(facts, dict):
            raise ValueError("Server facts must be a JSON object.")
        self.prompt_metadata = {
            "mode": "server_owned_static_facts" if supplied else "tool_fallback",
            "trusted_facts_supplied": supplied,
            "rendered_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "source": "app.gemini_gateway.render_system_prompt",
        }
        self.messages = [{"role": "system", "content": prompt}]
        self.turns, self.metrics = [], []

    async def say(self, text):
        self.messages.append({"role": "user", "content": text})
        turn = {"user": text, "tools": [], "assistant": ""}
        self.turns.append(turn)
        for _ in range(8):
            if self.budget[0] <= 0:
                raise EvaluationFailure("Global model-request cap reached.")
            self.budget[0] -= 1
            started = time.perf_counter()
            response = await self.client.post(URL, headers={"Authorization": "Bearer " + self.key}, json={
                "model": self.model, "reasoning_effort": self.reasoning_effort, "max_tokens": MAX_OUTPUT_TOKENS,
                "messages": self.messages, "tools": TOOL_SCHEMAS,
            })
            if not response.is_success:
                raise EvaluationFailure(f"Gemini HTTP {response.status_code}; response withheld.")
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            metric = {"elapsed_ms": round((time.perf_counter()-started)*1000, 2),
                      "max_output_tokens": MAX_OUTPUT_TOKENS,
                      "model_returned": data.get("model"), "finish_reason": choice.get("finish_reason"),
                      "usage": {k: data.get("usage", {}).get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}}
            self.metrics.append(metric)
            if data.get("model") != self.model:
                raise EvaluationFailure("Returned model did not match exact requested identity.")
            if choice.get("finish_reason") == "length":
                raise EvaluationFailure("Model response exhausted its output budget.")
            self.messages.append(message)  # Preserve every tool thought signature exactly.
            if message.get("content"):
                turn["assistant"] += message["content"]
            calls = message.get("tool_calls") or []
            if not calls:
                return turn
            if len(calls) > 8:
                raise EvaluationFailure("Tool fan-out exceeded bounded acceptance policy.")
            for tool in calls:
                fn = tool["function"]
                arguments = json.loads(fn["arguments"])
                result = await self.harness.run(self.sid, fn["name"], arguments, tool["id"])
                turn["tools"].append({"name": fn["name"], "ok": result.get("ok"), "error": result.get("error")})
                self.messages.append({"role": "tool", "tool_call_id": tool["id"], "content": json.dumps(result)})
        raise EvaluationFailure("Tool-round limit reached before a spoken response.")

    def snapshot(self):
        return self.store.snapshot(self.sid)

    async def prepare(self):
        await self.say("I need an oil change at your first available time. My name is Alex Morgan, phone 5125550142, vehicle 2021 Toyota Camry. Please check the time and ask before actually booking.")
        for _ in range(3):
            snapshot = self.snapshot()
            if snapshot["appointment"]:
                raise EvaluationFailure("Appointment existed before caller consent.")
            if snapshot["proposal"]:
                return snapshot["proposal"]["id"]
            await self.say("The first available time you found works. Use Alex Morgan, 5125550142, 2021 Toyota Camry. Please read it back before booking.")
        raise EvaluationFailure("No proposal was prepared within the clarification limit.")


async def run(key, output, max_requests, *, model=MODEL, reasoning_effort="low"):
    budget = [max_requests]
    report = {"model_requested": model, "reasoning_effort": reasoning_effort, "prompt_version": SYSTEM_PROMPT_VERSION,
              "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(), "date": today().isoformat(),
              "evidence_kind": "real_gemini_text_and_disposable_local_tools", "cases": [],
              "limitations": ["No audio, Vapi transport, acoustic latency or production DMS is exercised.",
                              "Small deterministic caller scripts; semantic quality still needs human review.",
                              "Reported time is complete HTTP model-request duration, not time to first audible response."]}
    with tempfile.TemporaryDirectory(prefix="servicevoice-gemini-eval-") as directory:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for scenario in ("book_move_cancel", "correction_then_decline", "human_request"):
                store = Store(Path(directory) / (scenario + ".sqlite3"))
                conversation = Conversation(client, store, key, budget, model=model, reasoning_effort=reasoning_effort)
                case = {"scenario": scenario, "passed": False}
                case["prompt_context"] = conversation.prompt_metadata
                report["cases"].append(case)
                try:
                    if scenario == "human_request":
                        await conversation.say("I want to speak to a human advisor, please.")
                        state = conversation.snapshot()
                        assert state["handoff"] and not state["appointment"], "No local handoff or unexpected appointment."
                    else:
                        old_proposal = await conversation.prepare()
                        if scenario == "correction_then_decline":
                            await conversation.say("Wait, the vehicle is a 2022 Toyota Camry. Correct that and read it back. Do not book yet.")
                            state = conversation.snapshot()
                            assert not state["appointment"], "Correction created an appointment without assent."
                            assert state["proposal"] and state["proposal"]["id"] != old_proposal, "Correction did not create a fresh proposal."
                            assert "2022 Toyota Camry" in state["proposal"]["summary"], "Wrong vehicle in corrected proposal."
                            await conversation.say("No, do not book it. I changed my mind.")
                            assert not conversation.snapshot()["appointment"], "Declined proposal was booked."
                            assert not conversation.snapshot()["proposal"], "Declined proposal remained actionable."
                        else:
                            await conversation.say("Yes, book that exact appointment, please.")
                            original = conversation.snapshot()["appointment"]
                            assert original and original["status"] == "confirmed", "Explicit confirmation did not book."
                            await conversation.say("Please move that appointment to your next available different time. Check the new time and ask me before changing it.")
                            for _ in range(2):
                                state = conversation.snapshot()
                                assert state["appointment"] == original, "Reschedule happened before new assent."
                                if state["proposal"]: break
                                await conversation.say("The first different available time works. Please read back the change.")
                            assert conversation.snapshot()["proposal"], "No reschedule proposal."
                            await conversation.say("Yes, move it to that new time.")
                            moved = conversation.snapshot()["appointment"]
                            assert moved["id"] == original["id"] and moved["slot_id"] != original["slot_id"], "Reschedule failed or changed identity."
                            await conversation.say("Actually, I need to cancel this appointment. Please read back what will be cancelled before doing it.")
                            assert conversation.snapshot()["appointment"]["status"] == "confirmed", "Cancelled without subsequent assent."
                            assert conversation.snapshot()["proposal"], "No cancellation proposal."
                            await conversation.say("Yes, cancel that appointment.")
                            assert conversation.snapshot()["appointment"]["status"] == "cancelled", "Cancellation did not commit."
                    case["passed"] = True
                except (EvaluationFailure, AssertionError) as exc:
                    case["failure"] = str(exc)
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    case["failure"] = "Transport or response-contract failure; raw error withheld."
                finally:
                    await conversation.harness.drain()
                    case["turns"] = conversation.turns
                    case["model_requests"] = conversation.metrics
                    print(json.dumps({"scenario": scenario, "passed": case["passed"], "failure": case.get("failure")}))
    report["model_request_count"] = max_requests - budget[0]
    report["passed"] = all(case["passed"] for case in report["cases"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-requests", type=int, default=40)
    parser.add_argument("--output", type=Path, default=ROOT/"docs/gemini-agent-evaluation.json")
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 60: parser.error("Request cap must be 1 through 60.")
    key = (dotenv_values(ROOT/".env").get("GEMINI_API_KEY") or "").strip()
    if not key:
        print("GEMINI_API_KEY is missing; no requests made."); return 2
    return asyncio.run(run(key, args.output, args.max_requests))


if __name__ == "__main__":
    raise SystemExit(main())
