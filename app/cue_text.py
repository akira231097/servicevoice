"""Normalize ASR typography without translating or changing spoken content."""

_TYPOGRAPHY = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " ",
})


def cue_text(value):
    return value.translate(_TYPOGRAPHY) if isinstance(value, str) else value
