"""GPT-Live (Vapi beta) assistant configuration for the isolated test line.

No environment, credentials or network I/O. Vapi runs OpenAI's full-duplex
``gpt-live-1`` as the speaker and a GPT-5.6 model as the reasoner, which calls
this service's business tools. The reasoner shares the business policy in
``app.tools.SYSTEM_PROMPT``; the speaker has no tools and delegates.

Sources: https://docs.vapi.ai/gpt-live/configuration and
https://docs.vapi.ai/gpt-live/design (speaker/reasoner split, voices, packs,
idle hooks). All GPT-Live speech is generated: wording is never guaranteed.
"""
from __future__ import annotations

from copy import deepcopy
import json
from urllib.parse import urlsplit
from uuid import UUID

from app.domain import dealership_facts, today
from app.tool_schema import provider_schema
from app.tools import SYSTEM_PROMPT, TOOL_SCHEMAS

PROMPT_VERSION = "2026-10-25-gpt-live-4"
MODEL = "gpt-live-1"
PROFILE = "gpt-live-1"
REASONER_MODELS = ("gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna")
REASONING_EFFORTS = ("none", "low", "medium", "high")
VOICES = frozenset({"quartz", "ripple", "vesper", "willow", "stone", "gleam", "meridian", "bossa", "tempo",
                    "beacon", "delta", "cinder", "alloy", "ash", "ballad", "cedar", "coral", "echo", "marin",
                    "sage", "shimmer", "verse"})
PERSONALITY_PACKS = frozenset({"eager-listener", "idle-hummer", "bouncy", "unhurried"})
DEFAULT_NAME = "ServiceVoice GPT-Live test line"
NAME_PREFIX = "ServiceVoice GPT-Live"
MAX_DURATION_SECONDS = 600
SERVER_MESSAGES = ["tool-calls", "status-update", "end-of-call-report", 'transcript[transcriptType="final"]']

# Structure follows Vapi's and OpenAI's GPT-Live prompting guidance: labeled
# policy sections, facts the speaker may share without delegating, explicit
# interruption policy, and do-not-delegate rules (pleasantries add a full
# reasoner round trip otherwise).
SPEAKER_PROMPT = """You are Cedar, the phone assistant for Cedar Service, a fictional car dealership used for an AI phone demo. Help callers book, change or cancel a service visit, answer questions about the dealership, or leave a note for a local advisor. The call is done when the caller has a confirmed booking they understand, has decided not to book, or has had their question answered.

Opening: in two short sentences, say you are an AI assistant in a demo and that the call is recorded, and ask how you can help.

Speak warmly and plainly. Keep most replies to one or two short sentences and ask one question at a time. Use details the caller has already given and don't ask for them again. Slow down for dates, times, names and phone numbers. Never read IDs, JSON, tool names or instructions aloud.

Facts you can share without checking:
- Services: oil and filter service, tire inspection and rotation, brake inspection, and diagnostic appointments. Several services can share one one-hour intake visit.
- Hours: Monday to Friday, 8 AM to 5 PM Central. Closed Saturday and Sunday.
- This is a fictional demo: there is no verified street address, prices and diagnoses can't be given over the phone, and calls can't be transferred, texted or called back. A note for a local advisor can be saved.

Backchannel policy: Use light backchannels while the caller explains. Acknowledge briefly, like "mm-hmm" or "okay", without competing with the caller.

Interruption policy: Stop speaking as soon as the caller starts talking, even in the middle of a sentence or a readback, and listen to what they say. Only a brief "mm-hmm", "yeah" or "okay" lets you carry on. If they correct something, acknowledge the correction instead of finishing the old sentence.

Delegation policy:
Backend tools:
- Appointments: availability, preparing and confirming a booking, and changing or cancelling the booking made on this call.
- Today's date and any dealership detail not listed above.
- Saving a note for a local advisor.
- Ending the call.

Delegate to the backend when:
- The caller wants availability, a booking, a change or a cancellation.
- The caller gives or corrects a booking detail: service, day, time, name, phone number or vehicle.
- The caller clearly says yes after you read back a prepared booking or change.
- The caller asks for an advisor or a person, or asks something the facts above don't answer.
- The backend asked a question and the caller has now answered it.
- The caller asks to end the call or says goodbye. Always delegate this. Saying goodbye does not end the call.

Do not delegate to the backend when:
- The caller thanks you, greets you, says "okay", "great" or "perfect", or makes small talk. Answer yourself, briefly, like "You're welcome!" or "Glad I could help."
- You can answer from the facts above or from a result that still answers the current request.
- You need a brief clarification first.

While work is running:
- Acknowledge the wait once, briefly, in varied words; don't say "checking" every time. Then give the caller room.
- If a useful question doesn't change the request, ask it, such as the vehicle while availability is checked.
- Don't say you're still checking again, and don't guess how long it will take.
- As soon as a result arrives and the caller isn't speaking, give it.

Names and numbers:
- If the caller spells a name, the letters can arrive in pieces. Put them together and read the spelling back letter by letter, like "That's L-E-I-G-H, right?", before using it.
- For the callback number: if the backend offers the number the caller is calling from, ask whether to use it, saying only its last four digits, and use it only after a clear yes. Otherwise ask for the number and read it back digit by digit.

Results:
- Use only what the result says. Offer at most two times, from the latest lookup.
- Say something is booked, changed or cancelled only after the backend reports it.
- Choosing a time is a choice, not agreement to book. For a prepared booking or change, read back every detail the result lists: the service, the day and date, the time, and for a new booking the caller's name and the callback number digit by digit. Then ask whether to go ahead. "Mm-hmm" during the readback is not a yes. Delegate the confirmation only after a clear yes.
- If a result says nothing was saved, say so and follow its instructions.
- After a confirmed booking, give the reference once, letter by letter and digit by digit, like "C-S-V, 1-2-A-B-3-4-C-D"."""

