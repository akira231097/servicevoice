"""Connect the isolated GPT-Live test number to Vapi, or undo it.

Default is a read-only preview of the Twilio number and any matching Vapi
number. --apply creates a dedicated, revocable Twilio API key, gives Vapi only
that key (never the account Auth Token), and imports the number with no
assistant so every call reaches the candidate's authenticated assistant-request.
The new key secret stays in memory. --rollback deletes the Vapi number and the
dedicated key and restores the Twilio voice settings from the receipt.
No credential values are printed or written.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re

import httpx
from dotenv import dotenv_values

VAPI = "https://api.vapi.ai"
TWILIO = "https://api.twilio.com/2010-04-01"
KEY_NAME = "vapi-gpt-live-218"
VOICE_FIELDS = ("VoiceUrl", "VoiceMethod", "VoiceFallbackUrl", "VoiceFallbackMethod", "StatusCallback",
                "StatusCallbackMethod", "VoiceApplicationSid", "VoiceReceiveMode")


def e164(value):
    if not re.fullmatch(r"\+1[2-9][0-9]{9}", value or ""):
        raise ValueError("Use the E.164 test number, for example +12025550143.")
    return value


def twilio_number(client, account, number):
    found = client.get(f"{TWILIO}/Accounts/{account}/IncomingPhoneNumbers.json", params={"PhoneNumber": number})
    found.raise_for_status()
    rows = found.json().get("incoming_phone_numbers", [])
    if len(rows) != 1:
        raise ValueError("The number is not exactly one incoming number in this Twilio account.")
    return rows[0]


def vapi_numbers(client, number):
    rows = client.get("/phone-number", params={"limit": 100})
    rows.raise_for_status()
    return [row for row in rows.json() if row.get("number") == number]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--number", required=True)
    parser.add_argument("--server-url")
    parser.add_argument("--server-credential-id")
    parser.add_argument("--receipt", type=Path, required=True, help="Private JSON receipt (IDs and prior voice settings only).")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", action="store_true")
    args = parser.parse_args(argv)
    env = dotenv_values(args.env_file, interpolate=False)
    number = e164(args.number)
    account, token = env.get("TWILIO_ACCOUNT_SID") or "", env.get("TWILIO_AUTH_TOKEN") or ""
    vapi_key = env.get("VAPI_API_KEY") or ""
    if not (account.startswith("AC") and token and vapi_key):
        print("TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and VAPI_API_KEY are required in the env file.")
        return 2
    with httpx.Client(auth=(account, token), timeout=30, trust_env=False, follow_redirects=False) as twilio, \
            httpx.Client(base_url=VAPI, headers={"Authorization": "Bearer " + vapi_key}, timeout=30,
                         trust_env=False, follow_redirects=False) as vapi:
        current = twilio_number(twilio, account, number)
        imported = vapi_numbers(vapi, number)
        state = {"twilio_number_sid": current["sid"], "voice_url_set": bool(current.get("voice_url")),
                 "voice_application_set": bool(current.get("voice_application_sid")),
                 "vapi_number_ids": [row.get("id") for row in imported]}
        if not (args.apply or args.rollback):
            print(json.dumps({"preview": state}, indent=2))
            return 0
        if args.rollback:
            receipt = json.loads(args.receipt.read_text())
            if receipt.get("twilio_number_sid") != current["sid"]:
                print("Refused: the receipt is for a different number.")
                return 2
            report = {}
            if receipt.get("vapi_phone_number_id"):
                report["vapi_delete_status"] = vapi.delete(f"/phone-number/{receipt['vapi_phone_number_id']}").status_code
            previous = receipt["previous_voice"]
            # URLs and the application are cleared when they were unset; methods
            # and the receive mode are only restored when they had a value.
            restore = {field: previous.get(field) or "" for field in
                       ("VoiceUrl", "VoiceFallbackUrl", "StatusCallback", "VoiceApplicationSid")}
            restore.update({field: previous[field] for field in
                            ("VoiceMethod", "VoiceFallbackMethod", "StatusCallbackMethod", "VoiceReceiveMode") if previous.get(field)})
            report["twilio_restore_status"] = twilio.post(
                f"{TWILIO}/Accounts/{account}/IncomingPhoneNumbers/{current['sid']}.json", data=restore).status_code
            if receipt.get("twilio_key_sid"):
                report["twilio_key_delete_status"] = twilio.delete(
                    f"{TWILIO}/Accounts/{account}/Keys/{receipt['twilio_key_sid']}.json").status_code
            print(json.dumps(report))
            return 0
        if imported:
            print("Refused: Vapi already has this number. Roll back or inspect it first.")
            return 1
        if not (args.server_url and args.server_credential_id):
            print("--server-url and --server-credential-id are required to import.")
            return 2
        previous = {field: current.get(re.sub(r"(?<!^)(?=[A-Z])", "_", field).lower()) for field in VOICE_FIELDS}
        receipt = {"created_at": datetime.now(timezone.utc).isoformat(), "number": number,
                   "twilio_number_sid": current["sid"], "previous_voice": previous}
        args.receipt.write_text(json.dumps(receipt, indent=2))
        key = twilio.post(f"{TWILIO}/Accounts/{account}/Keys.json", data={"FriendlyName": KEY_NAME})
        if not key.is_success:
            print(json.dumps({"twilio_key_status": key.status_code}))
            return 1
        key_sid, key_secret = key.json()["sid"], key.json()["secret"]
        receipt["twilio_key_sid"] = key_sid
        args.receipt.write_text(json.dumps(receipt, indent=2))
        created = vapi.post("/phone-number", json={
            "provider": "twilio", "number": number, "name": "ServiceVoice GPT-Live 218",
            "twilioAccountSid": account, "twilioApiKey": key_sid, "twilioApiSecret": key_secret,
            "server": {"url": args.server_url, "credentialId": args.server_credential_id, "timeoutSeconds": 20}})
        key_secret = None
        if not created.is_success:
            # Leave nothing half-connected: the dedicated key is useless without the import.
            twilio.delete(f"{TWILIO}/Accounts/{account}/Keys/{key_sid}.json")
            receipt.pop("twilio_key_sid")
            args.receipt.write_text(json.dumps(receipt, indent=2))
            print(json.dumps({"vapi_import_status": created.status_code}))
            return 1
        receipt["vapi_phone_number_id"] = created.json()["id"]
        args.receipt.write_text(json.dumps(receipt, indent=2))
        after = twilio_number(twilio, account, number)
        print(json.dumps({"vapi_phone_number_id": receipt["vapi_phone_number_id"], "twilio_key_sid": key_sid,
                          "assistant_on_number": created.json().get("assistantId") is not None,
                          "twilio_voice_url_now_set": bool(after.get("voice_url"))}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
