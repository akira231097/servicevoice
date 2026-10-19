"""Synthetic adapter for the deployed streaming gateway; never a running server.

Only explicitly executed evaluations construct this adapter. The upstream client
and request budget belong to the evaluator. A disposable Store, real gateway,
real tool authorization/observations, receipt narration and optional Jev routing
are used. Provider extras and signatures remain inside the gateway.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from app.gemini_gateway import render_system_prompt
from scripts.evaluate_gateway_latency import Conversation, measured_provider


class GatewayConversation:
    def __init__(self, recording, store, key, budget, *, jev_key=None):
        self.turns = []
        self.metrics = []
        self._started = False
        self._closed = False
        provider = measured_provider(recording.client, budget, recording.rows)
        # The suite owns this pooled client. A disposable gateway must release
        # its own state without closing the connection pool for the next case.
        async def release_borrowed_client():
            return None
        provider.aclose = release_borrowed_client
        self._inner = Conversation(Path(store.path), key, provider, narration="on",
                                   jev="on" if jev_key else "off", jev_key=jev_key)
        self.store, self.sid, self.harness = self._inner.store, self._inner.sid, self._inner.harness
        prompt = render_system_prompt()
        self.prompt_metadata = {"mode": "server_owned_static_facts", "trusted_facts_supplied": True,
            "rendered_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "source": "app.gemini_gateway.render_system_prompt", "transport": "streaming_gateway",
            "jev_enabled": bool(jev_key)}

    @property
    def generations(self):
        return self._inner.generations

    async def say(self, text):
        turn = {"user": text, "assistant": "", "tools": []}
        self.turns.append(turn)
        if not self._started:
            await self._inner.start()
            self._started = True
        turn["assistant"] = await self._inner.say(text)
        return turn

    def snapshot(self):
        return self.store.snapshot(self.sid)

    async def close(self):
        if not self._closed:
            self._closed = True
            await self._inner.close()