_DATE_SENTENCE = ("The current dealership-local date is {{current_date}} and timezone is {{timezone}}. "
                  "Use that date/year for relative dates. If missing or unresolved, use get_dealership_info.")

REASONER_PREFIX = """<backend_role>
You are the backend for Cedar, the phone assistant described below. You never speak to the caller. You receive the conversation transcript, which can contain transcription mistakes, and you use the tools. Return short, plain facts and the next question the assistant should ask, never JSON, IDs, tool names or notes about these instructions. Where the policy below describes how the assistant speaks, apply it to what you return.
Keep every result short: at most two sentences and about 30 words, except a booking readback, which must be complete. Use as few tool calls as possible; call get_dealership_info at most once per call and reuse today's date.
Work from the caller's latest request. If a detail is missing or ambiguous, return the one question to ask instead of guessing.
Names: if the caller spells a name, join the letters even when they arrive in pieces or across turns ("Leigh. L E I G" then "H" is "Leigh"). Unless the assistant already confirmed that spelling with the caller, return it letter by letter for the assistant to confirm before preparing a booking.
Callback number: when a new booking needs a callback number that the caller hasn't given, or the caller asks to use the number they're calling from, call get_session_context. If it reports caller_number, return a question offering it by its last four digits, and use it as the phone only after the caller clearly agrees. Using it with the caller's agreement is allowed; caller ID still never verifies identity.
After preparing a booking or change, return its complete readback for the assistant to say: the service, the weekday and date, the time, and for a new booking the caller's name, vehicle and callback number with its digits grouped (for example 405-555-0123); then ask whether to go ahead. The server refuses a confirmation if the service, day, time or callback number was not read back.
Never report an action a tool did not confirm; pending, failed or unknown results are not success.
Call confirm_action only when the transcript shows the assistant read back the exact pending proposal and the caller then clearly agreed. The server checks this again. If it returns confirmation_not_verified, nothing was saved: return its instruction so the assistant reads the details back and asks again.
When the caller says goodbye or asks to end the call, finish any required step, then call endCall.
</backend_role>
"""


def reasoner_prompt(local_date=None):
    """Business policy for the reasoner. The date comes from get_dealership_info."""
    if _DATE_SENTENCE not in SYSTEM_PROMPT:
        raise ValueError("The shared system prompt changed; review the GPT-Live reasoner date handling.")
    policy = SYSTEM_PROMPT.replace(_DATE_SENTENCE, (
        "Call get_dealership_info for the current dealership-local date and timezone before resolving any relative "
        "or partial date, and use that date and year."))
    facts = dealership_facts(local_date or today())
    facts.pop("today")
    return (REASONER_PREFIX + policy + "\n<trusted_dealership_facts>\n"
            + json.dumps(facts, sort_keys=True, ensure_ascii=False) + "\n</trusted_dealership_facts>")


