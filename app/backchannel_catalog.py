"""Approved, exact acknowledgment text; these phrases assert no task outcome.

The coordinator, not this catalog or a classifier, decides whether speech is
permitted. ``checking`` requires an actual active read. Names and user supplied
placeholders are deliberately unavailable to this static audio experiment.
"""
from types import MappingProxyType

CATALOG_VERSION = "backchannel-phrases-v1"
PHRASES = MappingProxyType({
    "neutral_ack": "Mm-hmm.",
    "acknowledge": "I hear you.",
    "checking": "Let me check that.",
    "moment": "One moment.",
})

