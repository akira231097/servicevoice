"""Bounded, authenticated media capture for an owned synthetic self-call only.

Disabled with the isolated tester flag. Ordinary inbound callers cannot enter
this capture path. Packet arrival times share the host clock with Jev decisions.
No media is returned over an HTTP endpoint or sent to the conversation model.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import time

from fastapi import WebSocket, WebSocketDisconnect

from .twilio_bridge import BridgeError, sid, verify_signature

CAPTURE_PATH = "/twilio/tester/media"


def capture_proof(secret, nonce, expires):
    body = f"synthetic-capture:{nonce}:{expires}"
    return f"{expires}."+hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def valid_proof(secret, nonce, proof, now):
    if not isinstance(nonce,str) or not re.fullmatch("[a-f0-9]{32}",nonce) or not isinstance(proof,str):
        return False
    match=re.fullmatch(r"(\d{10})\.([a-f0-9]{64})",proof)
    if not match:return False
    expires=int(match[1])
    return now < expires <= now+90 and hmac.compare_digest(capture_proof(secret,nonce,expires),proof)


class Capture:
    def __init__(self, call_sid, stream_sid):
        self.call_sid,self.stream_sid=call_sid,stream_sid
        self.audio={"inbound":bytearray(),"outbound":bytearray()}
        self.packets=[]
        self.sequence=1
        self.chunks={"inbound":0,"outbound":0}
        self.gaps=[]
        self.started=time.time()

    def media(self, message, received):
        if message.get("streamSid")!=self.stream_sid:raise ValueError("wrong_stream")
        sequence=int(message["sequenceNumber"])
        if sequence<=self.sequence:raise ValueError("reordered_sequence")
        if sequence!=self.sequence+1:self.gaps.append({"kind":"sequence","after":self.sequence,"next":sequence})
        self.sequence=sequence
        media=message["media"];track=media["track"]
        if track not in self.audio:raise ValueError("wrong_track")
        chunk=int(media["chunk"]);stamp=int(media["timestamp"])
        if not self.chunks[track]<chunk<=12000 or not 0<=stamp<=90000:raise ValueError("media_bounds")
        raw=base64.b64decode(media["payload"],validate=True)
        if not 1<=len(raw)<=1600 or len(self.packets)>=10000:raise ValueError("packet_bounds")
        offset=stamp*8;audio=self.audio[track]
        if offset<len(audio):raise ValueError("overlapping_media")
        if offset>len(audio):
            self.gaps.append({"kind":"track","track":track,"offset":len(audio),"bytes":offset-len(audio)})
            audio.extend(b"\xff"*(offset-len(audio)))
        if offset+len(raw)>720000:raise ValueError("capture_bounds")
        audio.extend(raw);self.chunks[track]=chunk
        self.packets.append({"track":track,"sequence":sequence,"chunk":chunk,"timestamp_ms":stamp,
                             "received_unix":received,"byte_offset":offset,"bytes":len(raw)})

    def save(self, folder, outcome):
        files={}
        for track,data in self.audio.items():
            path=folder/(track+".mulaw")
            with path.open("xb") as stream:
                os.chmod(path,0o600);stream.write(data)
            files[track]={"file":path.name,"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}
        receipt={"schema":"twilio-tester-capture-v1","call_sid":self.call_sid,"stream_sid":self.stream_sid,
            "scope":"Host-received synthetic caller tracks; not handset acoustic latency",
            "encoding":"audio/x-mulaw","sample_rate":8000,"started_unix":self.started,"ended_unix":time.time(),
            "outcome":outcome,"audio":files,"packets":self.packets,"gaps":self.gaps}
        path=folder/"capture.private.json"
        with path.open("x",encoding="utf-8") as stream:
            os.chmod(path,0o600);json.dump(receipt,stream)


class TestCaptureRuntime:
    def __init__(self, lane):
        self.lane=lane
        self.active=set()
        self.directory=Path(lane.store.path).parent/"twilio-test-captures"

    async def close(self):
        for socket in tuple(self.active):
            await socket.close(code=1001)

    async def accept(self, socket:WebSocket):
        lane,config=self.lane,self.lane.config
        # HTTP Upgrade signatures have appeared with HTTPS or WSS and the
        # documented trailing slash variation. Every accepted form is pinned to
        # this exact configured endpoint, never a forwarded Host or request URL.
        url=config.url("tester/media")
        urls=(url,url+"/",url.replace("https://","wss://",1),url.replace("https://","wss://",1)+"/")
        signature=socket.headers.get("x-twilio-signature","")
        if (lane.fixture is None or lane.closing or len(self.active)>=2 or socket.scope.get("query_string")
                or not any(verify_signature(config,u,[],signature) for u in urls)):
            await socket.close(code=1008);return
        self.active.add(socket)
        capture,folder,outcome=None,None,"disconnected"
        phase="connected"
        try:
            await socket.accept()
            async with asyncio.timeout(5):
                connected=await socket.receive_json()
                if connected!={"event":"connected","protocol":"Call","version":"1.0.0"}:raise ValueError("protocol")
                phase="start"
                message=await socket.receive_json();start=message.get("start",{})
                if message.get("event")!="start" or message.get("sequenceNumber")!="1":raise ValueError("start")
                call,stream=start.get("callSid"),start.get("streamSid")
                phase="start_identity"
                if start.get("accountSid")!=config.account_sid or not sid(call,"CA") or not sid(stream,"MZ") or message.get("streamSid")!=stream:
                    raise ValueError("start_identity")
                phase="tracks"
                if set(start.get("tracks",[]))!={"inbound","outbound"}:raise ValueError("tracks")
                phase="format"
                if start.get("mediaFormat")!={"encoding":"audio/x-mulaw","sampleRate":8000,"channels":1}:raise ValueError("format")
                params=start.get("customParameters",{});nonce=params.get("nonce")
                phase="proof"
                if not valid_proof(config.bridge_secret,nonce,params.get("proof"),time.time()):raise ValueError("proof")
                phase="ownership_read"
                owned=await lane.client.request("GET",f"/Calls/{call}.json")
                phase="ownership"
                if (owned.get("to")!=config.phone_number or owned.get("from")!=config.phone_number
                        or owned.get("direction")!="outbound-api" or owned.get("status")!="in-progress"):
                    raise ValueError("not_owned_synthetic_call")
                phase="storage"
                self.directory.mkdir(mode=0o700,exist_ok=True)
                if len(list(self.directory.iterdir()))>=50:raise ValueError("capture_budget")
                folder=self.directory/nonce
                folder.mkdir(mode=0o700,exist_ok=False) # A capability is single-use across restarts.
                capture=Capture(call,stream)
            phase="media"
            async with asyncio.timeout(90):
                while True:
                    raw=await socket.receive_text()
                    received=time.time()
                    if len(raw)>8192:raise ValueError("message_bounds")
                    message=json.loads(raw)
                    if message.get("event")=="stop":
                        if message.get("streamSid")!=stream:raise ValueError("wrong_stop")
                        outcome="completed";break
                    if message.get("event")!="media":raise ValueError("unexpected_event")
                    capture.media(message,received)
        except WebSocketDisconnect:
            pass
        except (ValueError,KeyError,TypeError,TimeoutError,OSError,BridgeError) as error:
            outcome="rejected_or_timed_out"
            # Fixed phase/type only: never log provider messages, credentials,
            # capability proofs, caller identity or audio payloads.
            logging.getLogger(__name__).warning("Synthetic capture rejected: phase=%s type=%s",phase,type(error).__name__)
            await socket.close(code=1008)
        finally:
            self.active.discard(socket)
            if capture and folder:
                await asyncio.to_thread(capture.save,folder,outcome)
            try:
                await socket.close(code=1000)
            except RuntimeError:
                pass


def install_capture(app,lane):
    runtime=TestCaptureRuntime(lane) if lane else None
    if lane:lane.test_capture=runtime

    @app.websocket(CAPTURE_PATH)
    async def capture(socket:WebSocket):
        if runtime is None:
            await socket.close(code=1008);return
        await runtime.accept(socket)
