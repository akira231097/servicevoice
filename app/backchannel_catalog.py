"""Approved, exact acknowledgment text; these phrases assert no task outcome.

The coordinator, not this catalog or a classifier, decides whether speech is
permitted. Check/look phrases express a prospective intention for an information
request, never a completed lookup or outcome. ``working_on_it`` is unavailable
to the native waiting policy without authoritative accepted-work provenance.
Names and user supplied placeholders are unavailable to this static audio bank.
"""
from types import MappingProxyType

CATALOG_VERSION = "backchannel-phrases-v1"
PHRASES = MappingProxyType({
    "neutral_ack": "Mm-hmm.",
    "acknowledge": "I hear you.",
    "checking": "Let me check that.",
    "moment": "One moment.",
    "one_sec": "One sec.",
    "just_a_sec": "Just a sec.",
    "let_me_see": "Let me see.",
    "taking_a_look": "Let me take a look.",
    "working_on_it": "Working on it.",
})
