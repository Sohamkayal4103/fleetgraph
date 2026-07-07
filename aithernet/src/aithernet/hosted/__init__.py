"""Node-side hosted integration (Stage 14F).

The local node makes OUTBOUND authenticated connections to the hosted control plane — enrollment,
heartbeat, config and release checks. It never exposes its local API publicly and never stores a
website password or session cookie. Requests are signed with the node's own Ed25519 identity (the
same key the transport uses), so no long-lived secret is transmitted per request. Loss of Internet
access never blocks supported local missions.
"""

from __future__ import annotations
