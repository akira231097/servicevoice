"""Opt-in Vapi platform control, deliberately outside the booking tool registry.

No network, credentials, environment reads, scheduler writes or call controller
live here. Vapi executes its native endCall tool after its blocking farewell.
"""
from copy import deepcopy

FLAG = "SERVICEVOICE_NATIVE_END_CALL"
END_CALL = "endCall"

CLOSING_POLICY = """<call_closing>
When the native endCall tool is available, you MUST issue it after the caller clearly
asks to end the call or says goodbye as their actual intent, or after the task
is complete AND the caller separately confirms they are finished. Once those
conditions are met, issue endCall rather than replying with farewell text alone;
farewell text does not disconnect the call. Interpret the
whole current turn in its conversational context. A booking confirmation alone,
a routine thank-you or acknowledgment, silence, a quoted farewell, a question
about a farewell, a refusal such as not yet, or goodbye followed by wait, a
correction or another request is not permission to end. If uncertain, ask one
brief clarification and keep listening. Never require a particular language or
keyword to understand the caller's intent.
Do not use endCall while an accepted action is pending, unknown or unresolved.
Reconcile it with the existing status tools before closing; never repeat a
business action to make closing possible. An unconfirmed proposal is not a
booking: an explicit goodbye may end that conversation without confirming it.
Call endCall alone, with no arguments and no other tools in the same response.
Vapi owns the final blocking farewell and disconnect; do not add a separate
farewell caption or claim that the call has already ended. A new caller request
before issuance supersedes closing. A recorded native attempt does not prove
execution, a booking change or successful audio delivery.
</call_closing>"""

END_CALL_SCHEMA = {"type": "function", "function": {
    "name": END_CALL,
    "description": "End this call only when the caller clearly wants to finish, following call_closing policy. Vapi speaks the final farewell before disconnecting. Never call with another tool, on booking confirmation alone, a quoted goodbye, a new request, or an unresolved action.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}

UNKNOWN_ATTEMPT_CONTEXT = (
    "The gateway authorized a Vapi native end-call attempt in this conversation. "
    "Its execution outcome and farewell playback are not independently verified. "
    "No booking outcome is established by that attempt. This conversation is "
    "continuing; listen to the current caller request rather than assuming it ended."
)


def enabled(config):
    """Only a trusted local exact opt-in enables the platform capability."""
    return config.get(FLAG) == "on"


def native_tool():
    """Provider-owned farewell; no free-form endCall arguments or custom server.

    Official Vapi ToolMessageStart documents that contents[0] is translated to
    the active language when no matching variant is supplied, and blocking=true
    delays tool execution until that message finishes. One source language is
    therefore not a caller-language allowlist.
    https://github.com/VapiAI/server-sdk-typescript/blob/ce0b348412130447f18e77363de7a97ca12f244b/src/api/types/ToolMessageStart.ts
    https://docs.vapi.ai/tools/default-tools
    Playback completion and cancellation after issuance still need phone tests.
    """
    return {"type": "endCall", "messages": [{"type": "request-start", "blocking": True,
        "contents": [{"type": "text", "language": "en", "text": "Thank you for calling. Goodbye."}]}]}


def schema():
    return deepcopy(END_CALL_SCHEMA)
