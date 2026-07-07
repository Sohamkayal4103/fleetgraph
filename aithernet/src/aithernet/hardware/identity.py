"""Deterministic stable hardware identity (Stage 14B, Part D).

A device's Aithernet identity must survive node restart and ordinary USB re-enumeration, must
not collide across providers, and must never depend on a transient discovery list index. The
strongest available combination of stable facts forms a ``hardware_key``:

    provider + driver + vendor + product + serial + channel/interface identity

* A device WITH a usable serial gets a strong key from ``provider+driver+serial(+channel)``.
* A device with NO serial gets a deterministic key from the remaining stable facts and is
  flagged ``identity_limited`` — it is still stable across restarts, but two physically distinct
  same-model serial-less devices on the same provider are intentionally indistinguishable
  (recorded honestly as identity-limited rather than guessed apart by enumeration order).
"""

from __future__ import annotations

import hashlib

_KEY_VERSION = "hw1"
#: serial values that are placeholders, not real unit identities.
_TRIVIAL_SERIALS = {"", "0", "00000000", "unknown", "none", "n/a", "na", "000000000000"}


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def is_usable_serial(serial: str | None) -> bool:
    """True when ``serial`` is a real per-unit identity (not a placeholder/blank)."""
    s = normalize_serial(serial)
    return bool(s) and s not in _TRIVIAL_SERIALS


def normalize_serial(serial: str | None) -> str:
    """Normalize a raw serial to a stable, bounded canonical form for identity comparison.

    Lower-cases, trims surrounding whitespace, drops embedded whitespace, and bounds the length.
    This makes the same physical serial reported with different casing/spacing by different tools
    (SoapySDR vs libiio) compare and key identically. It does NOT invent or pad a serial.
    """
    s = (serial or "").strip().lower()
    s = "".join(s.split())
    return s[:128]


def compute_hardware_key(
    *,
    provider_id: str,
    driver: str | None,
    vendor: str | None,
    product: str | None,
    serial: str | None,
    channel: str | None = None,
    transport: str | None = None,
) -> tuple[str, bool]:
    """Return ``(hardware_key, identity_limited)`` for one discovered device.

    The key is a versioned SHA-256 over a canonical, ordered set of normalized stable facts.
    ``identity_limited`` is ``True`` when no usable serial is available, so the record is marked
    honestly rather than relying on enumeration order to tell two serial-less units apart.
    """
    provider = _norm(provider_id)
    drv = _norm(driver)
    chan = _norm(channel)
    if is_usable_serial(serial):
        parts = [provider, drv, normalize_serial(serial), chan]
        identity_limited = False
    else:
        # No usable serial: fall back to the remaining stable discovered facts. This is stable
        # across restarts/re-enumeration but cannot distinguish identical serial-less units.
        parts = [provider, drv, _norm(vendor), _norm(product), _norm(transport), chan]
        identity_limited = True
    canonical = "|".join([_KEY_VERSION, *parts])
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"{provider}:{digest}", identity_limited
