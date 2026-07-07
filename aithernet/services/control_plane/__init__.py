"""Aithernet hosted control plane (Stage 14F).

A SEPARATELY runnable hosted service — it never imports or starts the local node runtime. It owns
account/tenant/invitation/policy management, one-time signed node enrollment, fleet heartbeat and
visibility, the release/download channel, and the support workflow. It has its own configuration,
database, explicit migration lifecycle, readiness, diagnostics, authorization boundary and
web-facing API, distinct from both the node and the Stage 14E ingestion service.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: One authoritative early-access version source (Stage 14F §48). Package, API metadata, release
#: manifest and portal display all read this.
__version__ = "0.8.0-beta.1"
