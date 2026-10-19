"""One provider-facing JSON schema shape for Vapi and the model gateway."""
from copy import deepcopy


def provider_schema(value):
    definitions = value.get("$defs", {}) if isinstance(value, dict) else {}

    def visit(item, resolving=()):
        if isinstance(item, list):return [visit(entry, resolving) for entry in item]
        if not isinstance(item, dict):return deepcopy(item)
        if "$ref" in item:
            reference = item["$ref"]
            if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
                raise ValueError("Only local schema definitions are supported.")
            key = reference[len("#/$defs/"):]
            if key not in definitions or key in resolving:
                raise ValueError("Missing or recursive schema definition.")
            return {**visit(definitions[key], (*resolving, key)),
                    **visit({name: content for name, content in item.items() if name != "$ref"}, resolving)}
        normalized = {name: visit(content, resolving) for name, content in item.items() if name not in ("title", "$defs")}
        if "const" in normalized:
            normalized["enum"] = [normalized.pop("const")]
        alternatives = normalized.get("anyOf")
        if isinstance(alternatives, list) and len(alternatives) == 2:
            non_null = [entry for entry in alternatives if entry != {"type": "null"}]
            if len(non_null) == 1 and isinstance(non_null[0], dict) and isinstance(non_null[0].get("type"), str):
                normalized.pop("anyOf")
                normalized.update(non_null[0])
                normalized["type"] = [non_null[0]["type"], "null"]
        return normalized

    return visit(value)
