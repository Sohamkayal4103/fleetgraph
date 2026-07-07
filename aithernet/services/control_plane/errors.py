"""Sanitized control-plane errors — a stable code + HTTP status, never secret values."""

from __future__ import annotations


class ControlPlaneError(Exception):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class AuthError(ControlPlaneError):
    def __init__(self, code: str = "unauthorized") -> None:
        super().__init__(code, status=401)


class ForbiddenError(ControlPlaneError):
    def __init__(self, code: str = "forbidden") -> None:
        super().__init__(code, status=403)


class NotFoundError(ControlPlaneError):
    def __init__(self, code: str = "not_found") -> None:
        super().__init__(code, status=404)


class RateLimitError(ControlPlaneError):
    def __init__(self, code: str = "rate_limited") -> None:
        super().__init__(code, status=429)
