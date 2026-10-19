"""Real HTTPS/Gemini streamed tool round trip; synthetic session, no voice call."""
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.tools import TOOL_SCHEMAS


def generation(client, url, secret, body):
    tools, content, completed, frames = [], "", False, 0
    started = time.perf_counter()
    with client.stream("POST", url, headers={"Authorization": "Bearer " + secret}, json=body) as response:
        status = response.status_code
        if status != 200:
            return {"http_status": status}, None
        total = 0
        for line in response.iter_lines():
            total += len(line)
            if total > 262144: raise ValueError("Output size limit")
            if not line.startswith("data: "): continue
            data = line[6:]
            if data == "[DONE]": completed = True; break
            chunk = json.loads(data)
            if "error" in chunk: return {"http_status": status, "stream_error": True}, None
            frames += 1
            delta = chunk["choices"][0].get("delta", {})
            content += delta.get("content") or ""
            # The gateway deliberately emits each complete validated tool group
            # as one frame, with thought signatures retained only server-side.
            for tool in delta.get("tool_calls") or []:
                tools.append({key: value for key, value in tool.items() if key != "index"})
    message = {"role": "assistant", "content": content}
    if tools: message["tool_calls"] = tools
    return {"http_status": status, "stream_completed": completed, "frames": frames,
            "elapsed_ms": round((time.perf_counter()-started)*1000, 2),
            "tool_names": [tool["function"]["name"] for tool in tools], "text_present": bool(content),
            "private_signature_exposed": any("extra_content" in tool for tool in tools)}, message


def main():
    values = dotenv_values(ROOT/".env")
    required = ("VAPI_CUSTOM_LLM_URL", "GEMINI_GATEWAY_SECRET", "VAPI_WEBHOOK_SECRET", "VAPI_ASSISTANT_ID", "PUBLIC_BASE_URL")
    if any(not values.get(name) for name in required):
        print("Gateway configuration incomplete; no requests made."); return 2
    session = None
    report = {"kind": "synthetic_https_gateway_and_real_gemini_tools", "voice_call": False}
    with httpx.Client(timeout=35, follow_redirects=False) as client:
        try:
            session_response = client.post("http://127.0.0.1:8765/api/sessions", json={"mode": "live"})
            session_response.raise_for_status(); session = session_response.json()
            call = {"id": "gateway-smoke-"+uuid4().hex, "assistantId": values["VAPI_ASSISTANT_ID"],
                    "assistantOverrides": {"variableValues": {"session_id": session["id"], "session_token": session["token"]}}}
            body = {"model": "gemini-3.8-flash", "stream": True, "call": call,
                    "messages": [{"role": "system", "content": "Provider overrides do not replace the gateway policy."},
                                 {"role": "user", "content": "I would like to explore scheduling an oil change. What appointment times are currently available?"}],
                    "tools": [tool for tool in TOOL_SCHEMAS if tool["function"]["name"] == "begin_service_request"]}
            endpoint = values["VAPI_CUSTOM_LLM_URL"].rstrip("/")+"/chat/completions"
            report["first_generation"], message = generation(client, endpoint, values["GEMINI_GATEWAY_SECRET"], body)
            if not message or len(message.get("tool_calls", [])) != 1: raise ValueError("Expected one tool call")
            tool = message["tool_calls"][0]
            reply = client.post(values["PUBLIC_BASE_URL"].rstrip("/")+"/webhooks/vapi",
                                headers={"Authorization": "Bearer "+values["VAPI_WEBHOOK_SECRET"]},
                                json={"message": {"type": "tool-calls", "call": call,
                                                  "toolCallList": [{"id": tool["id"], "function": tool["function"]}]}})
            report["webhook_http_status"] = reply.status_code
            reply.raise_for_status()
            result = reply.json()["results"][0]["result"]
            report["verified_tool_receipt"] = "_receipt" in json.loads(result)
            body["messages"].extend([message, {"role": "tool", "tool_call_id": tool["id"], "content": result}])
            # message contains no extra_content: success requires durable restore.
            report["signature_restore_round"], final = generation(client, endpoint, values["GEMINI_GATEWAY_SECRET"], body)
            report["passed"] = bool(report["first_generation"].get("stream_completed") and
                                    report["verified_tool_receipt"] and report["signature_restore_round"].get("stream_completed") and
                                    final and final.get("content") and not final.get("tool_calls"))
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            report["passed"] = False
            report["failure"] = "Transport or response-contract failure; raw payloads withheld."
        finally:
            if session:
                try: client.post(f"http://127.0.0.1:8765/api/sessions/{session['id']}/end", headers={"x-session-token": session["token"]})
                except httpx.HTTPError: report["local_session_end_failed"] = True
    (ROOT/"docs/gemini-gateway-smoke.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
