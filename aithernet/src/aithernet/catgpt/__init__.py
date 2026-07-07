"""Aithernet-managed CatGPT Gateway (beta.5, Part B/C).

An optional, Aithernet-managed *local* sidecar that runs a pinned CatGPT-Gateway component and
re-exposes the user's own ChatGPT/Claude web subscription as an OpenAI-compatible endpoint for the
**coordinator** provider only. Aithernet drives it through the local OpenAI-compatible API; it never
stores the browser session, cookies, or the user's web credentials — the user logs in themselves in
the local browser (noVNC).

This package is a *wrapper/sidecar* (not a fork): it renders an Aithernet-authored compose + env,
manages the container lifecycle, binds the API to loopback by default, and stores the local bearer
token via Aithernet's managed secret store. The upstream project (CatGPT-Gateway, MIT licensed) is
run as a pinned local component — see ``NOTICE`` / ``docs/third-party/catgpt-gateway``.
"""

from aithernet.catgpt.contract import (
    GATEWAY_CONTRACT_ENDPOINTS,
    GatewayStatus,
    classify_gateway_status,
    verify_contract,
)
from aithernet.catgpt.manager import (
    CatGptGatewayManager,
    DockerRunner,
    GatewayConfig,
    RunResult,
    state_dir,
)

__all__ = [
    "CatGptGatewayManager",
    "DockerRunner",
    "GatewayConfig",
    "RunResult",
    "state_dir",
    "GatewayStatus",
    "classify_gateway_status",
    "verify_contract",
    "GATEWAY_CONTRACT_ENDPOINTS",
]
