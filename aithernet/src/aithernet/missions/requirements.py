"""beta.7 (FIX 8) — deterministic detection of explicit tool requirements in a mission prompt.

The node never *invents* a requirement: only clear, explicit language ("must use MCP", "MCP
validation required", "before implementation use RF-MCP", "do not silently fallback") makes a tool
mandatory. Soft language ("where useful", "if helpful", "optional") keeps the tool OPTIONAL — the
mission may skip it, but the final summary still records whether it was used (tool accountability).
"""
from __future__ import annotations

#: Substrings that name the GNU Radio / RF-MCP tool path.
_MCP_TERMS = ("rf-mcp", "gnuradio mcp", "gnu radio mcp", "gnuradio_mcp", " mcp", "mcp ", "mcp.")

#: Language that makes a named tool MANDATORY.
_REQUIRE_TERMS = (
    "must use", "must call", "must be used", "must validate", "required", "require ",
    "mandatory", "before implementation", "before implementing", "before you implement",
    "mcp validation required", "validate with mcp", "validate using mcp",
)

#: Language that keeps a named tool OPTIONAL (overrides a weak require match).
_OPTIONAL_TERMS = (
    "where useful", "if useful", "where helpful", "if helpful", "optional", "may use",
    "you may", "if appropriate", "where appropriate", "if available",
)

#: Language forbidding a silent fallback.
_NO_FALLBACK_TERMS = (
    "do not silently fallback", "do not silently fall back", "do not fallback",
    "do not fall back", "no fallback", "must not fallback", "without fallback",
    "no silent fallback",
)


def _mentions_mcp(low: str) -> bool:
    if "rf-mcp" in low or "gnuradio mcp" in low or "gnu radio mcp" in low or "gnuradio_mcp" in low:
        return True
    # a bare "mcp" token (word-ish), not part of another word
    for tok in low.replace("/", " ").replace(",", " ").replace(".", " ").split():
        if tok.strip("()[]:;") == "mcp":
            return True
    return False


def parse_tool_requirements(text: str) -> dict:
    """Return ``{mentions_mcp, mcp_required, mcp_before_implementation, no_fallback}``.

    ``mcp_required`` is True only when the prompt both names the MCP/GNU-Radio tool path AND uses
    mandatory language AND does not qualify it as optional.
    """
    low = (text or "").lower()
    mentions = _mentions_mcp(low)
    has_require = any(t in low for t in _REQUIRE_TERMS)
    has_optional = any(t in low for t in _OPTIONAL_TERMS)
    no_fallback = any(t in low for t in _NO_FALLBACK_TERMS)
    before_impl = "before implementation" in low or "before implementing" in low \
        or "before you implement" in low
    # a no-fallback directive alongside an MCP mention is itself a hard requirement
    mcp_required = mentions and ((has_require and not has_optional) or no_fallback)
    return {
        "mentions_mcp": mentions,
        "mcp_required": bool(mcp_required),
        "mcp_before_implementation": bool(before_impl),
        "no_fallback": bool(no_fallback),
    }
