"""Validate local Vapi keys and optionally register this project's webhook credential.

Default is read-only. --apply-credential creates only a custom authentication
credential for the existing local webhook secret, never a phone call or API key.
No credential values or provider response bodies are printed.
"""
import argparse
import json
from pathlib import Path
from uuid import UUID

import httpx
from dotenv import dotenv_values, set_key

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-credential",action="store_true")
    args=parser.parse_args()
    values=dotenv_values(ROOT/".env")
    private=values.get("VAPI_API_KEY") or ""
    public=values.get("VAPI_PUBLIC_KEY") or ""
    if not private:
        print("Private key missing in .env.");return 1
    report={}
    try:
        with httpx.Client(base_url="https://api.vapi.ai",headers={"Authorization":"Bearer "+private},timeout=25,follow_redirects=False) as client:
            response=client.get("/assistant")
            report["private_api_http_status"]=response.status_code
            if response.status_code!=200:
                print(json.dumps(report));return 1
            if public:
                tokens=client.get("/token",params={"limit":1000})
                report["token_list_http_status"]=tokens.status_code
                if tokens.status_code==200:
                    data=tokens.json()
                    if isinstance(data,list):
                        report["public_key_found_in_account"]=any(isinstance(t,dict) and t.get("tag")=="public" and public in (t.get("value"),t.get("id")) for t in data)
            else:
                report["public_key_present"]=False
            existing=values.get("VAPI_SERVER_CREDENTIAL_ID")
            if existing:
                response=client.get("/credential/"+str(UUID(existing)))
                report["webhook_credential_http_status"]=response.status_code
            elif args.apply_credential:
                secret=values.get("VAPI_WEBHOOK_SECRET") or ""
                if len(secret)<32:
                    print("Webhook secret must have at least 32 characters.");return 1
                payload={"provider":"custom-credential","name":"servicevoice-webhook",
                         "authenticationPlan":{"type":"bearer","token":secret,"headerName":"Authorization","bearerPrefixEnabled":True}}
                # Deliberately no retry: the remote create can succeed before a timeout.
                response=client.post("/credential",json=payload)
                report["webhook_credential_create_http_status"]=response.status_code
                if response.is_success:
                    saved_id=str(UUID(response.json()["id"]))
                    set_key(str(ROOT/".env"),"VAPI_SERVER_CREDENTIAL_ID",saved_id)
                    report["webhook_credential_saved"]=True
                else:
                    print(json.dumps(report));return 1
        print(json.dumps(report,indent=2))
        return 0
    except httpx.RequestError:
        print("Network request incomplete. Check the Vapi account before retrying a credential create.");return 1
    except (ValueError,KeyError,TypeError,OSError):
        print("Unexpected configuration or provider response. Inspect the account without exposing credential values.");return 1

if __name__=="__main__":
    raise SystemExit(main())
