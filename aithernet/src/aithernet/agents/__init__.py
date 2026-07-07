"""Customer-facing AI provider configuration (Stage 14G, PHASE 6).

A DISTINCT interface from the Stage 7 peer-agent connections and the Stage 14D external-agent
gateway: this configures the node's own *coordinator* (planning LLM) and *coding* agent providers,
detects their official executables, reports honest readiness, and persists the selection to a
protected node config file — never the project tree, never with credential values inline.
"""

from __future__ import annotations

from aithernet.agents import providers

__all__ = ["providers"]
