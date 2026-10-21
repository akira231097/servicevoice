"""Approved, exact acknowledgment text; these phrases assert no task outcome.

The coordinator, not this catalog or a classifier, decides whether speech is
permitted. Prospective check/look phrases express an intention for a current
information request, never a verified lookup or outcome. ``working_on_it``
requires authoritative accepted work that is still queued or running. Names
and user supplied placeholders are unavailable to this static audio bank.
"""
from types import MappingProxyType

CATALOG_VERSION = "backchannel-phrases-v1"
# Additive entries preserve all existing exact transcripts and fingerprints.
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
