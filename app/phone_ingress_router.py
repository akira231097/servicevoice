"""Exact configured phone identities sharing storage, never admission authority."""
from .telephony_ingress import IngressError


class PhoneIngressRouter:
    def __init__(self, primary, isolated, store):
        self.primary, self.isolated, self.store = primary, isolated, store
        self.config, self.limits = primary.config, primary.limits
        if (primary.config.phone_number_id == isolated.config.phone_number_id
                or primary.config.assistant_id == isolated.config.assistant_id):
            raise ValueError("Isolated phone identity must be distinct.")

    def select(self, call):
        if not isinstance(call, dict): raise IngressError("invalid_telephony_call", 422)
        for ingress in (self.primary, self.isolated):
            if (call.get("orgId"), call.get("phoneNumberId")) == (ingress.config.organization_id, ingress.config.phone_number_id):
                return ingress
        raise IngressError("telephony_identity_mismatch", 403)

    def authenticate(self, authorization):
        for ingress in (self.primary, self.isolated):
            try:
                ingress.authenticate(authorization)
                return
            except IngressError:
                pass
        raise IngressError("invalid_telephony_authentication", 401)

    def authenticate_message(self, message, authorization):
        call = message.get("call") if isinstance(message, dict) else None
        self.select(call).authenticate(authorization)

    def admit(self, message, authorization, **kwargs):
        return self.select(message.get("call")).admit(message, authorization, **kwargs)

    def end(self, message, authorization, **kwargs):
        return self.select(message.get("call")).end(message, authorization, **kwargs)

    def resolve(self, call, assistant=None, **kwargs):
        return self.select(call).resolve(call, assistant, **kwargs)

    def _identity(self, call, assistant=None, **kwargs):
        return self.select(call)._identity(call, assistant, **kwargs)

    def end_bridge_binding(self, call_id, session_id):
        with self.store.read_connection(.05) as db:
            row = db.execute("SELECT organization_id,phone_number_id FROM telephony_bindings WHERE provider_call_id=? AND session_id=?",
                (call_id, session_id)).fetchone()
        if row is None: raise IngressError("telephony_binding_conflict", 409)
        ingress = self.select({"orgId": row["organization_id"], "phoneNumberId": row["phone_number_id"]})
        return ingress.end_bridge_binding(call_id, session_id)
