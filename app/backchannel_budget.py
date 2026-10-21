"""Bounded, event-loop-owned acknowledgment budget; never booking authority."""
from collections import OrderedDict
import time


class BackchannelBudget:
    def __init__(self, *, cooldown_seconds=15, per_call=4, capacity=256, clock=time.monotonic):
        self.cooldown, self.per_call, self.capacity, self.clock = cooldown_seconds, per_call, capacity, clock
        self.calls = OrderedDict()

    def consume(self, call_id, user_count):
        now = self.clock()
        old = self.calls.get(call_id)
        if old is not None:
            ordinal, emitted, count = old
            if user_count <= ordinal:return False, "turn_already_acknowledged"
            if now-emitted < self.cooldown:return False, "cooldown"
            if count >= self.per_call:return False, "call_budget"
        elif len(self.calls) >= self.capacity:
            # Never evict a call's budget to let another request speak.
            return False, "budget_capacity"
        self.calls[call_id] = (user_count, now, (old[2] if old else 0)+1)
        return True, "emitted"
