"""Bounded, event-loop-owned acknowledgment budget; never booking authority."""
from collections import OrderedDict
import time


class BackchannelBudget:
    def __init__(self, *, cooldown_seconds=1, per_call=4, window_seconds=15, capacity=256, clock=time.perf_counter):
        self.cooldown, self.per_call, self.capacity, self.clock = cooldown_seconds, per_call, capacity, clock
        self.window = window_seconds
        self.calls = OrderedDict()
        # Latest consumption per call: (stamp, row before it). A refund restores
        # that row only while the stamped consumption is still the current one.
        self.receipts = {}

    def previous_phrase(self, call_id):
        row = self.calls.get(call_id)
        return row[3] if row else None

    def receipt(self, call_id):
        """Stamp of the latest consumption, for a later refund of an unplayed cue."""
        value = self.receipts.get(call_id)
        return value[0] if value else None

    def retire_confirmed(self, call_ids):
        """Only the gateway's authoritative ended/expired-session read may retire."""
        for call_id in call_ids:
            self.calls.pop(call_id, None)
            self.receipts.pop(call_id, None)

    def consume(self, call_id, user_count, phrase_id=None):
        now = self.clock()
        old = self.calls.get(call_id)
        if old is not None:
            ordinal, emitted, timestamps, previous = old
            if user_count <= ordinal:return False, "turn_already_acknowledged"
            if now-emitted < self.cooldown:return False, "cooldown"
            timestamps = [stamp for stamp in timestamps if now-stamp < self.window]
            if len(timestamps) >= self.per_call:return False, "call_budget"
        elif len(self.calls) >= self.capacity:
            # Never evict a call's budget to let another request speak.
            return False, "budget_capacity"
        else:timestamps = []
        self.calls[call_id] = (user_count, now, timestamps+[now], phrase_id)
        self.receipts[call_id] = (now, old)
        return True, "emitted"

    def refund(self, call_id, stamp):
        """Give back the latest consumption when its cue provably never reached playback.

        Only the gateway decides that: a generation Vapi cancelled or replaced
        before any answer text was released. The turn ordinal and rolling window
        return to their prior state, but the cooldown still counts from the
        unplayed emission, so a refund can never permit back-to-back speech.
        """
        receipt, current = self.receipts.get(call_id), self.calls.get(call_id)
        if receipt is None or current is None or receipt[0] != stamp or current[1] != stamp:
            return False, "not_refundable"
        previous = receipt[1]
        ordinal, emitted, timestamps, phrase = previous if previous is not None else (current[0]-1, stamp, [], None)
        self.calls[call_id] = (ordinal, max(emitted, stamp), list(timestamps), phrase)
        del self.receipts[call_id]
        return True, "refunded"
