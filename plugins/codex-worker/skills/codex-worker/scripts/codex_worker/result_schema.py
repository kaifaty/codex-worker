"""Structured worker and review result contracts."""
import json
import re


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


RESULT_SCHEMA = object_schema({
    "status": {"type": "string", "enum": ["done", "partial", "blocked", "needs_decision"]},
    "summary": {"type": "string"},
    "checks": {"type": "array", "items": object_schema({
        "command": {"type": "string"},
        "outcome": {"type": "string", "enum": ["passed", "failed", "not_run"]},
        "details": {"type": "string", "description": "Include numerical evidence: counts, exits, attempts."},
    })},
    "pr": {"type": "string", "description": "Compatibility field; always return an empty string."},
    "pr_title": {"type": "string", "description": "PR title for the supervisor to publish. Empty uses the last commit subject; a new PR first uses the latest nonempty title from this task's earlier results."},
    "pr_body": {"type": "string", "description": "Description of the entire PR. Empty leaves an existing PR unchanged; a new PR uses the latest nonempty body from this task's earlier results, then summary."},
    "commit_message": {"type": "string", "description": "Repository-style commit message; first line is the subject. Empty if already committed or unchanged."},
    "questions": {"type": "array", "items": {"type": "string"}},
    "remaining": {"type": "array", "items": {"type": "string"}},
    "findings": {"type": "array", "items": object_schema({
        "id": {"type": "string", "pattern": "^F[1-9][0-9]*$"},
        "decision": {"type": "string", "enum": ["fixed", "rejected"]},
        "note": {"type": "string"},
    })},
})
REVIEW_SCHEMA = object_schema({
    "verdict": {"type": "string", "enum": ["clean", "defects"]},
    "findings": {"type": "array", "items": object_schema({
        "id": {"type": "string", "pattern": "^F[1-9][0-9]*$"},
        "severity": {"type": "string", "enum": ["P1", "P2", "P3"]},
        "file": {"type": "string"},
        "line": {"type": "integer", "minimum": 1},
        "title": {"type": "string"},
        "detail": {"type": "string"},
        "verify": {"type": "string"},
    })},
    "summary": {"type": "string"},
})


def matches_schema(value, schema):
    """Validate the small JSON Schema subset used by our output contracts."""
    kind = schema["type"]
    if kind == "object":
        return (isinstance(value, dict) and set(value) == set(schema["required"])
                and all(matches_schema(value[key], field) for key, field in schema["properties"].items()))
    if kind == "array":
        return isinstance(value, list) and all(matches_schema(item, schema["items"]) for item in value)
    if kind == "integer":
        return type(value) is int and value >= schema.get("minimum", value)
    if kind == "string":
        return (isinstance(value, str) and ("enum" not in schema or value in schema["enum"])
                and ("pattern" not in schema or re.fullmatch(schema["pattern"], value) is not None))
    return False


def parse_result(message, reviewing=False):
    try:
        value = json.loads(message)
    except (ValueError, TypeError):
        return None
    return value if matches_schema(value, REVIEW_SCHEMA if reviewing else RESULT_SCHEMA) else None


def result_lines(result, raw="(no final message)"):
    if result is None:
        return ["== result: unknown (unstructured final message)", raw]
    label = {"done": "done", "partial": "PARTIAL", "blocked": "BLOCKED", "needs_decision": "NEEDS DECISION"}
    lines = ["== result: " + label[result["status"]], result["summary"]]
    for check in result["checks"]:
        lines.append(f"- {check['command']}: {check['outcome']} — {check['details']}")
    if result["questions"]:
        lines.append("Questions:")
        lines.extend(f"{n}. {question}" for n, question in enumerate(result["questions"], 1))
    if result["remaining"]:
        lines.append("Remaining:")
        lines.extend("- " + item for item in result["remaining"])
    return lines
