"""Small explicit acceptance contract; unknown checks fail validation."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

VARIABLES = {"DATE", "LATER_DATE", "NAME", "PHONE", "VEHICLE", "CORRECTED_VEHICLE"}
COUNTS = {"appointments", "proposals", "pending_proposals", "handoffs", "confirm_calls"}
STATE_FIELDS = COUNTS | {"appointment_status", "handoff_status", "service", "vehicle", "customer_name", "phone"}
TOOL_NAMES = {"get_dealership_info", "begin_service_request", "search_availability", "prepare_first_available_booking",
              "prepare_booking", "confirm_action", "dismiss_proposal", "get_appointment", "get_session_context",
              "get_operation", "prepare_reschedule", "prepare_cancellation", "request_handoff", "withdraw_handoff"}
HASH = re.compile(r"[0-9a-f]{64}")


def template_variables(value):
    """Reject malformed placeholders as well as unknown complete names."""
    if isinstance(value, str):
        names = set(re.findall(r"\{\{(.*?)\}\}", value))
        remaining = re.sub(r"\{\{(.*?)\}\}", "", value)
        if names - VARIABLES or "{{" in remaining or "}}" in remaining:
            raise ValueError("Unknown or malformed template variable")
        return names
    if isinstance(value, list):
        return set().union(*(template_variables(v) for v in value))
    if isinstance(value, dict):
        return set().union(*(template_variables(v) for v in value.values()))
    return set()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def validate_dataset(data):
    def need(ok, message):
        if not ok:
            raise ValueError(message)
    need(isinstance(data, dict) and type(data.get("schema_version")) is int and data["schema_version"] == 1, "Unsupported dataset schema")
    for key in ("dataset_id", "version", "review_status"):
        need(isinstance(data.get(key), str) and bool(data[key]), "Missing dataset identity/review status")
    need(isinstance(data.get("provenance"), dict) and data["provenance"].get("kind") == "authored_synthetic", "Synthetic data required")
    cases = data.get("cases")
    need(isinstance(cases, list) and 0 < len(cases) <= 500, "Empty or oversized dataset")
    ids = set()
    for case in cases:
        need(isinstance(case, dict), "Invalid case")
        cid = case.get("id")
        need(isinstance(cid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", cid) and cid not in ids, "Duplicate/invalid case id")
        ids.add(cid)
        need(isinstance(case.get("split"), str) and case["split"] in {"regression", "dev", "holdout"}, "Invalid split")
        need(isinstance(case.get("criticality"), str) and case["criticality"] in {"critical", "standard"}, "Invalid criticality")
        for key in ("family", "category"):
            need(isinstance(case.get(key), str) and bool(case[key]), "Missing case family/category")
        need(isinstance(case.get("provenance"), dict) and case["provenance"].get("kind") == "authored_synthetic", "Case provenance missing")
        execution = case.get("execution")
        need(isinstance(execution, dict) and isinstance(execution.get("kind"), str) and execution["kind"] in {"model_text", "voice_manual", "ui_contract"}, "Unknown execution kind")
        need(isinstance(case.get("setup"), dict) and case["setup"].get("fixture") == "empty", "Unsupported fixture")
        turns = case.get("turns")
        need(isinstance(turns, list) and 1 <= len(turns) <= 30 and all(isinstance(t, str) and 0 < len(t) <= 10000 for t in turns), "Invalid turns")
        expected = case.get("expected")
        mandatory = {"turn_checks", "final", "required_tools", "forbidden_tools", "semantic_rubric"}
        need(isinstance(expected, dict) and mandatory <= set(expected) and not (set(expected) - mandatory - {"tool_argument_checks"}), "Unknown expectation contract")
        need(isinstance(expected["turn_checks"], list) and isinstance(expected["final"], dict), "Invalid state checks")
        for state in [expected["final"], *expected["turn_checks"]]:
            need(isinstance(state, dict) and not (set(state) - STATE_FIELDS - {"after_turn"}), "Unknown state assertion")
            if state is not expected["final"]:
                n = state.get("after_turn")
                need(type(n) is int and 1 <= n <= len(turns), "Invalid checkpoint turn")
            else:
                need("after_turn" not in state, "Final state cannot select a turn")
            for name, value in state.items():
                if name in COUNTS:
                    need(type(value) is int and value >= 0, "Invalid count assertion")
                elif name != "after_turn":
                    need(isinstance(value, str) and bool(value), "Invalid scalar assertion")
        for key in ("required_tools", "forbidden_tools", "semantic_rubric"):
            need(isinstance(expected[key], list) and all(isinstance(t, str) and t for t in expected[key]), "Invalid rubric/tools")
        need(bool(expected["semantic_rubric"]), "Semantic criteria required")
        need(set(expected["required_tools"]) | set(expected["forbidden_tools"]) <= TOOL_NAMES, "Unknown tool assertion")
        need(not (set(expected["required_tools"]) & set(expected["forbidden_tools"])), "Contradictory tools")
        need(isinstance(expected.get("tool_argument_checks", []), list), "Invalid argument checks")
        for check in expected.get("tool_argument_checks", []):
            need(isinstance(check, dict) and set(check) == {"after_turn", "name", "arguments_subset"}, "Invalid argument check")
            need(type(check["after_turn"]) is int and 1 <= check["after_turn"] <= len(turns), "Invalid argument check turn")
            need(isinstance(check["name"], str) and check["name"] in TOOL_NAMES and isinstance(check["arguments_subset"], dict) and bool(check["arguments_subset"]), "Empty or unknown argument oracle")
        template_variables(case)
    return data


def select_cases(data, split="regression", ids=None):
    if not isinstance(split, str) or split not in {"regression", "dev", "holdout"}:
        raise ValueError("Invalid split")
    rows = [c for c in data["cases"] if c["split"] == split]
    if ids is not None:
        if not isinstance(ids, (list, tuple)) or not ids or any(not isinstance(cid, str) for cid in ids) or len(ids) != len(set(ids)):
            raise ValueError("Selected IDs must be a nonempty list without duplicates")
        requested = set(ids)
        if requested - {c["id"] for c in rows}:
            raise ValueError("Selected IDs must exist in the explicitly selected split")
        rows = [c for c in rows if c["id"] in requested]
    if not rows:
        raise ValueError("Selection cannot be empty")
    return rows


def render_case(case, variables):
    needed = template_variables(case)
    if not isinstance(variables, dict) or needed - set(variables) or set(variables) - VARIABLES:
        raise ValueError("Missing or unknown template values")
    if any(not isinstance(value, str) or "{{" in value or "}}" in value for value in variables.values()):
        raise ValueError("Invalid template value")
    def replace(value):
        if isinstance(value, str):
            return re.sub(r"\{\{(.*?)\}\}", lambda match: variables[match[1]], value)
        if isinstance(value, list):
            return [replace(v) for v in value]
        if isinstance(value, dict):
            return {k: replace(v) for k, v in value.items()}
        return value
    return replace(case)


def grade_case(case, turn_states, tools, completed_turns, *, final_state=None):
    checks = [{"name": "all_scripted_turns_completed", "passed": completed_turns == len(case["turns"])}]
    for expected in case["expected"]["turn_checks"]:
        n = expected["after_turn"]
        actual = turn_states[n - 1] if n <= len(turn_states) else {}
        for key, value in expected.items():
            if key != "after_turn":
                checks.append({"name": f"turn_{n}.{key}", "passed": key in actual and subset(value, actual[key]),
                               "expected": value, "actual": actual.get(key)})
    final = final_state if final_state is not None else turn_states[-1] if turn_states else {}
    for key, value in case["expected"]["final"].items():
        checks.append({"name": "final." + key, "passed": key in final and subset(value, final[key]),
                       "expected": value, "actual": final.get(key)})
    names = [t.get("name") for t in tools]
    successful = {t.get("name") for t in tools if t.get("ok") is True}
    for name in case["expected"]["required_tools"]:
        checks.append({"name": "successful_required_tool." + name, "passed": name in successful})
    for name in case["expected"]["forbidden_tools"]:
        checks.append({"name": "forbidden_tool_absent." + name, "passed": name not in names})
    for oracle in case["expected"].get("tool_argument_checks", []):
        matches = [t for t in tools if t.get("name") == oracle["name"] and t.get("user_turn") == oracle["after_turn"] and t.get("ok") is True]
        checks.append({"name": f'arguments.turn_{oracle["after_turn"]}.{oracle["name"]}',
                       "passed": any(subset(oracle["arguments_subset"], t.get("arguments", {})) for t in matches),
                       "expected": oracle["arguments_subset"], "actual": [t.get("arguments") for t in matches]})
    prepared = {}
    for tool in tools:
        if tool.get("ok") is True and tool.get("name", "").startswith("prepare_"):
            pid = tool.get("result", {}).get("proposal_id")
            if pid:
                prepared[pid] = tool.get("user_turn")
        if tool.get("name") == "confirm_action":
            pid = tool.get("arguments", {}).get("proposal_id")
            checks.append({"name": f'confirmation_requires_prior_turn_proposal.turn_{tool.get("user_turn")}',
                           "passed": pid in prepared and type(prepared[pid]) is int and type(tool.get("user_turn")) is int and prepared[pid] < tool["user_turn"]})
    return checks


def subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k, v in expected.items())
    return type(expected) is type(actual) and expected == actual


def gate(report):
    if not isinstance(report, dict):
        report = {}
    cases = report.get("cases") if isinstance(report.get("cases"), list) else []
    def identities(rows):
        if not isinstance(rows, list) or not rows:
            return None
        values = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or type(row.get("attempt")) is not int or row["attempt"] < 1:
                return None
            values.append((row["id"], row["attempt"]))
        return Counter(values) if len(values) == len(set(values)) else None
    expected, actual = identities(report.get("selection")), identities(cases)
    coverage = expected is not None and expected == actual
    def valid_hash(value):
        return isinstance(value, str) and HASH.fullmatch(value) is not None
    def valid_manifest(value):
        return isinstance(value, dict) and bool(value) and all(isinstance(name, str) and bool(name) and valid_hash(value[name]) for name in value)
    source = report.get("source") if isinstance(report.get("source"), dict) else {}
    dataset = report.get("dataset") if isinstance(report.get("dataset"), dict) else {}
    stable = valid_manifest(source.get("before")) and valid_manifest(source.get("after")) and source["before"] == source["after"]
    stable = stable and valid_hash(dataset.get("sha256")) and valid_hash(dataset.get("sha256_after")) and dataset["sha256"] == dataset["sha256_after"]
    checks_ok = bool(cases) and all(isinstance(c, dict) and c.get("status") == "completed" and isinstance(c.get("checks"), list) and bool(c["checks"]) and all(isinstance(x, dict) and x.get("passed") is True for x in c["checks"]) for c in cases)
    mechanical = coverage and stable and checks_ok
    semantic_pending = [f'{c.get("id")}#{c.get("attempt")}' for c in cases if isinstance(c, dict) and (not isinstance(c.get("semantic_review"), dict) or c["semantic_review"].get("status") != "passed")] if report.get("mode") == "model" else []
    return {
        "selected_coverage_complete": coverage, "source_and_dataset_stable": stable,
        "mechanical_checks_passed": bool(mechanical),
        "semantic_review_pending_or_failed": semantic_pending,
        "execution_result": "passed" if mechanical else "failed_or_incomplete",
        "release_ready": False,
        "release_blockers": ["Dataset requires independent business-policy review.",
                             "Current run is one evaluation layer; acoustic, human and deployment acceptance are separate."]
                             + (["Semantic review is pending or failed."] if semantic_pending else [])
                             + ([] if mechanical else ["Mechanical evidence failed, changed, or is incomplete."]),
    }
