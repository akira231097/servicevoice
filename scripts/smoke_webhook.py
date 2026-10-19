"""Exercise HTTPS webhook routing with synthetic data; never starts a voice call."""
import json
import uuid
from pathlib import Path
import httpx
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]

def main():
    env=dotenv_values(ROOT/".env")
    base=env.get("PUBLIC_BASE_URL","").rstrip("/")
    if not base.startswith("https://"):
        raise SystemExit("Configure PUBLIC_BASE_URL first.")
    with httpx.Client(timeout=20) as client:
        local="http://127.0.0.1:"+str(env.get("SERVICEVOICE_PORT") or "8765")
        created=client.post(local+"/api/sessions",json={"mode":"live"})
        created.raise_for_status();session=created.json()
        try:
            call={"id":"synthetic-smoke-"+uuid.uuid4().hex,"assistantId":env["VAPI_ASSISTANT_ID"],
                  "assistantOverrides":{"variableValues":{"session_id":session["id"],"session_token":session["token"]}}}
            payload={"message":{"type":"tool-calls","call":call,"toolCallList":[{"id":"smoke-context","function":{"name":"begin_service_request","arguments":{"service":"oil_change"}}}]}}
            response=client.post(base+"/webhooks/vapi",json=payload,headers={"Authorization":"Bearer "+env["VAPI_WEBHOOK_SECRET"]})
            response.raise_for_status()
            result=json.loads(response.json()["results"][0]["result"])
            replay=client.post(base+"/webhooks/vapi",json=payload,headers={"Authorization":"Bearer "+env["VAPI_WEBHOOK_SECRET"]})
            replay.raise_for_status()
            replayed=json.loads(replay.json()["results"][0]["result"])
            snapshot=client.get(local+"/api/sessions/"+session["id"],headers={"X-Session-Token":session["token"]}).json()
            report={"test_type":"synthetic HTTPS tool webhook; no Vapi call","http_status":response.status_code,
                    "business_result_ok":result.get("ok"),"receipt_present":"_receipt" in result,
                    "available_slots":len(result.get("slots",[])),"exact_retry_replayed":result==replayed,
                    "persisted_tool_events":len(snapshot["events"]),"appointment_created":snapshot["appointment"] is not None}
            target=ROOT/"docs"/"webhook-smoke-result.json"
            target.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
            print(json.dumps(report,indent=2))
            return 0 if report["business_result_ok"] and report["exact_retry_replayed"] and report["persisted_tool_events"]==1 else 1
        finally:
            client.post(local+"/api/sessions/"+session["id"]+"/end",headers={"X-Session-Token":session["token"]})

if __name__=="__main__":
    raise SystemExit(main())