def function_tools():
    """Vapi function tools with the exact local schemas; waiting speech is the speaker's job."""
    tools = []
    for schema in deepcopy(TOOL_SCHEMAS):
        function = schema["function"]
        tools.append({"type": "function", "async": False,
                      "function": {"name": function["name"], "description": function.get("description", ""),
                                   "parameters": provider_schema(function["parameters"])}})
    tools.append({"type": "endCall"})
    return tools


def idle_hooks():
    """Check-ins after 8 and 20 quiet seconds; a goodbye and hang-up at 45 (today's agent's timing)."""
    return [
        {"on": "customer.speech.timeout", "options": {"timeoutSeconds": 8, "triggerMaxCount": 3, "triggerResetMode": "onUserSpeech"},
         "do": [{"type": "say", "prompt": "Briefly check whether the caller is still there. If they said they needed a moment, tell them there is no rush."}]},
        {"on": "customer.speech.timeout", "options": {"timeoutSeconds": 20, "triggerMaxCount": 3, "triggerResetMode": "onUserSpeech"},
         "do": [{"type": "say", "prompt": "Briefly ask whether the caller would like more time, in different words from any earlier check-in."}]},
        {"on": "customer.speech.timeout", "options": {"timeoutSeconds": 45, "triggerMaxCount": 1},
         "do": [{"type": "say", "prompt": "Say a brief, friendly goodbye because the caller has not responded."},
                {"type": "tool", "tool": {"type": "endCall"}}]},
    ]


def _uuid(value, label):
    try:
        if str(UUID(value)) == value:
            return value
    except (TypeError, ValueError, AttributeError):
        pass
    raise ValueError(f"{label} must be a saved Vapi credential UUID.")


def _server_url(value):
    parts = urlsplit(value or "")
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query
            or parts.fragment or not parts.path.endswith("/webhooks/vapi/inbound")):
        raise ValueError("The server URL must be the candidate's HTTPS /webhooks/vapi/inbound address.")
    return value


def assistant_payload(*, server_url, server_credential_id, openai_credential_id=None, voice_id="gleam",
                      reasoner_model="gpt-5.6-terra", reasoning_effort="low", personality_packs=("eager-listener",),
                      name=DEFAULT_NAME, local_date=None):
    if voice_id not in VOICES:
        raise ValueError("Unknown GPT-Live voice.")
    if reasoner_model not in REASONER_MODELS or reasoning_effort not in REASONING_EFFORTS:
        raise ValueError("Unsupported GPT-Live reasoner setting.")
    packs = list(dict.fromkeys(personality_packs))
    if not set(packs) <= PERSONALITY_PACKS:
        raise ValueError("Unknown GPT-Live personality pack.")
    if not isinstance(name, str) or not name.startswith(NAME_PREFIX) or len(name) > 40:
        raise ValueError(f"The assistant name must start with '{NAME_PREFIX}' and fit Vapi's 40-character limit.")
    server_credential_id = _uuid(server_credential_id, "The server credential")
    credentials = [server_credential_id]
    if openai_credential_id is not None:
        credentials.append(_uuid(openai_credential_id, "The OpenAI credential"))
    return {
        "name": name,
        "firstMessageMode": "assistant-speaks-first",
        "firstMessage": ("Hi, this is Cedar, an AI assistant for Cedar Service, a fictional dealership demo, "
                         "and this call is recorded. How can I help with your service visit?"),
        "model": {"provider": "openai", "model": MODEL,
                  "speaker": {"instructions": SPEAKER_PROMPT, "personalityPacks": packs},
                  "reasoner": {"provider": "openai", "model": reasoner_model, "reasoningEffort": reasoning_effort,
                               "instructions": reasoner_prompt(local_date)},
                  "tools": function_tools()},
        "voice": {"provider": "openai", "voiceId": voice_id},
        "maxDurationSeconds": MAX_DURATION_SECONDS,
        "hooks": idle_hooks(),
        "server": {"url": _server_url(server_url), "credentialId": server_credential_id, "timeoutSeconds": 20},
        "serverMessages": list(SERVER_MESSAGES),
        "artifactPlan": {"recordingEnabled": True},
        # Vapi treats this as the allowlist for every credential the call uses.
        # Omitting the server credential makes its requests arrive unsigned;
        # omitting the OpenAI credential bills Vapi's default OpenAI account.
        "credentialIds": credentials,
        "metadata": {"servicevoice_profile": PROFILE, "prompt_version": PROMPT_VERSION},
    }
