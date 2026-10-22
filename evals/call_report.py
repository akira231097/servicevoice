"""Render one automatic call review as a self-contained HTML page.

Input is either a saved review JSON (``--report``) or the latest, or a named,
``voice.call.review`` run read from LangSmith (``--latest`` / ``--run-id``).
The page shows exactly what the reviewer exported: the headline, scorecard,
turn-by-turn timeline, latency, waiting cues, booking and limits. It never
contains transcripts or customer details because the review does not either.
Credentials come from the ignored ``.env`` and are never printed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.live_eval_evidence import VERSION  # noqa: E402

STYLE = """
:root{color-scheme:light;--ink:#15212b;--muted:#5b6b78;--line:#d9e2e8;--pass:#0a7a4f;--fail:#b3261e;--unsure:#9a6400;--na:#6b7a86;--wash:#f3f7f9;--accent:#0b6b74}
*{box-sizing:border-box}body{margin:0;background:#fff;color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1040px;margin:auto;padding:36px 24px 60px}h1{font-size:28px;line-height:1.2;margin:0 0 6px}h2{font-size:20px;margin:30px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
.headline{font-size:18px;color:var(--muted);margin:0 0 18px}.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:13px;font-weight:650;margin-right:6px}
.PASS{background:#e3f4ec;color:var(--pass)}.FAIL{background:#fbe5e3;color:var(--fail)}.UNCERTAIN{background:#fff1d6;color:var(--unsure)}.NA{background:#eef1f3;color:var(--na)}
table{border-collapse:collapse;width:100%;font-size:15px}th,td{text-align:left;vertical-align:top;padding:8px 10px;border-bottom:1px solid var(--line)}th{background:var(--wash)}
ol.turns li{margin:8px 0}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:12px 0}
.tile{background:var(--wash);border-radius:6px;padding:12px 14px}.tile b{display:block;font-size:22px}.tile span{font-size:13px;color:var(--muted)}
ul{padding-left:22px}li{margin:4px 0}footer{margin-top:36px;font-size:13px;color:var(--muted)}code{background:#eef2f5;padding:1px 4px;border-radius:3px}
"""


def need(condition, code):
    if not condition:
        raise SystemExit(json.dumps({"status": "refused", "reason": code}))


def esc(value):
    return html.escape("" if value is None else str(value))


def ms(value):
    return "?" if not isinstance(value, (int, float)) else f"{value / 1000:.2f} s"


def fetch(run_id, latest, project, env_file):
    from evals.langsmith_dashboard import Client, settings, project_id
    client = Client(*settings(env_file))
    if latest:
        pid = project_id(client, project)
        rows = client.call("POST", "/api/v1/runs/query", body={"session": [pid], "is_root": True, "limit": 1, "order": "desc",
                           "filter": f'and(eq(name, "voice.call.review"), eq(metadata_key, "review_version"), eq(metadata_value, "{VERSION}"))',
                           "select": ["id"]})
        runs = rows.get("runs", []) if isinstance(rows, dict) else []
        need(runs, "no_call_review_found")
        run_id = str(UUID(runs[0]["id"]))
    run = client.call("GET", "/api/v1/runs/" + run_id)
    need(isinstance(run, dict) and run.get("name") == "voice.call.review", "not_a_call_review_run")
    return run.get("outputs") or {}, run_id


def render(report, *, run_id=None):
    summary = report.get("1 Summary") or {}
    scorecard = report.get("2 Scorecard") or {}
    timeline = report.get("3 Turn-by-turn timeline") or {}
    latency = report.get("4 Latency") or {}
    cues = report.get("5 Waiting cues") or {}
    booking = report.get("6 Booking and actions") or {}
    howto = report.get("7 How to read this review") or {}
    call = report.get("call_summary") or {}
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
             f"<title>Call review</title><style>{STYLE}</style></head><body><main>",
             f"<p class='muted'>ServiceVoice · automatic call review · {esc(report.get('review_version'))}</p>",
             "<h1>Call review</h1>", f"<p class='headline'>{esc(summary.get('headline'))}</p>", "<ul>"]
    parts += [f"<li>{esc(line)}</li>" for line in summary.get("lines", [])]
    parts.append("</ul><h2>What to check</h2><ul>")
    parts += [f"<li>{esc(item)}</li>" for item in summary.get("what_to_check", [])]
    parts.append("</ul><h2>Scorecard</h2><table><thead><tr><th>Check</th><th>Result</th><th>Why</th><th>Evidence</th></tr></thead><tbody>")
    for group in scorecard.get("groups", []):
        parts.append(f"<tr><th colspan='4'>{esc(group.get('group'))}</th></tr>")
        for row in group.get("checks", []):
            result = row.get("result", "UNCERTAIN")
            parts.append(f"<tr><td>{esc(row.get('question'))}</td><td><span class='badge {esc(result.replace('/', ''))}'>{esc(result)}</span></td>"
                         f"<td>{esc(row.get('why'))}</td><td class='muted'>{esc(', '.join(row.get('evidence', [])))}</td></tr>")
    parts.append("</tbody></table>")
    parts.append(f"<p class='muted'>{esc(scorecard.get('legend'))}</p>")
    parts.append("<h2>Turn by turn</h2><ol class='turns'>")
    parts += [f"<li>{esc(line)}</li>" for line in timeline.get("turns", [])] or ["<li class='muted'>No caller turns were observed.</li>"]
    parts.append("</ol>")
    synthesis = timeline.get("speech_synthesis") or {}
    if synthesis.get("requests"):
        parts.append(f"<p class='muted'>Speech synthesis: {esc(synthesis.get('requests'))} requests, {esc(synthesis.get('cached_cue_clips'))} from the cue cache, "
                     f"first audio p95 {esc(ms(synthesis.get('first_audio_p95_ms')))}, {esc(synthesis.get('failed'))} failed.</p>")
    basis = timeline.get("join_basis") or {}
    parts.append(f"<p class='muted'>Join basis: artifact rows attached by ordinal = {esc(basis.get('artifact_rows_attached_by_ordinal'))}; "
                 f"Vapi per-turn latencies attached = {esc(basis.get('vapi_turn_latencies_attached_by_ordinal'))}. {esc(basis.get('note'))}</p>")
    parts.append("<h2>Latency</h2><div class='grid'>")
    for label, key in (("First response, average", "first_response_avg_ms"), ("First response, p95", "first_response_p95_ms"),
                       ("Answer start, average (server)", "answer_avg_ms"), ("Answer start, p95 (server)", "answer_p95_ms"), ("Cue start, p95 (server)", "cue_start_p95_ms")):
        parts.append(f"<div class='tile'><b>{esc(ms(latency.get(key)))}</b><span>{esc(label)}</span></div>")
    parts.append("</div><ul>")
    parts += [f"<li>{esc(line)}</li>" for line in latency.get("summary", [])]
    parts.append("</ul>")
    targets = latency.get("targets_ms") or {}
    if targets:
        parts.append(f"<p class='muted'>Targets: first response average {esc(ms(targets.get('turn_average')))}, p95 {esc(ms(targets.get('turn_p95')))}; server answer p95 {esc(ms(targets.get('server_answer_p95')))}.</p>")
    parts.append("<h2>Waiting cues</h2><div class='grid'>")
    parts.append(f"<div class='tile'><b>{esc(cues.get('turns_with_a_cue'))} / {esc(cues.get('caller_turns'))}</b><span>caller turns with a cue</span></div>")
    parts.append(f"<div class='tile'><b>{esc(cues.get('follow_up_cues'))}</b><span>follow-up cues during lookups</span></div>")
    parts.append(f"<div class='tile'><b>{esc(ms(cues.get('cue_start_p95_ms')))}</b><span>cue start, p95 (server)</span></div></div>")
    if cues.get("phrases_used"):
        parts.append("<p>Phrases: " + ", ".join(f"<code>{esc(text)}</code> ×{esc(n)}" for text, n in cues["phrases_used"].items()) + "</p>")
    if cues.get("turns_without_a_cue_by_reason"):
        parts.append("<p>Silent turns: " + ", ".join(f"{esc(reason)} ×{esc(n)}" for reason, n in cues["turns_without_a_cue_by_reason"].items()) + "</p>")
    parts.append(f"<p class='muted'>{esc(cues.get('meaning'))}</p>")
    parts.append("<h2>Booking and actions</h2>")
    parts.append(f"<p>Appointments saved: <b>{esc(booking.get('appointment_count'))}</b> · committed actions: {esc(booking.get('committed_actions'))} · unresolved operations: {esc(booking.get('unresolved_operations'))}</p>")
    tools = booking.get("tools") or []
    if tools:
        parts.append("<table><thead><tr><th>Turn</th><th>Tool</th><th>Result</th><th>Duration</th><th>Receipt</th></tr></thead><tbody>")
        for tool in tools:
            parts.append(f"<tr><td>{esc(tool.get('turn') or '?')}</td><td>{esc(tool.get('name'))}</td><td>{'ok' if tool.get('ok') else esc(tool.get('error') or 'unknown')}</td>"
                         f"<td>{esc(ms(tool.get('duration_ms')) if tool.get('duration_ms') is not None else '')}</td><td>{'saved' if tool.get('receipt_present') else ''}</td></tr>")
        parts.append("</tbody></table>")
    cost = booking.get("provider_cost") or {}
    if cost.get("total_usd") is not None:
        parts.append(f"<p class='muted'>Provider cost ${cost['total_usd']:.4f} (platform ${cost.get('platform_usd', 0):.4f}, speech recognition ${cost.get('speech_recognition_usd', 0):.4f}).</p>")
    parts.append("<h2>What this review cannot prove</h2><ul>")
    parts += [f"<li>{esc(item)}</li>" for item in howto.get("what_this_cannot_prove", [])]
    parts.append("</ul>")
    parts.append(f"<footer>Channel {esc(call.get('channel'))} · ended {esc(call.get('ended_reason'))} · {esc(call.get('caller_utterances'))} caller / {esc(call.get('assistant_utterances'))} assistant utterances"
                 + (f" · LangSmith run <code>{esc(run_id)}</code>" if run_id else "") + f" · rendered {esc(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}. "
                 "No transcript or customer detail is included; the review never hears audio.</footer></main></body></html>")
    return "\n".join(parts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--report", type=Path, help="A saved review JSON (the run's outputs)")
    source.add_argument("--run-id", help="A voice.call.review run id in LangSmith")
    source.add_argument("--latest", action="store_true", help="The newest voice.call.review run in the project")
    parser.add_argument("--project", default="servicevoice-call-reviews")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New HTML file; existing files are never overwritten")
    args = parser.parse_args(argv)
    need(not args.output.exists(), "output_exists")
    run_id = None
    if args.report:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        report = report.get("outputs", report) if isinstance(report, dict) else {}
    else:
        if args.run_id:
            need(str(UUID(args.run_id)) == args.run_id, "invalid_run_id")
        report, run_id = fetch(args.run_id, args.latest, args.project, args.env_file)
    need(isinstance(report, dict) and "1 Summary" in report, "report_has_no_readable_sections")
    args.output.write_text(render(report, run_id=run_id), encoding="utf-8")
    print(json.dumps({"status": "rendered", "output": str(args.output), "run_id": run_id, "review_version": report.get("review_version")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
