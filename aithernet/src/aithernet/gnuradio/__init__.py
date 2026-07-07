"""Structured GNU Radio workspace context (Stage 11B).

Centralizes GNU Radio-specific interpretation of MCP tool results in one place (the
:class:`~aithernet.gnuradio.context.GNURadioContextService`) rather than spreading
tool-name conditionals through the orchestrator and UI. It stores compact, never-fabricated
summaries derived only from successful, parsed tool results, referencing large raw results
in the MCP-call persistence layer. It recognizes known gr-mcp tool semantics only after
confirming the relevant tool is present in the dynamically discovered live catalog.
"""

from __future__ import annotations

from aithernet.gnuradio.context import GNURadioContextService
from aithernet.gnuradio.contracts import (
    FlowgraphStatus,
    GNURadioContext,
    GNURadioContextRefresh,
)

__all__ = [
    "FlowgraphStatus",
    "GNURadioContext",
    "GNURadioContextRefresh",
    "GNURadioContextService",
]
