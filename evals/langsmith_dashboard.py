"""Provision the LangSmith call-quality dashboard and the human listening queue.

Default is a dry run that prints the plan. ``--apply`` creates or updates one
dashboard section (idempotent by title), creates the annotation queue if it is
missing, and renames superseded sections to an ``Archive ·`` prefix so stale
numbers are never mistaken for current ones. Nothing is deleted. Credentials
are read from the ignored ``.env`` (or the environment) and never printed.

Every chart filters on ``voice.call.review`` runs carrying the current review
version, so reviewer status rows, runtime spans and older review contracts
cannot leak into a call average.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.live_eval_evidence import VERSION  # noqa: E402
from app.live_evaluations import REVIEW_QUEUE_RUBRIC  # noqa: E402

ENDPOINTS = {"https://api.smith.langchain.com", "https://eu.api.smith.langchain.com",
             "https://aws.api.smith.langchain.com", "https://apac.api.smith.langchain.com"}
SECTION_TITLE = "ServiceVoice · call quality"
SECTION_DESCRIPTION = ("One row per reviewed phone call. Quality checks score 1 = pass, 0 = fail, blank = uncertain or not "
                       "applicable; needs_review and slow_call are flags (1 = look); metric_* rows are measurements.")
ARCHIVE_PREFIX = "Archive · "
GUIDE = """**How to read this page.** Every number comes from automatic post-call reviews (`voice.call.review` runs, review contract `{version}`). Open a call row to read its *1 Summary*, *2 Scorecard* and *3 Turn-by-turn timeline*; expand the trace for one `turn NN` row per caller turn.

**Quality tiles** average a 1/0 score over calls where the check applied, so 0.9 means nine of ten scored calls passed. Uncertain and not-applicable calls are blank and never count as passes. **Needs a human look** and **Slow calls** are fractions of all reviewed calls. **Delay tiles** are milliseconds: first response is what the caller heard first (usually a waiting cue), answer is when the real reply or tool call started on the server.

