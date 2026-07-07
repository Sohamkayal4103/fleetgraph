"""External-agent connection interface (Stage 7).

This package is the local node's connection layer for *external agents* — outside parties
(a user's private agent, a manager agent, another node's coordinator, a lab orchestrator,
or a local/cloud process) that connect to the node as message/mission sources. It is NOT
a peer-mesh protocol: the node persists connections and messages, may create a mission
from a message, and may run exactly one explicit mission step on request. It never opens
outgoing sessions, polls ``endpoint_url``, or runs autonomous loops — those are later
stages.
"""

from __future__ import annotations

from aithernet.communication.contracts import (
    CommunicationError,
    ExternalAgentCreate,
    ExternalAgentDisabledError,
    ExternalAgentMessageCreate,
    ExternalAgentMessageRead,
    ExternalAgentMessageResponse,
    ExternalAgentNotFoundError,
    ExternalAgentRead,
    ExternalAgentStatus,
    ExternalAgentStatusUpdate,
    ExternalAgentStatusValue,
    ExternalAgentValidationError,
    MessageDirection,
)
from aithernet.communication.runtime import CommunicationRuntime

__all__ = [
    "CommunicationError",
    "CommunicationRuntime",
    "ExternalAgentCreate",
    "ExternalAgentDisabledError",
    "ExternalAgentMessageCreate",
    "ExternalAgentMessageRead",
    "ExternalAgentMessageResponse",
    "ExternalAgentNotFoundError",
    "ExternalAgentRead",
    "ExternalAgentStatus",
    "ExternalAgentStatusUpdate",
    "ExternalAgentStatusValue",
    "ExternalAgentValidationError",
    "MessageDirection",
]
