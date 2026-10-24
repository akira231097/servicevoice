"""Bounded model comparison; synthetic text and disposable local tools only.

No live call, appointment database, routing configuration or deployment changes.
Reports retain synthetic replies for review. Stream timing is provider-side;
first token is not the gateway's validated release or handset audio.
"""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time

import httpx
from dotenv import dotenv_values

from evaluate_gemini import run, render_system_prompt, TOOL_SCHEMAS, URL, MAX_OUTPUT_TOKENS

MODELS = (("gemini-3.8-flash", "low"), ("gemini-3.5-flash-lite", "minimal"))
UTTERANCES = (
    "The steering wheel shakes a little on the highway, but I am parked now. What kind of service should I ask for?",
    "It's a 2021 Honda Civic, and the noise only happens on cold mornings.",
    "I am not sure what the problem is. Can I describe it before we choose a service?",
    "Please check your first available oil change time. Ask me before booking anything.",
)


async def stream_case(client, key, model, effort, utterance):
    row = {"model": model, "reasoning_effort": effort, "utterance": utterance,
           "first_content_ms": None, "first_tool_ms": None, "done_ms": None, "text": "", "passed": False}
    started = time.perf_counter()
    try:
        async with client.stream("POST", URL, headers={"Authorization": "Bearer "+key}, json={
            "model": model, "reasoning_effort": effort, "stream": True, "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": [{"role": "system", "content": render_system_prompt()}, {"role": "user", "content": utterance}],
            "tools": TOOL_SCHEMAS}) as response:
            row["http_status"] = response.status_code
            if response.status_code != 200: return row
            count = 0
            async for line in response.aiter_lines():
                count += len(line)
                if count > 1024*1024: raise ValueError("response bounds")
                if not line.startswith("data: "): continue
                if line == "data: [DONE]":
                    row["done_ms"] = round((time.perf_counter()-started)*1000, 2)
                    break
                event = json.loads(line[6:])
                if event.get("model") != model: raise ValueError("wrong model")
                for choice in event.get("choices", []):
                    delta = choice.get("delta", {})
                    for field, target in (("content", "first_content_ms"), ("tool_calls", "first_tool_ms")):
                        if delta.get(field) and row[target] is None:
                            row[target] = round((time.perf_counter()-started)*1000, 2)
                    row["text"] += delta.get("content") or ""
                    if choice.get("finish_reason") is not None: row["finish_reason"] = choice["finish_reason"]
            row["passed"] = row["done_ms"] is not None and row.get("finish_reason") in {"stop", "tool_calls"}
    except Exception:
        row["error"] = "transport_or_response_contract_failure"
    return row


async def compare(args, report, save):
    values = dotenv_values(args.env_file)
    key = values.get("GEMINI_API_KEY")
    if not key: raise ValueError("missing credential")
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        # Interleave and reverse order on repeat to reduce time-of-run bias.
        for repeat in range(2):
            for utterance in UTTERANCES:
                for model, effort in MODELS[::1 if repeat == 0 else -1]:
                    row = await stream_case(client, key, model, effort, utterance)
                    row["repeat"] = repeat
                    report["streams"].append(row)
                    save()
    for model, effort in MODELS:
        path = args.output.parent / (args.output.stem+"-"+model+"-tools.json")
        with path.open("x", encoding="utf-8") as reserved:
            reserved.write('{"status":"started"}\n')
        result = await run(key, path, 40, model=model, reasoning_effort=effort)
        report["tool_reports"].append({"model": model, "path": str(path), "passed": result == 0})
        save()
    report["metrics"] = {}
    for model, _ in MODELS:
        rows = [r for r in report["streams"] if r["model"] == model]
        report["metrics"][model] = {"passed": sum(r["passed"] for r in rows), "count": len(rows)}
        for field in ("first_content_ms", "first_tool_ms", "done_ms"):
            numbers = [r[field] for r in rows if r[field] is not None]
            report["metrics"][model][field] = {"n": len(numbers), "median": statistics.median(numbers) if numbers else None}
    report["status"] = "completed"
    save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = {"status": "started", "streams": [], "tool_reports": [],
              "limitations": ["Small sample; provider stream times are not safe-to-speak or phone audio times.",
                              "Tool scenarios use disposable local scheduling databases; no production writes.",
                              "Quality checks are deterministic state assertions, not a human listening evaluation."]}
    with args.output.open("x", encoding="utf-8") as output:
        def save():
            output.seek(0); json.dump(report, output, indent=2); output.write("\n"); output.truncate(); output.flush()
        save()
        try: asyncio.run(compare(args, report, save))
        except BaseException:
            report["status"] = "incomplete"; save()
            raise
    print(json.dumps({"status": report["status"], "metrics": report.get("metrics"), "tool_reports": report["tool_reports"]}))


if __name__ == "__main__": main()
