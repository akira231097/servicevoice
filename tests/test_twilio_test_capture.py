import base64
import json
from pathlib import Path
from types import SimpleNamespace
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from app.twilio_bridge import TwilioConfig, signature
from app.twilio_test_capture import MAX_CAPTURE_SECONDS, Capture, capture_proof, valid_proof, install_capture

CALL="CA"+"1"*32
STREAM="MZ"+"2"*32
NONCE="3"*32


def packet(sequence=2,track="inbound",chunk=1,stamp=0):
    return {"event":"media","sequenceNumber":str(sequence),"streamSid":STREAM,
        "media":{"track":track,"chunk":str(chunk),"timestamp":str(stamp),"payload":base64.b64encode(b"\x7f"*160).decode()}}


def test_packet_timeline_retains_gaps_and_rejects_reordering():
    c=Capture(CALL,STREAM);c.media(packet(),1.)
    c.media(packet(3,"outbound"),1.01)
    c.media(packet(5,"inbound",3,60),1.07)
    assert len(c.audio["inbound"])==640
    assert c.audio["inbound"][160:480]==b"\xff"*320
    assert c.packets[-1]["received_unix"]==1.07
    assert {g["kind"] for g in c.gaps}=={"track","sequence"}
    with pytest.raises(ValueError,match="reordered_sequence"):c.media(packet(4),2.)


@pytest.mark.parametrize("change",["stream","track","overlap","size","timestamp"])
def test_media_bounds(change):
    c=Capture(CALL,STREAM);c.media(packet(),1.)
    value=packet(3,chunk=2,stamp=20)
    if change=="stream":value["streamSid"]="MZ"+"4"*32
    if change=="track":value["media"]["track"]="unknown"
    if change=="overlap":value["media"]["timestamp"]="0"
    if change=="size":value["media"]["payload"]=base64.b64encode(b"x"*1601).decode()
    if change=="timestamp":value["media"]["timestamp"]=str(MAX_CAPTURE_SECONDS*1000+1)
    with pytest.raises(ValueError):c.media(value,2.)


def test_owned_capture_keeps_audio_after_three_minutes_but_remains_bounded():
    c=Capture(CALL,STREAM);c.media(packet(),1.)
    c.media(packet(3,chunk=2,stamp=181000),182.)
    assert c.audio["inbound"][181000*8:181000*8+160]==b"\x7f"*160
    c.media(packet(4,chunk=3,stamp=MAX_CAPTURE_SECONDS*1000-20),240.)
    assert len(c.audio["inbound"])==MAX_CAPTURE_SECONDS*8000
    with pytest.raises(ValueError):
        c.media(packet(5,chunk=4,stamp=MAX_CAPTURE_SECONDS*1000),241.)


def test_capture_proof_expiry_and_scope():
    proof=capture_proof("secret",NONCE,1700000060)
    assert valid_proof("secret",NONCE,proof,1700000000)
    assert not valid_proof("secret",NONCE,proof,1700000060)
    assert not valid_proof("different",NONCE,proof,1700000000)
    assert not valid_proof("secret","4"*32,proof,1700000000)
    assert not valid_proof("secret",NONCE,proof,1699999900)


@pytest.mark.parametrize("fault",[None,"signature","proof","foreign_call","disabled","replay"])
def test_websocket_capture_accepts_only_owned_synthetic_call(tmp_path,fault):
    config=TwilioConfig("AC"+"1"*32,"a"*32,"SK"+"2"*32,"b"*32,"+14155550123",
        "https://lane.example.test/test","sip:"+"c"*32+"@sip.vapi.ai","d"*32)
    class Provider:
        async def request(self,method,path):
            assert method=="GET" and path==f"/Calls/{CALL}.json"
            return {"direction":"outbound-api","status":"in-progress","to":config.phone_number,
                    "from":"+14155550124" if fault=="foreign_call" else config.phone_number}
    lane=SimpleNamespace(config=config,store=SimpleNamespace(path=str(tmp_path/"bridge.sqlite3")),
        fixture=None if fault=="disabled" else b"fixture",closing=False,client=Provider())
    app=FastAPI();install_capture(app,lane)
    auth=signature(config.auth_token,config.url("tester/media"),[])
    if fault=="signature":auth="wrong"
    proof=capture_proof(config.bridge_secret,NONCE,int(time.time())+60)
    start={"event":"start","sequenceNumber":"1","streamSid":STREAM,"start":{
        "accountSid":config.account_sid,"callSid":CALL,"streamSid":STREAM,"tracks":["inbound","outbound"],
        "mediaFormat":{"encoding":"audio/x-mulaw","sampleRate":8000,"channels":1},
        "customParameters":{"nonce":NONCE,"proof":"wrong" if fault=="proof" else proof}}}
    if fault=="replay":(tmp_path/"twilio-test-captures"/NONCE).mkdir(parents=True)
    def exchange():
        with TestClient(app).websocket_connect("/twilio/tester/media",headers={"x-twilio-signature":auth}) as socket:
            socket.send_json({"event":"connected","protocol":"Call","version":"1.1.0","sequenceNumber":"0"})
            socket.send_json(start)
            if fault is not None:return socket.receive()
            socket.send_json(packet())
            socket.send_json(packet(3,"outbound"))
            socket.send_json({"event":"stop","sequenceNumber":"4","streamSid":STREAM})
            return socket.receive()
    if fault in {"signature","disabled"}:
        with pytest.raises(WebSocketDisconnect):exchange()
    else:
        result=exchange();assert result["type"]=="websocket.close"
    receipt=tmp_path/"twilio-test-captures"/NONCE/"capture.private.json"
    if fault is None:
        value=json.loads(receipt.read_text())
        assert value["provider_protocol_version"]=="1.1.0"
        assert value["outcome"]=="completed" and len(value["packets"])==2
        assert value["audio"]["inbound"]["bytes"]==160
    else:assert not receipt.exists()
