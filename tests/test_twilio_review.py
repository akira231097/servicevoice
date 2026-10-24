from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import httpx

from app.call_review_presentation import listening_summary, feedback_rows
from app.diagnostics import safe_attributes
from test_live_evaluations import scenario, worker_fixture


def test_reviewer_discovers_both_assistants_and_advances_one_watermark(scenario, tmp_path):
    worker, mock, _ = worker_fixture(tmp_path, scenario)
    first, second = str(uuid4()), str(uuid4())
    worker.config = replace(worker.config, assistant_id=first+","+second)
    calls = []
    for assistant in (first, second):
        call = deepcopy(mock.call)
        call.update(id=str(uuid4()), assistantId=assistant)
        calls.append(call)
    queries = []
    def handler(request):
        assistant = request.url.params["assistantId"]
        queries.append(assistant)
        return httpx.Response(200, json=[call for call in calls if call["assistantId"] == assistant])
    worker.client = httpx.Client(transport=httpx.MockTransport(handler))
    worker.discover_provider()
    assert set(queries) == {first, second}
    assert worker.outbox.status()["jobs"] == {"pending": 2}


def test_multi_assistant_reviewer_accepts_each_call(scenario, tmp_path):
    worker, mock, _ = worker_fixture(tmp_path, scenario)
    assistant = str(uuid4())
    worker.config = replace(worker.config, assistant_id=str(uuid4())+","+assistant)
    mock.call["assistantId"] = assistant
    def handler(request):
        if request.url.host == "api.vapi.ai" and request.url.path == "/call":
            return httpx.Response(200, json=[])
        return mock(request)
    worker.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert worker.tick()
    assert worker.outbox.status()["jobs"] == {"complete": 1}


def test_listening_diagnostics_and_metrics_never_claim_audio_pass():
    key = "a"*64
    rows = [{"component": "listening_cue", "event": name, "call_key": key,
             "start_unix": 1_700_000_000+index, "attributes": {"listening_reason":"accepted_unverified"}}
            for index, name in enumerate(["decision", "dispatched", "clip_fetched", "announcement_end"])]
    data = listening_summary({"events": rows}, key)
    assert data["dispatched"] == 1 and data["clip_fetches"] == 1
    assert data["audible_playback"] == "unverified"
    report = {"listening_cues": data, "checks": {"listening_cues_safe":{"verdict":"uncertain"}}}
    feedback = {row["key"]:row for row in feedback_rows(str(uuid4()), report)}
    assert feedback["metric_listening_cues"]["score"] == 1
    assert feedback["listening_cues_safe"]["score"] is None
    assert safe_attributes({"listening_reason":"approved", "transcript":"private caller words", "credential":"private"}) == {"listening_reason":"approved"}
    assert listening_summary({"events": rows}, "b"*64) is None


def test_review_retains_real_phrase_and_media_health_measurements():
    key="a"*64;start=1_700_000_000
    def event(component,name,second,**attrs):
        return {"component":component,"event":name,"call_key":key,"start_unix":start+second,"attributes":attrs}
    rows=[event("twilio_bridge","speaker_started",0,listening_reason="caller"),
        event("twilio_bridge","speaker_stopped",10,listening_reason="caller"),
        event("listening_cue","clip_fetched",4,backchannel_phrase="uh_huh_1"),
        event("twilio_bridge","media_suspected",12,listening_reason="caller_energy_without_words")]
    result=listening_summary({"events":rows},key)
    assert result["timeline"][0]["phrase"]=="Uh-huh."
    assert result["caller_energy_seconds"]==10 and result["clip_fetches_per_10s_caller_energy"]==1
    assert result["silent_opening_detections"]==1 and result["audible_playback"]=="unverified"


def test_intended_tool_followup_is_not_a_duplicate_but_extra_cues_are():
    key='a'*64
    rows=[{'component':'twilio_bridge','event':'call_admitted','call_key':key,'attributes':{}}]
    def add(followup):
        rows.append({'component':'gateway','event':'backchannel.emitted','call_key':key,
            'turn_id':'fixture','attributes':{'backchannel_follow_up':followup}})
    add(False);add(True)
    assert listening_summary({'events':rows},key)['duplicate_waiting_dispatches_by_gateway_turn']==0
    add(True)
    assert listening_summary({'events':rows},key)['duplicate_waiting_dispatches_by_gateway_turn']==1
