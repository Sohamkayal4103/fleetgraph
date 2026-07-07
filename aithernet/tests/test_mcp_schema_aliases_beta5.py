"""beta.5: MCP schema-call robustness for the exact beta.4 GNU Radio mistakes.

During beta.4 fallback the coordinator repeatedly guessed wrong field names and burned scarce
gnuradio_mcp actions:
  * make_block called with `key` instead of `block_name`
  * get_block_params called with `block_id` instead of `block_name`
  * connect_blocks called with source_block/source_port/sink_block/sink_port instead of the
    *_block_name / *_port_name fields

The preflight now canonicalizes these unambiguous aliases BEFORE dispatch (so the call succeeds),
while still rejecting genuinely-invalid calls with a structured correction.
"""

from __future__ import annotations

from types import SimpleNamespace

from aithernet.mcp.schema_guard import preflight_tool_call


def _tool(name, properties, required, *, additional=True):
    schema = {"type": "object", "properties": properties, "required": required}
    if additional is False:
        schema["additionalProperties"] = False
    return SimpleNamespace(name=name, input_schema=schema)


MAKE_BLOCK = _tool("make_block", {"block_name": {"type": "string"}}, ["block_name"])
GET_PARAMS = _tool("get_block_params", {"block_name": {"type": "string"}}, ["block_name"])
CONNECT = _tool(
    "connect_blocks",
    {"source_block_name": {"type": "string"}, "sink_block_name": {"type": "string"},
     "source_port_name": {"type": "string"}, "sink_port_name": {"type": "string"}},
    ["source_block_name", "sink_block_name", "source_port_name", "sink_port_name"])


def test_make_block_key_alias_is_canonicalized():
    res = preflight_tool_call(MAKE_BLOCK, "make_block", {"key": "analog_sig_source_x"})
    assert res.ok is True
    assert res.arguments == {"block_name": "analog_sig_source_x"}
    assert res.canonicalized == {"key": "block_name"}


def test_make_block_block_id_alias_is_canonicalized():
    res = preflight_tool_call(MAKE_BLOCK, "make_block", {"block_id": "blocks_throttle"})
    assert res.ok is True and res.arguments == {"block_name": "blocks_throttle"}


def test_get_block_params_block_id_alias_is_canonicalized():
    res = preflight_tool_call(GET_PARAMS, "get_block_params", {"block_id": "sig0"})
    assert res.ok is True
    assert res.arguments == {"block_name": "sig0"}
    assert res.canonicalized == {"block_id": "block_name"}


def test_connect_blocks_all_four_aliases_are_canonicalized():
    res = preflight_tool_call(CONNECT, "connect_blocks", {
        "source_block": "sig0", "source_port": "0", "sink_block": "thr0", "sink_port": "0"})
    assert res.ok is True
    assert res.arguments == {
        "source_block_name": "sig0", "source_port_name": "0",
        "sink_block_name": "thr0", "sink_port_name": "0"}
    assert res.canonicalized == {
        "source_block": "source_block_name", "source_port": "source_port_name",
        "sink_block": "sink_block_name", "sink_port": "sink_port_name"}


def test_correct_field_names_pass_through_unchanged():
    res = preflight_tool_call(MAKE_BLOCK, "make_block", {"block_name": "blocks_head"})
    assert res.ok is True and res.arguments == {"block_name": "blocks_head"}
    assert not res.canonicalized


def test_missing_required_field_still_rejected_with_correction():
    res = preflight_tool_call(CONNECT, "connect_blocks", {"source_block": "sig0"})
    assert res.ok is False
    assert "sink_block_name" in res.correction  # names what is still missing
    assert res.missing_required  # some required fields remain unfilled


def test_alias_never_overwrites_a_real_value():
    # If the canonical field is already present, an alias must NOT clobber it.
    res = preflight_tool_call(
        MAKE_BLOCK, "make_block", {"block_name": "real", "key": "guess"})
    assert res.arguments["block_name"] == "real"


def test_unknown_field_rejected_when_additional_properties_false():
    strict = _tool("make_block", {"block_name": {"type": "string"}}, ["block_name"],
                   additional=False)
    res = preflight_tool_call(strict, "make_block", {"block_name": "x", "totally_unknown": 1})
    assert res.ok is False and "totally_unknown" in res.correction