**What this cannot show:** audio quality, pronunciation, what the caller actually heard, or consent beyond stored text. Calls flagged for review are queued in **{queue}** for a listen.
"""


def need(condition, code):
    if not condition:
        raise SystemExit(json.dumps({"status": "refused", "reason": code}))


def settings(env_file=None):
    """Read only the LangSmith settings; never interpolate or print secret values."""
    from dotenv import dotenv_values
    raw = dotenv_values(env_file or ROOT / ".env", interpolate=False)
    values = {key: str(value or "") for key, value in raw.items() if key in {"LANGSMITH_API_KEY", "LANGSMITH_ENDPOINT", "LANGSMITH_WORKSPACE_ID"}}
    values.update({key: os.environ[key] for key in ("LANGSMITH_API_KEY", "LANGSMITH_ENDPOINT", "LANGSMITH_WORKSPACE_ID") if key in os.environ})
    key = values.get("LANGSMITH_API_KEY", "").strip()
    endpoint = (values.get("LANGSMITH_ENDPOINT") or "https://api.smith.langchain.com").strip().rstrip("/")
    workspace = values.get("LANGSMITH_WORKSPACE_ID", "").strip()
    need(16 <= len(key) <= 512 and not any(c.isspace() for c in key), "langsmith_key_missing_or_invalid")
    need(endpoint in ENDPOINTS, "unsupported_langsmith_endpoint")
    need(workspace and str(UUID(workspace)) == workspace, "workspace_id_required")
    return key, endpoint, workspace


class Client:
    def __init__(self, key, endpoint, workspace):
        import httpx
        self.endpoint = endpoint
        self.headers = {"x-api-key": key, "x-tenant-id": workspace}
        self.http = httpx.Client(timeout=40, follow_redirects=False, trust_env=False)

    def call(self, method, path, *, body=None, params=None, allowed=(200,)):
        for attempt in range(5):
            response = self.http.request(method, self.endpoint + path, headers=self.headers, json=body, params=params)
            if response.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            need(response.status_code in allowed, f"langsmith_http_{response.status_code}_{method.lower()}_{path.split('?')[0].strip('/').replace('/', '_')[:40]}")
            return response.json() if response.content else None
        raise SystemExit(json.dumps({"status": "refused", "reason": "langsmith_rate_limited"}))


def project_id(client, name):
    rows = client.call("GET", "/api/v1/sessions", params={"name": name, "limit": 2})
    rows = [r for r in rows if isinstance(r, dict) and r.get("name") == name] if isinstance(rows, list) else []
    need(len(rows) == 1, "review_project_not_found_or_ambiguous")
    return str(UUID(rows[0]["id"]))


def run_filter():
    return f'and(eq(name, "voice.call.review"), eq(metadata_key, "review_version"), eq(metadata_value, "{VERSION}"))'


def series(name, project, *, metric=None, feedback_key=None, kind="avg", group_by=None):
    row = {"name": name, "filter_definition": {"source_type": "tracing_project", "run_filter": run_filter(), "project_ids": [project]}}
    if feedback_key:
        row["metric_definition"] = {"type": kind, "field": "feedback_score", "params": {"feedback_key": feedback_key}}
    elif metric == "count":
        row["metric_definition"] = {"type": "count"}
    else:
        row["metric_definition"] = {"type": kind, "field": metric}
    if group_by:
        row["group_by_definitions"] = [group_by]
    return row


def charts(project, queue_name):
    """Chart definitions in reading order: what, how good, how fast, how much."""
    text = GUIDE.format(version=VERSION, queue=queue_name)
    rows = [("text", {"markdown": text})]
    kpi = [
        ("Calls reviewed", "Reviewed calls in the selected period, including uncertain reviews.", series("Calls", project, metric="count")),
        ("Needs a human look — fraction", "needs_review = 1: a conversation, booking or evidence check failed or stayed uncertain.", series("Flagged", project, feedback_key="needs_review")),
        ("Slow calls — fraction", "slow_call = 1: first response or server answer missed its target.", series("Slow", project, feedback_key="slow_call")),
        ("Bookings saved", "Appointments saved across reviewed calls.", series("Bookings", project, feedback_key="metric_bookings", kind="sum")),
        ("Stuck to the facts — pass rate", "hallucination_free over scored calls; blank calls do not count.", series("Facts", project, feedback_key="hallucination_free")),
        ("Booking matches the ask — pass rate", "booking_accuracy over calls with a booking.", series("Booking accuracy", project, feedback_key="booking_accuracy")),
        ("Read back and got a yes — pass rate", "confirmation_compliance over calls that saved a change.", series("Confirmation", project, feedback_key="confirmation_compliance")),
        ("Told the truth about actions — pass rate", "action_truthfulness over scored calls.", series("Truthful", project, feedback_key="action_truthfulness")),
        ("Finished the goal — pass rate", "task_completion over scored calls.", series("Task", project, feedback_key="task_completion")),
        ("Clear conversation — pass rate", "conversation_quality over scored calls (text only, never audio).", series("Clarity", project, feedback_key="conversation_quality")),
        ("Average first response — ms", "Caller end of speech to first assistant audio (Vapi stage sum); with cues this is usually the cue.", series("First response", project, feedback_key="metric_first_response_avg_ms")),
        ("Slowest answer start (p95) — ms", "Average over calls of the per-call p95 server time to the real answer or tool call.", series("Answer p95", project, feedback_key="metric_answer_p95_ms")),
        ("Reviewer coverage — fraction", "coverage_semantic_review = 1 when the Gemini reviewer graded the call.", series("Coverage", project, feedback_key="coverage_semantic_review")),
        ("Provider cost per call — USD", "Average Vapi-reported cost; self-hosted Gemini and speech are not included.", series("Cost", project, feedback_key="metric_cost_usd")),
    ]
    for index, (title, description, row) in enumerate(kpi, start=1):
        rows.append(("kpi", {"title": title, "description": description, "series": [row]}))
    rows.append(("line", {"title": "Response time over time — ms", "description": "Per-call averages: first response (Vapi), real answer p95 (server) and cue start p95 (server).",
                          "series": [series("First response avg", project, feedback_key="metric_first_response_avg_ms"),
                                     series("Answer p95", project, feedback_key="metric_answer_p95_ms"),
                                     series("Cue start p95", project, feedback_key="metric_cue_p95_ms")]}))
    rows.append(("line", {"title": "Where the first response goes — ms", "description": "Vapi stage averages per call: speech recognition, model first token, voice.",
                          "series": [series("Speech recognition", project, feedback_key="metric_transcriber_avg_ms"),
                                     series("Model first token", project, feedback_key="metric_model_avg_ms"),
                                     series("Voice", project, feedback_key="metric_voice_avg_ms")]}))
    rows.append(("bar", {"title": "Waiting cues per call", "description": "Caller turns that received a cue, and follow-up cues during lookups.",
                         "series": [series("Cues played", project, feedback_key="metric_cues_played"),
                                    series("Follow-ups", project, feedback_key="metric_cue_follow_ups")]}))
    rows.append(("bar", {"title": "How calls ended", "description": "Count of reviewed calls by Vapi end reason.",
                         "series": [series("Calls", project, metric="count", group_by={"attribute": "metadata", "path": "ended_reason"})]}))
    rows.append(("bar", {"title": "Caller interruptions and recognition", "description": "Times the caller interrupted the assistant (Vapi), and the lowest speech-recognition confidence per call.",
                         "series": [series("Caller interruptions", project, feedback_key="metric_caller_interruptions"),
                                    series("Lowest ASR confidence", project, feedback_key="metric_asr_confidence_min")]}))
    return rows


def ensure_section(client, apply, sections):
    existing = [s for s in sections if s.get("title") == SECTION_TITLE]
    if existing:
        return str(UUID(existing[0]["id"])), "existing"
    if not apply:
        return None, "would_create"
    created = client.call("POST", "/api/v1/charts/section", body={"title": SECTION_TITLE, "description": SECTION_DESCRIPTION, "index": 0}, allowed=(200, 201))
    return str(UUID(created["id"])), "created"


def existing_charts(client, section_id):
    detail = client.call("POST", "/api/v1/charts/section/" + section_id, body={"omit_data": True, "timezone": "UTC"})
    return [c for c in (detail or {}).get("charts", []) if isinstance(c, dict)]


def archive_old_sections(client, apply, sections):
    renamed = []
    for section in sections:
        title = section.get("title", "")
        if title == SECTION_TITLE or title.startswith(ARCHIVE_PREFIX) or not re.search(r"servicevoice", title, re.I):
            continue
        renamed.append(title)
        if apply:
            client.call("PATCH", "/api/v1/charts/section/" + section["id"], body={"title": ARCHIVE_PREFIX + title, "index": 50})
    return renamed


def ensure_queue(client, apply, project, name):
    rows = client.call("GET", "/api/v1/annotation-queues", params={"name": name, "limit": 5})
    rows = [r for r in rows if isinstance(r, dict) and r.get("name") == name] if isinstance(rows, list) else []
    if rows:
        return str(UUID(rows[0]["id"])), "existing"
    if not apply:
        return None, "would_create"
    body = {"name": name, "description": "Calls the automatic review flagged for a human listen. Open each item, listen in Vapi, answer the rubric.",
            "rubric_instructions": REVIEW_QUEUE_RUBRIC["instructions"], "rubric_items": REVIEW_QUEUE_RUBRIC["items"],
            "num_reviewers_per_item": 1, "enable_reservations": False, "session_ids": [project]}
    created = client.call("POST", "/api/v1/annotation-queues", body=body, allowed=(200, 201))
    return str(UUID(created["id"])), "created"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="servicevoice-call-reviews", help="LangSmith project the reviewer exports to")
    parser.add_argument("--queue", default="ServiceVoice calls to review", help="Annotation queue name (must match SERVICEVOICE_EVAL_REVIEW_QUEUE)")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--apply", action="store_true", help="Create/update the dashboard and queue; default prints the plan only")
    parser.add_argument("--output", type=Path, help="New JSON receipt; existing files are never overwritten")
    args = parser.parse_args(argv)
    need(re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", args.project), "invalid_project_name")
    need(re.fullmatch(r"[A-Za-z0-9 ._-]{1,80}", args.queue), "invalid_queue_name")
    if args.output is not None:
        need(not args.output.exists(), "output_exists")
    client = Client(*settings(args.env_file))
    project = project_id(client, args.project)
    sections = client.call("GET", "/api/v1/charts/section", params={"limit": 50}) or []
    section_id, section_state = ensure_section(client, args.apply, sections)
    planned = charts(project, args.queue)
    current = existing_charts(client, section_id) if section_id else []
    by_title = {c.get("title") or ("text" if c.get("chart_type") == "text" else None): c for c in current}
    created, updated = [], []
    if args.apply:
        for index, (kind, chart) in enumerate(planned):
            if kind == "text":
                match = next((c for c in current if c.get("chart_type") == "text"), None)
                if match:
                    client.call("PATCH", "/api/v1/charts/" + match["id"], body={"markdown": chart["markdown"], "index": index})
                    updated.append("text")
                else:
                    client.call("POST", "/api/v1/charts/create", body={"chart_type": "text", "section_id": section_id, "markdown": chart["markdown"], "index": index}, allowed=(200, 201))
                    created.append("text")
                continue
            body = {"title": chart["title"], "description": chart["description"], "chart_type": kind, "series": chart["series"], "section_id": section_id, "index": index}
            match = by_title.get(chart["title"])
            if match:
                client.call("PATCH", "/api/v1/charts/" + match["id"], body={"description": chart["description"], "chart_type": kind, "series": chart["series"], "index": index})
                updated.append(chart["title"])
            else:
                client.call("POST", "/api/v1/charts/create", body=body, allowed=(200, 201))
                created.append(chart["title"])
        verified = existing_charts(client, section_id)
        need(len(verified) == len(planned), "dashboard_readback_count_mismatch")
    archived = archive_old_sections(client, args.apply, sections)
    queue_id, queue_state = ensure_queue(client, args.apply, project, args.queue)
    receipt = {"schema": "langsmith-dashboard-receipt-v1", "applied": args.apply, "project": args.project, "project_id": project,
               "section": {"title": SECTION_TITLE, "id": section_id, "state": section_state, "charts_planned": len(planned), "created": created, "updated": updated},
               "archived_sections": archived, "queue": {"name": args.queue, "id": queue_id, "state": queue_state, "rubric_items": [i["feedback_key"] for i in REVIEW_QUEUE_RUBRIC["items"]]},
               "review_version": VERSION, "credentials_printed": False}
    if args.output is not None:
        args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
