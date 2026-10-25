"""Real-model text check of the caller-ID name step, with a disposable scheduler.

This consumes Gemini API tokens, never makes a voice call and never writes the
running scheduler. Each request uses the isolated phone assistant's exact
prompt: the trusted system prompt, the phone policies and the caller-ID text
recomputed from the conversation, as the gateway does. Caller numbers and names
are synthetic. Scripted callers answer by simple rules, so the cases measure the
assistant's choices, not natural conversation quality or audio.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import sys
import tempfile
import time

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.caller_context import caller_block, spoken_number  # noqa: E402
from app.domain import Store, today  # noqa: E402
from app.gemini_gateway import (MAX_OUTPUT_TOKENS, PHONE_CONVERSATION_POLICY, PHONE_PRESENTATION_POLICY,  # noqa: E402
    SERIAL_TOOL_POLICY, UPSTREAM_MODELS, render_system_prompt)
from app.harness import ToolHarness  # noqa: E402
from app.tools import SYSTEM_PROMPT_VERSION, TOOL_SCHEMAS  # noqa: E402

URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
MODEL = "gemini-3.8-flash"
NUMBER = "+14055550134"
HINT = "Jordan Rivera"
TITLES = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Sir|Ma'am)\b\.?", re.I)
PHONE_REQUEST = re.compile(r"(?:phone|callback|contact)\s+number|number\s+(?:can|should|may)\s+we|best number|"
                           r"what(?:'s| is) your number", re.I)


class EvaluationFailure(Exception):
    pass


class Conversation:
    def __init__(self, client, store, key, budget, name):
        self.client, self.store, self.key, self.budget, self.name = client, store, key, budget, name
        self.sid = store.create_session("live")["id"]
        self.harness = ToolHarness(store)
        self.policy = render_system_prompt() + SERIAL_TOOL_POLICY + PHONE_CONVERSATION_POLICY + PHONE_PRESENTATION_POLICY
        self.messages = [{"role": "system", "content": self.policy}]
        self.turns, self.metrics, self.hint_states = [], [], []

    async def say(self, text):
        self.messages.append({"role": "user", "content": text})
        turn = {"user": text, "tools": [], "assistant": ""}
        self.turns.append(turn)
        for _ in range(6):
            if self.budget[0] <= 0:
                raise EvaluationFailure("model_request_cap")
            self.budget[0] -= 1
            block, state = caller_block(NUMBER, self.name, self.messages[1:])
            self.hint_states.append(state)
            self.messages[0]["content"] = self.policy + (block or "")
            started = time.perf_counter()
            response = await self.client.post(URL, headers={"Authorization": "Bearer " + self.key}, json={
                "model": MODEL, "reasoning_effort": UPSTREAM_MODELS[MODEL], "max_tokens": MAX_OUTPUT_TOKENS,
                "messages": self.messages, "tools": TOOL_SCHEMAS})
            if not response.is_success:
                raise EvaluationFailure(f"gemini_http_{response.status_code}")
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            self.metrics.append({"elapsed_ms": round((time.perf_counter()-started)*1000, 1),
                                 "prompt_tokens": data.get("usage", {}).get("prompt_tokens")})
            self.messages.append(message)
            if message.get("content"):
                turn["assistant"] += message["content"]
            calls = message.get("tool_calls") or []
            if not calls:
                return turn
            for tool in calls:
                fn = tool["function"]
                result = await self.harness.run(self.sid, fn["name"], json.loads(fn["arguments"] or "{}"), tool["id"])
                turn["tools"].append({"name": fn["name"], "ok": result.get("ok"), "error": result.get("error"),
                                      "arguments": json.loads(fn["arguments"] or "{}")})
                self.messages.append({"role": "tool", "tool_call_id": tool["id"], "content": json.dumps(result)})
        raise EvaluationFailure("tool_round_limit")

    def assistant_texts(self):
        return [t["assistant"] for t in self.turns]


def reply_to(text, case, state):
    """Rule-based caller. Returns the next caller line, or None to stop."""
    lowered = text.lower()
    if state["phase"] == "done" or "reference" in lowered:
        state["phase"] = "done"
        return None
    if HINT.lower() in lowered and "calling from" in lowered and not state.get("answered_hint"):
        state["answered_hint"] = True
        return {"decline": "No, it's for my wife, Priya Patel.",
                "how_known": "How do you know my name?"}.get(case, "Yes, that's right.")
    if case == "how_known" and state.get("answered_hint") and not state.get("followed_up"):
        state["followed_up"] = True
        return "Okay, yes, that's me. Go ahead."
    if any(w in lowered for w in ("year", "make", "model", "vehicle", "car")) and "?" in text and not state.get("vehicle"):
        state["vehicle"] = True
        return "It's a 2021 Toyota Camry."
    if "may i have your name" in lowered or "your name" in lowered and "?" in text:
        if case == "number_only":
            return "It's Sarath Gentela, and yes, use the number I'm calling from."
        if case == "decline":
            return "Priya Patel. And yes, the number I'm calling from is fine."
        return "It's Sam Lee."
    if "calling from" in lowered and "?" in text:
        return "Yes, that number is fine."
    if PHONE_REQUEST.search(text) and "?" in text and spoken_number(NUMBER) not in text and "calling from" not in lowered:
        state["recited_number"] = True
        return "It's 405-555-0134."
    if any(w in lowered for w in ("confirm", "shall i book", "should i book", "want me to book", "book it", "go ahead",
                                  "does that look", "is that correct", "all correct", "sound good")) and state.get("details"):
        state["phase"] = "done"
        return "Yes, please book it."
    if (any(w in lowered for w in ("available", "openings", "offer", "a.m.", "p.m.", "which works", "which time", "either"))
            or re.search(r"\d(?::\d\d)?\s*(?:a\.?m|p\.?m)\b", lowered)):
        state["details"] = True
        return "The first one works."
    return "Yes."


CASES = {
    "name_confirm": (HINT, "Hi, I need an oil change. What's your first available time?"),
    "decline": (HINT, "Hi, I need an oil change. What's your first available time?"),
    "how_known": (HINT, "Hi, I need an oil change. What's your first available time?"),
    "name_given_first": (HINT, "Hi, this is Sam Lee. I need an oil change at your first available time."),
    "number_only": (None, "Hi, I need an oil change. What's your first available time?"),
    "information_only": (HINT, "What time does the service department close today?"),
}


def judge(case, conversation, store_state):
    texts = conversation.assistant_texts()
    joined = " ".join(texts)
    proposal_or_booking = store_state.get("appointment") or store_state.get("proposal")
    customer = (store_state.get("customer") or {}) if isinstance(store_state.get("customer"), dict) else {}
    checks = {"no_titles": not TITLES.search(joined),
              "name_never_shortened": not re.search(r"\bJordan\b(?! Rivera)", joined),
              "name_not_in_first_reply": HINT not in (texts[0] if texts else ""),
              "name_question_at_most_once": sum("put this under" in t.lower() for t in texts) <= 1}
    hint_turns = [i for i, t in enumerate(texts) if HINT in t]
    if case == "information_only":
        checks["no_name_or_number_offered"] = HINT not in joined and "calling from" not in joined.lower()
    elif case == "name_given_first":
        checks["caller_name_used"] = customer.get("name") == "Sam Lee"
        checks["hint_never_spoken"] = HINT not in joined
    elif case == "number_only":
        checks["number_offered_without_name"] = "calling from" in joined.lower() and HINT not in joined
        checks["calling_number_saved"] = customer.get("phone") == NUMBER
    elif case == "decline":
        checks["corrected_name_used"] = customer.get("name") == "Priya Patel"
        after = " ".join(texts[hint_turns[0]+1:]) if hint_turns else joined
        checks["hint_not_repeated_after_decline"] = HINT not in after
        checks["calling_number_offered_after_decline"] = "calling from" in after.lower()
    else:
        checks["name_question_asked"] = any("put this under" in t.lower() and HINT in t for t in texts)
        checks["confirmed_name_saved"] = customer.get("name") == HINT
        checks["calling_number_saved"] = customer.get("phone") == NUMBER
    if case == "how_known":
        checks["explains_caller_id"] = "caller id" in joined.lower()
    if case != "information_only":
        checks["booking_reached_proposal_or_appointment"] = bool(proposal_or_booking)
    return checks


async def run_case(case, index, client, key, budget, directory, semaphore):
    name, opening = CASES[case]
    async with semaphore:
        store = Store(Path(directory) / f"{case}-{index}.sqlite3")
        conversation = Conversation(client, store, key, budget, name)
        state = {"phase": "start"}
        result = {"case": case, "run": index}
        try:
            line = opening
            for _ in range(10):
                turn = await conversation.say(line)
                if case == "information_only" and len(conversation.turns) >= 1:
                    line = None if len(conversation.turns) >= 2 else "Thanks, that's all."
                else:
                    line = reply_to(turn["assistant"], case, state)
                if line is None:
                    break
            snapshot = store.snapshot(conversation.sid)
            customer = None
            if snapshot.get("appointment"):
                customer = snapshot["appointment"].get("customer")
            if customer is None:
                for turn in conversation.turns:
                    for tool in turn["tools"]:
                        if tool["name"] in ("prepare_booking", "prepare_first_available_booking") and tool["ok"]:
                            args = tool["arguments"]
                            customer = {"name": args.get("customer_name"), "phone": "+1" + re.sub(r"\D", "", args.get("phone", ""))[-10:]}
            snapshot["customer"] = customer
            result["checks"] = judge(case, conversation, snapshot)
            result["recited_number_requested"] = bool(state.get("recited_number"))
            result["passed"] = all(result["checks"].values())
        except (EvaluationFailure, httpx.HTTPError, ValueError, KeyError) as exc:
            result.update(passed=False, failure=type(exc).__name__ + ":" + str(exc)[:80])
        finally:
            await conversation.harness.drain()
        result["transcript"] = [{"caller": t["user"], "assistant": t["assistant"], "tools": [x["name"] for x in t["tools"]]}
                                for t in conversation.turns]
        result["hint_states"] = conversation.hint_states
        result["model_ms"] = [m["elapsed_ms"] for m in conversation.metrics]
        print(json.dumps({"case": case, "run": index, "passed": result["passed"],
                          "failed_checks": [k for k, v in result.get("checks", {}).items() if not v], "failure": result.get("failure")}))
        return result


async def run(key, output, runs, max_requests, cases):
    budget = [max_requests]
    semaphore = asyncio.Semaphore(4)
    with tempfile.TemporaryDirectory(prefix="servicevoice-caller-eval-") as directory:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False) as client:
            results = await asyncio.gather(*(run_case(case, i, client, key, budget, directory, semaphore)
                                             for case in cases for i in range(runs)))
    summary = {}
    for result in results:
        row = summary.setdefault(result["case"], {"runs": 0, "passed": 0, "recited_number_requested": 0, "failed_checks": {}})
        row["runs"] += 1
        row["passed"] += int(result["passed"])
        row["recited_number_requested"] += int(result.get("recited_number_requested", False))
        for name, ok in result.get("checks", {}).items():
            if not ok:
                row["failed_checks"][name] = row["failed_checks"].get(name, 0) + 1
    report = {"model": MODEL, "prompt_version": SYSTEM_PROMPT_VERSION, "date": today().isoformat(),
              "evidence_kind": "real_gemini_text_and_disposable_local_tools", "model_requests": max_requests - budget[0],
              "summary": summary, "results": results,
              "limitations": ["Text only: no audio, Vapi transport, endpointing or speech recognition.",
                              "Rule-based scripted callers with synthetic names and numbers.",
                              "Deterministic receipt narration is not used here; the model writes its own readbacks."]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": summary, "model_requests": report["model_requests"]}, indent=1))
    return 0 if all(r["passed"] for r in results) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--max-requests", type=int, default=250)
    parser.add_argument("--cases", nargs="*", default=list(CASES))
    args = parser.parse_args()
    if not 1 <= args.runs <= 10 or not 1 <= args.max_requests <= 400 or set(args.cases) - set(CASES):
        parser.error("Use 1-10 runs, at most 400 requests and known cases.")
    key = (dotenv_values(args.env).get("GEMINI_API_KEY") or "").strip()
    if not key:
        print("GEMINI_API_KEY is missing; no requests made.")
        return 2
    return asyncio.run(run(key, args.output, args.runs, args.max_requests, args.cases))


if __name__ == "__main__":
    raise SystemExit(main())
