"""beta.10 Defect 5: deterministic MCP tool-call preflight against the authoritative tool schema.

When an MCP server advertises a JSON ``input_schema`` for a tool, the coordinator must never be
allowed to guess parameter names. This module validates every proposed tool call BEFORE dispatch:
it canonicalizes well-known argument aliases (e.g. ``block_type``/``block_id`` → ``block_name`` for
``make_block``), rejects unknown fields, identifies missing required fields, and returns a
normalized correction the coordinator can act on — plus the schema digest for the action record.

It is conservative: when no authoritative schema exists for a tool, the call passes through
unchanged (we never invent constraints).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

#: Per-tool argument alias canonicalization. Maps an alias the model commonly guesses to the
#: schema's real field name. Applied only when the canonical field is in the tool's schema and the
#: alias is not (so we never overwrite a legitimately-provided value). Unambiguous aliases only.
TOOL_ALIASES: dict[str, dict[str, str]] = {
    "make_block": {
        "block_type": "block_name",
        "block_id": "block_name",
        "key": "block_name",          # beta.5: the exact beta.4 mistake (key vs block_name)
        "id": "block_name",
        "type": "block_name",
        "name": "block_name",
        "params": "parameters",
        "props": "parameters",
    },
    # beta.5: the coordinator repeatedly guessed these wrong during beta.4 fallback, consuming
    # scarce mcp actions. The gr-mcp schema fields are block_name / *_block_name / *_port_name.
    "get_block_params": {
        "block_id": "block_name",     # beta.4 mistake
        "id": "block_name",
        "block": "block_name",
        "block_type": "block_name",
        "name": "block_name",
    },
    "remove_block": {
        "block_id": "block_name", "id": "block_name", "block": "block_name", "name": "block_name",
    },
    "validate_block": {
        "block_id": "block_name", "id": "block_name", "block": "block_name", "name": "block_name",
    },
    "set_block_params": {
        "block_id": "block_name", "id": "block_name", "block": "block_name", "name": "block_name",
        "params": "params", "parameters": "params", "props": "params",
    },
    "connect_blocks": {
        "source_block": "source_block_name",   # beta.4 mistake
        "src_block": "source_block_name",
        "source_port": "source_port_name",     # beta.4 mistake
        "src_port": "source_port_name",
        "sink_block": "sink_block_name",        # beta.4 mistake
        "dst_block": "sink_block_name",
        "dest_block": "sink_block_name",
        "sink_port": "sink_port_name",          # beta.4 mistake
        "dst_port": "sink_port_name",
        "dest_port": "sink_port_name",
    },
}

_JSON_TYPES = {
    "string": str, "integer": int, "number": (int, float), "boolean": bool,
    "object": dict, "array": list, "null": type(None),
}


def schema_digest(input_schema: dict | None) -> str:
    """A stable sha256 digest of a tool's input schema (for the action record)."""
    canonical = json.dumps(input_schema or {}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class PreflightResult:
    ok: bool
    tool_name: str
    arguments: dict                       # canonicalized arguments to actually dispatch
    schema_present: bool
    schema_digest: str
    canonicalized: dict = field(default_factory=dict)  # alias -> canonical applied
    missing_required: list = field(default_factory=list)
    unknown_fields: list = field(default_factory=list)
    type_errors: list = field(default_factory=list)
    correction: str = ""                  # normalized, coordinator-facing correction message


def _canonicalize(tool_name: str, arguments: dict, properties: dict) -> tuple[dict, dict]:
    """Rename well-known aliases to their schema field when unambiguous. Returns (args, applied)."""
    aliases = TOOL_ALIASES.get(tool_name, {})
    if not aliases:
        return dict(arguments), {}
    out = dict(arguments)
    applied: dict = {}
    for alias, canonical in aliases.items():
        if alias in out and canonical in properties and canonical not in out:
            out[canonical] = out.pop(alias)
            applied[alias] = canonical
    return out, applied


def preflight_tool_call(tool_info, tool_name: str, arguments: dict) -> PreflightResult:
    """Validate + canonicalize a proposed tool call against its authoritative schema.

    ``tool_info`` is the cached MCPToolInfo (or None). When it carries no object ``input_schema``,
    the call passes through unchanged (schema_present=False)."""
    schema = getattr(tool_info, "input_schema", None) if tool_info is not None else None
    digest = schema_digest(schema)
    if not isinstance(schema, dict) or schema.get("type") not in (None, "object") \
            or "properties" not in schema:
        return PreflightResult(ok=True, tool_name=tool_name, arguments=dict(arguments or {}),
                               schema_present=False, schema_digest=digest)

    properties: dict = schema.get("properties") or {}
    required: list = list(schema.get("required") or [])
    additional = schema.get("additionalProperties", True)
    args, applied = _canonicalize(tool_name, dict(arguments or {}), properties)

    missing = [f for f in required if f not in args]
    unknown = [k for k in args if k not in properties] if additional is False else []
    type_errors = []
    for k, v in args.items():
        spec = properties.get(k)
        if isinstance(spec, dict) and "type" in spec:
            py = _JSON_TYPES.get(spec["type"])
            # bool is a subclass of int — guard against accepting it for integer/number.
            numeric = py in (int, (int, float))
            if py and (not isinstance(v, py) or (numeric and isinstance(v, bool))):
                type_errors.append(f"{k} (expected {spec['type']})")

    ok = not (missing or unknown or type_errors)
    correction = ""
    if not ok:
        parts = []
        if missing:
            parts.append(f"missing required field(s): {', '.join(missing)}")
        if unknown:
            parts.append(f"unknown field(s) not in the tool schema: {', '.join(unknown)}")
        if type_errors:
            parts.append(f"wrong type for: {', '.join(type_errors)}")
        allowed = ", ".join(sorted(properties)) or "(none)"
        correction = (f"Tool '{tool_name}' rejected before execution — " + "; ".join(parts)
                      + f". Allowed fields: {allowed}. Required: "
                      + (", ".join(required) or "(none)") + ".")
    return PreflightResult(
        ok=ok, tool_name=tool_name, arguments=args, schema_present=True, schema_digest=digest,
        canonicalized=applied, missing_required=missing, unknown_fields=unknown,
        type_errors=type_errors, correction=correction)
