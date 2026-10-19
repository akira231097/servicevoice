"""Server-owned tool issuance context, separate from opaque provider extras.

This binds a tool decision to the ordered user-content history supplied to the
gateway. It is a freshness check, not independent authentication of human speech
or consent. Provider signature bytes/JSON are never modified or forwarded with
our reserved metadata.
"""
from copy import deepcopy
import hashlib
import json
import re

RESERVED_KEY = "__servicevoice_issuance_context"
PROVIDER_KEY = "provider_extra_content"


def _context(value):
    if (not isinstance(value, dict)
            or set(value) != {"version", "user_context_sha256", "user_count"}
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["user_count"]) is not int or not 1 <= value["user_count"] <= 2**31-1
            or not isinstance(value["user_context_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["user_context_sha256"]) is None):
        raise ValueError("invalid_issuance_context")
    return dict(value)


def user_context(messages):
    """Hash exact ordered user strings; zero user turns have no anchor."""
    if not isinstance(messages, list):
        raise ValueError("invalid_context_history")
    users = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("invalid_context_history")
        if message.get("role") == "user":
            content = message.get("content")
            if not isinstance(content, str):
                raise ValueError("invalid_context_history")
            users.append(content)
    if not users:
        return None
    raw = json.dumps(users, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return {"version": 1, "user_context_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(), "user_count": len(users)}


def encode_extras(opaque, context=None):
    """Only server callers supply context; reject a model's reserved collision."""
    if not isinstance(opaque, dict) or RESERVED_KEY in opaque:
        raise ValueError("reserved_issuance_context_collision")
    provider = deepcopy(opaque)
    if context is None:
        return provider
    return {RESERVED_KEY: _context(context), PROVIDER_KEY: provider}


def decode_extras(stored):
    """Legacy opaque records remain readable, but have no narration anchor."""
    if not isinstance(stored, dict):
        raise ValueError("invalid_stored_tool_extras")
    if RESERVED_KEY not in stored:
        return deepcopy(stored), None
    if set(stored) != {RESERVED_KEY, PROVIDER_KEY}:
        raise ValueError("invalid_issuance_envelope")
    provider = stored[PROVIDER_KEY]
    if not isinstance(provider, dict) or RESERVED_KEY in provider:
        raise ValueError("invalid_issuance_envelope")
    return deepcopy(provider), _context(stored[RESERVED_KEY])
