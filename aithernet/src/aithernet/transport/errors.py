"""Agent-transport error types (Stage 13A).

A small, explicit hierarchy so the inbound API and delivery worker can map failures to
sanitized, structured responses without leaking keys, signatures, headers, or environment.
``retryable`` distinguishes transient transport faults (connection/timeout/5xx) from
permanent rejections (bad signature, untrusted/revoked peer, malformed envelope) that must
never be auto-retried.
"""

from __future__ import annotations


class TransportError(Exception):
    """Base class for all agent-transport failures.

    ``code`` is a stable machine string; ``retryable`` marks transient transport faults.
    """

    code: str = "transport_error"
    retryable: bool = False

    def __init__(self, message: str, *, code: str | None = None, retryable: bool | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable


class IdentityError(TransportError):
    """The local node identity is missing, unreadable, or invalid."""

    code = "identity_error"


# -- inbound / verification (permanent) ------------------------------------------


class EnvelopeError(TransportError):
    """A signed envelope is malformed, oversized, or structurally invalid."""

    code = "malformed_envelope"


class ProtocolVersionError(TransportError):
    """The envelope declares an unsupported protocol or version."""

    code = "unsupported_protocol_version"


class SignatureError(TransportError):
    """The envelope/ack signature did not verify against the expected public key."""

    code = "invalid_signature"


class PeerTrustError(TransportError):
    """The sender peer is unknown, untrusted, disabled, or revoked."""

    code = "peer_not_trusted"


class KeyMismatchError(TransportError):
    """The presented public key/fingerprint does not match the trusted peer key."""

    code = "key_mismatch"


class RecipientMismatchError(TransportError):
    """The envelope was addressed to a different node/recipient."""

    code = "recipient_mismatch"


class MeshAuthorizationError(TransportError):
    """The sender is not authorized by mesh membership (beta.3 tenant-isolated mesh).

    Raised AFTER signature verification but BEFORE canonical peer ingress / MissionEngine, when
    the authenticated peer and this node do not share the named mesh (or the named mesh/authority
    is wrong, revoked, or out of scope). This is what enforces cross-tenant isolation: an
    authenticated, key-trusted peer in another tenant is still rejected here unless an explicit
    shared mesh exists.
    """

    code = "mesh_not_authorized"


class ExpiredMessageError(TransportError):
    """The message is expired or outside the accepted clock skew."""

    code = "expired_or_skewed"


class MessageIdCollisionError(TransportError):
    """The same message id arrived with a different canonical hash (tampering)."""

    code = "message_id_collision"


class PayloadTooLargeError(TransportError):
    """The request/response body exceeded the configured size limit."""

    code = "payload_too_large"


# -- outbound transport (transient unless noted) ---------------------------------


class TransportConnectionError(TransportError):
    """A connection-level failure reaching the peer (retryable)."""

    code = "connection_error"
    retryable = True


class TransportTimeoutError(TransportError):
    """The peer did not respond within the configured timeout (retryable)."""

    code = "timeout"
    retryable = True


class TransportHTTPError(TransportError):
    """The peer returned an HTTP error. ``retryable`` depends on the status class."""

    code = "http_error"

    def __init__(self, message: str, *, status_code: int, retryable: bool):
        super().__init__(message, retryable=retryable)
        self.status_code = status_code
