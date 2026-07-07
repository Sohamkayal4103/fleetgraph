"""Customer-facing hardware profiles for the guided setup wizard (Stage 14G, PHASE 4A).

The wizard speaks in customer-facing profile names (``software-only``, ``plutosdr`` …). Each maps
to an internal profile recognised by :mod:`aithernet.hardware.setup` (``core``, ``pluto`` …). The
older internal names remain valid as **compatibility aliases** so existing automation keeps working.

This module performs no I/O and installs nothing — it only describes profiles and resolves names.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HardwareProfile:
    """A customer-facing hardware profile and the internal profile it resolves to."""

    name: str                       # customer-facing name, e.g. "plutosdr"
    internal: str                   # internal aithernet.hardware.setup profile, e.g. "pluto"
    summary: str
    needs_hardware: bool            # True if a physical SDR is expected for full use
    runtime_libs: tuple[str, ...] = ()   # the RF runtime path(s) this profile exercises
    aliases: tuple[str, ...] = ()        # additional names that resolve to this profile


#: Ordered customer-facing profiles. ``internal`` must be a key of
#: :data:`aithernet.hardware.setup.PROFILES`.
PROFILES: tuple[HardwareProfile, ...] = (
    HardwareProfile(
        name="software-only", internal="core",
        summary="RF MCP runtime + GNU Radio, no vendor SDR drivers. Software development only.",
        needs_hardware=False, runtime_libs=("gnuradio",), aliases=("core",),
    ),
    HardwareProfile(
        name="simulation", internal="simulation",
        summary="Core plus file/null sources for no-hardware mission development and CI.",
        needs_hardware=False, runtime_libs=("gnuradio",),
    ),
    HardwareProfile(
        name="plutosdr", internal="pluto",
        summary="ADALM-PLUTO: libiio + SoapyPlutoSDR + GNU Radio IIO (receive-only).",
        needs_hardware=True, runtime_libs=("libiio", "soapysdr", "gr-iio"),
        aliases=("pluto",),
    ),
    HardwareProfile(
        name="usrp", internal="usrp",
        summary="Ettus USRP: UHD runtime, utilities and device images (receive-only).",
        needs_hardware=True, runtime_libs=("uhd", "soapysdr"),
    ),
    HardwareProfile(
        name="generic-soapy", internal="soapy-generic",
        summary="Generic SoapySDR runtime + tools + GNU Radio Soapy integration.",
        needs_hardware=True, runtime_libs=("soapysdr", "gr-soapy"),
        aliases=("soapy-generic",),
    ),
    HardwareProfile(
        name="full-lab", internal="full",
        summary="Everything: Pluto + USRP + generic Soapy + GNU Radio integrations.",
        needs_hardware=True, runtime_libs=("libiio", "uhd", "soapysdr", "gr-iio", "gr-soapy"),
        aliases=("full",),
    ),
)

#: name/alias (lower-case) -> HardwareProfile
_INDEX: dict[str, HardwareProfile] = {}
for _p in PROFILES:
    _INDEX[_p.name] = _p
    for _a in _p.aliases:
        _INDEX[_a] = _p


def profile_names() -> list[str]:
    """The canonical customer-facing profile names, in display order."""
    return [p.name for p in PROFILES]


def all_accepted_names() -> list[str]:
    """Every accepted name including compatibility aliases (for validation/help)."""
    return sorted(_INDEX)


def resolve(name: str) -> HardwareProfile:
    """Resolve a customer-facing name OR a compatibility alias to a :class:`HardwareProfile`.

    Raises ``KeyError`` with an actionable message for an unknown name.
    """
    key = (name or "").strip().lower()
    if key not in _INDEX:
        raise KeyError(
            f"unknown hardware profile '{name}'. Choose one of: {', '.join(profile_names())} "
            f"(aliases also accepted: {', '.join(sorted(set(_INDEX) - set(profile_names())))})"
        )
    return _INDEX[key]


def internal_name(name: str) -> str:
    """Resolve to the internal :mod:`aithernet.hardware.setup` profile name."""
    return resolve(name).internal
