import asyncio
import json

import httpx
import pytest

from app.jev_classifier import (ClassificationUnavailable, JEV_MODEL, JevClassifier,
                                request_payload, route_candidate, parse_choice, LANGUAGES)


def state(**extra):
    return {"utterance":"What are your hours?","stage":"service","has_pending_proposal":False,
            "has_pending_operation":False,"has_appointment":False,**extra}


def result():
    return {"model":JEV_MODEL,"answers":{
        "intent":{"type":"choice","choice":"hours","probabilities":{"hours":1.,"location":0.,"services":0.,"advisor_capability":0.,"other":0.},"confidence":1.},
        "language":{"type":"choice","choice":"english_only","probabilities":{"english_only":1.,"other_or_mixed":0.,"uncertain":0.},"confidence":1.}},
        "usage":{"input_tokens":400,"output_tokens":0}}


def test_only_minimal_state_sent_no_gold_or_private_context():
    payload=request_payload(state(language_hint="gold",expected_label="gold",session_token="private",history="private",customer="private"))
    assert {k:v for k,v in payload["state"].items() if k!='published_facts'}==state()
    assert payload["state"]['published_facts']['physical_address'] is None
    assert payload["state"]['published_facts']['handoff_capabilities']['callback_delivery'] is False
    assert payload["model"]==JEV_MODEL


@pytest.mark.parametrize('probabilities,confidence',[
    ({'english_only':.97,'other_or_mixed':.02,'uncertain':.01},.95),
    ({'english_only':.99,'other_or_mixed':.01,'uncertain':0.},.98),
])
def test_independently_rounded_api_probabilities_and_confidence(probabilities,confidence):
    choice,_,_=parse_choice({'type':'choice','choice':'english_only','probabilities':probabilities,'confidence':confidence},LANGUAGES)
    assert choice=='english_only'


def test_valid_decision_and_state_language_latency_gates():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=result()))) as http:
            classifier=JevClassifier("private",client=http)
            d=await classifier.classify(state())
            assert route_candidate(state(),d)==("static_faq","candidate_only")
            for changed in [state(stage="confirmation"),state(has_appointment=True),state(has_pending_proposal=True),state(has_pending_operation=True)]:
                assert route_candidate(changed,d)[0]=="gemini"
            assert route_candidate(state(utterance="తెలుగు"),d)[0]=="gemini"
            assert route_candidate(state(),d,deadline_ms=-1)[0]=="gemini"
    asyncio.run(run())


@pytest.mark.parametrize("mutation",["wrong_model","missing_option","sum","nan","not_max","bool","confidence","no_language","mixed"])
def test_malformed_scores_never_route_as_static(mutation):
    value=result()
    a=value["answers"]["intent"]
    if mutation=="wrong_model":value["model"]="unrequested"
    elif mutation=="missing_option":del a["probabilities"]["other"]
    elif mutation=="sum":a["probabilities"]["other"]=.5
    elif mutation=="nan":a["probabilities"]["hours"]=float("nan")
    elif mutation=="not_max":a["choice"]="other"
    elif mutation=="bool":a["confidence"]=True
    elif mutation=="confidence":a["confidence"]=.2
    elif mutation=="no_language":del value["answers"]["language"]
    else:
        value["answers"]["language"].update(choice="other_or_mixed",probabilities={"english_only":0.,"other_or_mixed":1.,"uncertain":0.})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=json.dumps(value),headers={"content-type":"application/json"}))) as http:
            classifier=JevClassifier("private",client=http)
            if mutation=="mixed":
                assert route_candidate(state(),await classifier.classify(state()))[0]=="gemini"
            else:
                with pytest.raises(ClassificationUnavailable):await classifier.classify(state())
    asyncio.run(run())


@pytest.mark.parametrize("status",[401,429,529,302])
def test_errors_not_retried_and_no_body_or_credential_in_error(status):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status,text="private-key and raw provider data",headers={"location":"https://example.org"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False) as http:
            with pytest.raises(ClassificationUnavailable) as error:
                await JevClassifier("private-key",client=http).classify(state())
            assert str(error.value)=="provider_http_"+str(status)
            assert len(calls)==1
    asyncio.run(run())


def test_timeout_and_external_cancellation_do_not_retry():
    calls=[]
    async def handler(request):
        calls.append(request)
        await asyncio.sleep(10)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            classifier=JevClassifier("private",client=http,timeout_seconds=.05)
            with pytest.raises(ClassificationUnavailable,match="timeout"):
                await classifier.classify(state())
            task=asyncio.create_task(classifier.classify(state()))
            await asyncio.sleep(.01);task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert len(calls)==2
    asyncio.run(run())


def test_oversize_input_and_response_rejected():
    with pytest.raises(ClassificationUnavailable):request_payload(state(utterance="x"*1601))
    with pytest.raises(ClassificationUnavailable):request_payload(state(has_pending_operation="false"))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b" "*32769,headers={"content-type":"application/json"}))) as http:
            with pytest.raises(ClassificationUnavailable,match="response_bounds"):
                await JevClassifier("private",client=http).classify(state())
    asyncio.run(run())
