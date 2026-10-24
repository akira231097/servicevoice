"""Detect a suspected broken opening, without mistaking a quiet caller for one.

Speaker events prove energy, not intelligible speech or handset audibility. This
watchdog is deliberately limited to the opening before any caller words/tools.
It cannot diagnose where an audio failure occurred.
"""
from dataclasses import dataclass, field
import math


@dataclass
class OpeningMedia:
    admitted: float
    greeting: float = math.inf
    words: bool = False
    vapi_user_bursts: int = 0
    caller_bursts: int = 0
    agent_bursts: int = 0
    seen: set = field(default_factory=set)
    active: dict = field(default_factory=dict)

    def provider(self, message, now):
        if message.get("type") == "transcript" and message.get("role") == "user":
            value = message.get("transcript")
            if isinstance(value, str) and any(c.isalnum() for c in value): self.words = True
        if message.get("type") == "speech-update" and message.get("status") == "started":
            if message.get("role") == "assistant": self.greeting = min(self.greeting, now)
            if message.get("role") == "user": self.vapi_user_bursts += 1

    def conference(self, role, status, sequence, now):
        if role not in {"caller", "agent"} or status not in {"start", "stop"}: return
        if not isinstance(sequence, str) or not sequence.isdigit() or len(sequence) > 12: return
        key = (role, sequence)
        if key in self.seen or len(self.seen) >= 128: return
        self.seen.add(key)
        if status == "start":
            self.active.setdefault(role, now)
        elif role in self.active:
            duration = now-self.active.pop(role)
            if .15 <= duration <= 30:
                if role == "caller": self.caller_bursts += 1
                else: self.agent_bursts += 1

    def suspicion(self, now):
        if self.words or now-self.admitted > 45 or now-self.greeting < 8:
            return None
        # No greeting-only timeout and no idle-silence timeout. Require multiple
        # caller energy bursts, plus independent Vapi VAD or a missing greeting
        # on Twilio's agent leg. Never claim the handset actually heard audio.
        if self.caller_bursts >= 2 and (self.vapi_user_bursts >= 1 or self.agent_bursts == 0):
            return "caller_energy_without_words"
        return None
