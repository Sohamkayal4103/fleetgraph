"""Guided customer installation (``aithernet setup``) — Stage 14G, PHASE 4A.

A resumable, idempotent setup state machine plus customer-facing hardware profiles. See
:mod:`aithernet.provisioning.wizard` for the step machine and :mod:`aithernet.provisioning.profiles`
for the profile catalogue.
"""

from __future__ import annotations

from aithernet.provisioning import profiles, services, state, wizard

__all__ = ["profiles", "services", "state", "wizard"]
