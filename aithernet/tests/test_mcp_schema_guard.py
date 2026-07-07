"""beta.10 Defect 5: MCP tool-call preflight against the authoritative schema.

The coordinator must never guess tool parameters when an authoritative schema exists. These tests
prove unknown fields are rejected before dispatch, missing required fields are identified, known
aliases are canonicalized (the make_block block_type/block_id → block_name case the model hit in
beta.9), the schema digest is stable, and tools without a schema pass through unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aithernet.mcp.schema_guard import preflight_tool_call, schema_digest

_MAKE_BLOCK = SimpleNamespace(
    name="make_block",
    input_schema={
        "type": "object",
        "properties": {"block_name": {"type": "string"}, "parameters": {"type": "object"}},
        "required": ["block_name"],
        "additionalProperties": False,
    },
)


def test_valid_call_passes():
    r = preflight_tool_call(_MAKE_BLOCK, "make_block", {"block_name": "blocks_throttle"})
    assert r.ok and r.schema_present
    assert r.arguments == {"block_name": "blocks_throttle"}


def test_alias_canonicalization_block_type_to_block_name():
    # The exact beta.9 failure: model guessed block_type/block_id instead of block_name.
    r = preflight_tool_call(_MAKE_BLOCK, "make_block", {"block_type": "blocks_throttle"})
    assert r.ok, r.correction
    assert r.arguments == {"block_name": "blocks_throttle"}
    assert r.canonicalized == {"block_type": "block_name"}


def test_unknown_field_rejected_before_dispatch():
    r = preflight_tool_call(_MAKE_BLOCK, "make_block",
                            {"block_name": "x", "id": "y", "params": {}})
    # id -> block_name is an alias but block_name already present, so id stays unknown -> rejected.
    assert not r.ok
    assert "id" in r.unknown_fields
    assert "unknown field" in r.correction
    assert "block_name" in r.correction  # the correction lists allowed fields


def test_missing_required_field_identified():
    r = preflight_tool_call(_MAKE_BLOCK, "make_block", {"parameters": {}})
    assert not r.ok
    assert r.missing_required == ["block_name"]
    assert "missing required" in r.correction


def test_type_error_detected():
    r = preflight_tool_call(_MAKE_BLOCK, "make_block", {"block_name": 123})
    assert not r.ok
    assert any("block_name" in e for e in r.type_errors)


def test_no_schema_passes_through_unchanged():
    tool = SimpleNamespace(name="freeform", input_schema=None)
    r = preflight_tool_call(tool, "freeform", {"anything": 1, "goes": True})
    assert r.ok and not r.schema_present
    assert r.arguments == {"anything": 1, "goes": True}


def test_schema_digest_is_stable_and_distinct():
    d1 = schema_digest(_MAKE_BLOCK.input_schema)
    d2 = schema_digest(_MAKE_BLOCK.input_schema)
    assert d1 == d2 and d1.startswith("sha256:")
    assert schema_digest({"type": "object", "properties": {}}) != d1


def test_runtime_raises_schema_validation_error(monkeypatch):
    # The runtime helper raises a distinct, correction-bearing error for an invalid call.
    from aithernet.mcp.contracts import MCPSchemaValidationError
    from aithernet.orchestrator.runtime import NodeRuntime

    fake = SimpleNamespace(mcp=SimpleNamespace(session=SimpleNamespace(_tools=[_MAKE_BLOCK])))
    with pytest.raises(MCPSchemaValidationError) as ei:
        NodeRuntime._preflight_tool_arguments(fake, "make_block", {"bogus": 1})
    assert ei.value.tool_name == "make_block"
    assert ei.value.schema_digest.startswith("sha256:")
    # A valid call returns canonicalized args.
    args = NodeRuntime._preflight_tool_arguments(fake, "make_block", {"block_type": "x"})
    assert args == {"block_name": "x"}
