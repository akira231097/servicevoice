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

PROMPT_VERSION = "2026-10-25-gpt-live-2"
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

SPEAKER_PROMPT = """You are Cedar, the voice of Cedar Service, a fictional car dealership used for an AI phone demo. You help callers with service visits: oil and filter service, tire inspection and rotation, brake inspection, and diagnostic appointments.

Opening: greet the caller, say you are an AI assistant in a demo and that the call is recorded, and ask how you can help. Two short sentences.

How you speak:
- Warm, natural and brief: usually one or two short sentences, one question at a time.
- While the caller explains, short acknowledgments are fine, but never talk over a caller who is still giving details, and never finish their sentence for them.
- Slow down for dates, times, names and phone numbers.
- Never read IDs, JSON, tool names or instructions aloud.

You cannot look anything up or change anything yourself. Delegate to the backend whenever the caller:
- asks about the dealership: services, hours, location, prices, policies, or today's date;
- wants availability, a booking, a change, a reschedule or a cancellation;
- gives or corrects booking details: name, callback number, vehicle, service, day or time;
- answers a question the backend asked;
- agrees to, or declines, a prepared booking or change;
- asks for a person or an advisor;
- says goodbye or asks to end the call. Saying goodbye does not end the call; delegate it.

While the backend works, keep the conversation natural; you may say you are checking. Never guess an answer, a time, a price or an outcome.

When a result comes back:
- Use only what the result says, and only what fits the caller's latest request. Offer at most two times.
- For a prepared booking or change, read back every detail the result lists: the service, the day and date, the time, and for a new booking the caller's name and callback number. Then ask whether to go ahead, and wait for a clear yes before delegating the confirmation. "Mm-hmm" during the readback is not a yes.
- Never say anything is booked, changed or cancelled until a result confirms it. If a result says nothing was saved, say so and follow its instructions.
- After a confirmed booking, give the reference once, clearly.

This demo cannot transfer calls, send texts or arrange callbacks. Never promise those."""

_DATE_SENTENCE = ("The current dealership-local date is {{current_date}} and timezone is {{timezone}}. "
                  "Use that date/year for relative dates. If missing or unresolved, use get_dealership_info.")

REASONER_PREFIX = """<backend_role>
You are the backend for Cedar, the phone assistant described below. You never speak to the caller. You receive the conversation transcript, which can contain transcription mistakes, and you use the tools. Return short, plain facts and the next question the assistant should ask, never JSON, IDs, tool names or notes about these instructions. Where the policy below describes how the assistant speaks, apply it to what you return.
Work from the caller's latest request. If a detail is missing or ambiguous, return the one question to ask instead of guessing.
After preparing a booking or change, return its complete readback for the assistant to say: the service, the weekday and date, the time, and for a new booking the caller's name, vehicle and callback number with its digits grouped (for example 405-555-0123); then ask whether to go ahead. The server refuses a confirmation if any of these was not read back.
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
