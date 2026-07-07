"""Recommended provider presets (beta.4).

Presets are *recommendations only*. They never restrict manual combinations and a preset is never
auto-applied to a billable provider without the operator explicitly choosing it. Each preset names
a coordinator + coding provider key; guided setup then asks the operator to confirm and supply any
required credential. ``shared_pool`` flags the single-account case (Claude coordinator + Claude
Code coding share one allowance).
"""

from __future__ import annotations

from dataclasses import dataclass

from aithernet.agents import capabilities as caps


@dataclass(frozen=True)
class Preset:
    name: str
    coordinator: str
    coding: str
    shared_pool: bool
    description: str

    def billing_summary(self) -> dict:
        """Per-role billing model so setup states, accurately, who pays for what."""
        return {
            "coordinator": {"provider": self.coordinator,
                            "billing": caps.billing_model(self.coordinator),
                            "auth": caps.auth_category(self.coordinator)},
            "coding": {"provider": self.coding,
                       "billing": caps.billing_model(self.coding),
                       "auth": caps.auth_category(self.coding)},
            "shared_pool": self.shared_pool,
        }


#: The recommended presets. Operators may freely deviate.
PRESETS: dict[str, Preset] = {
    "reasoning_first": Preset(
        "reasoning_first", "claude_cli", "codex_cli", False,
        "Strongest available Claude reasoning as coordinator; Codex (or Claude Code) for coding."),
    "coding_first": Preset(
        "coding_first", "claude_cli", "claude_code", True,
        "Strong Claude coordinator with Claude Code for coding (one shared Claude allowance)."),
    "lowest_cost": Preset(
        "lowest_cost", "gemini_api", "codex_cli", False,
        "A low-cost API coordinator with a subscription coding agent."),
    "fully_local": Preset(
        "fully_local", "openai_compatible_local", "local_coding", False,
        "Local OpenAI-compatible coordinator and a local coding provider — no external account."),
    "provider_diverse": Preset(
        "provider_diverse", "claude_cli", "codex_cli", False,
        "Claude subscription coordinator + Codex subscription coding (independent allowances)."),
    "single_provider_claude": Preset(
        "single_provider_claude", "claude_cli", "claude_code", True,
        "Claude subscription for both roles — one shared capacity pool."),
}


def get_preset(name: str) -> Preset | None:
    return PRESETS.get(name)


def list_presets() -> list[Preset]:
    return list(PRESETS.values())
