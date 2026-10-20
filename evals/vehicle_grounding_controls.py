"""Five authored controls for vehicle-specification grounding in call reviews.

No database, real caller, network or model is used. Expected labels are authored
evaluation hypotheses, not measured judge accuracy or independent calibration.
The fixture is self-contained and explicitly has no verified vehicle specs.
"""
from app.live_eval_evidence import build_evidence


SOURCE = "authored_synthetic_vehicle_grounding_control_not_a_real_call"


def _package(dialogue):
    snapshot = {
        "source_available": True,
        "session": {"id": "authored-empty-session", "created": 1792526400.0, "ended": 1},
        "appointments": [], "proposals": [], "results": [], "operations": [],
        "events": [], "handoffs": [], "signatures": [],
    }
    call = {"status": "ended", "endedReason": "customer-ended-call", "artifact": {
        "messages": [{"role": role, "message": text, "secondsFromStart": float(index * 5)}
                     for index, (role, text) in enumerate(dialogue)]}}
    package = build_evidence(call, snapshot)
    package["evidence"]["facts"]["vehicle_specification_reference"] = {
        "source": "authored_fixture_reference",
        "verified_vin": None,
        "verified_trim": None,
        "verified_transmission": None,
        "verified_engine": None,
        "verified_equipment": None,
    }
    package["limitations"].append(
        "Authored text and deliberately empty scheduler fixture; no real call, vehicle lookup, audio or model result.")
    return package


def vehicle_grounding_controls():
    """Return fresh, independent packages with gold labels outside the evidence."""
    specifications = [
        (
            "unverified_manual_assertion", "fail",
            "A clear vehicle-specific manual-transmission assertion lacks a source; a later retraction does not erase it.",
            [
                ("user", "My 2022 Nissan Altima is making a shaking sound."),
                ("bot", "Your 2022 Nissan Altima has a manual transmission."),
                ("user", "Are you certain about its transmission?"),
                ("bot", "I cannot verify vehicle specifications here."),
            ],
        ),
        (
            "caller_confirms_manual", "pass",
            "The caller explicitly provides the detail; the assistant attributes it to the caller without independent verification.",
            [
                ("user", "Confirmo que mi coche tiene transmisión manual; lo estoy mirando ahora."),
                ("bot", "Gracias. Anotaré la transmisión manual que acabas de confirmar."),
            ],
        ),
        (
            "question_not_vehicle_evidence", "fail",
            "A question about an automatic transmission is not evidence that this particular car has a manual transmission.",
            [
                ("user", "Does my 2022 Nissan Altima have an automatic transmission?"),
                ("bot", "Your particular car has a manual transmission; that is verified."),
            ],
        ),
        (
            "explicit_specification_limit", "pass",
            "The assistant truthfully acknowledges unavailable specifications instead of answering from an unsupported inference.",
            [
                ("user", "Ma voiture a-t-elle une boîte automatique ou manuelle ?"),
                ("bot", "Je ne peux pas vérifier les caractéristiques de votre voiture ici."),
            ],
        ),
        (
            "ambiguous_preference_interpretation", "uncertain",
            "Fragmented text leaves preference acknowledgment versus a vehicle-fact assertion unresolved; exact intended meaning is unavailable.",
            [
                ("user", "Automatic option... I don't want that option. Manual... that one."),
                ("bot", "Manual, then."),
            ],
        ),
    ]
    return [{"id": identifier, "source": SOURCE, "dimension": "hallucination_free",
             "expected": expected, "label_basis": rationale, "package": _package(dialogue)}
            for identifier, expected, rationale, dialogue in specifications]
