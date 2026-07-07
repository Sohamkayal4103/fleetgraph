"""Cloud Claude/Gemini coordinator adapters: Bedrock + Vertex (beta.4).

These are distinct provider configurations from the direct API providers: Claude on AWS Bedrock,
Claude on Google Vertex AI, and Gemini on Google Vertex AI. They reuse the Anthropic/Gemini request
body + response parsing and only change the endpoint + authentication to the documented cloud
mechanism. Credentials are supplied through the documented cloud path (a bearer token / API key
referenced from the managed secret store), never stored in node.yaml, datasets, logs, artifacts,
peers, Drive, or the portal.

Live cloud qualification is operator-run (the qualification host has no AWS/Google Cloud
credentials); these adapters pass deterministic qualification (registration, capability, readiness,
endpoint construction, error normalization) and are labelled live-pending until an operator runs
them against their own cloud account.
"""

from __future__ import annotations

from typing import ClassVar

from aithernet.coordinator.contracts import CoordinatorInput
from aithernet.coordinator.providers.anthropic import AnthropicProvider
from aithernet.coordinator.providers.gemini_api import GeminiAPIProvider

#: The Anthropic-on-Vertex API version marker (goes in the body; the model is in the URL).
_VERTEX_ANTHROPIC_VERSION = "vertex-2023-10-16"


class AnthropicBedrockProvider(AnthropicProvider):
    """Claude on AWS Bedrock. Auth: a Bedrock bearer credential (documented AWS mechanism)."""

    name: ClassVar[str] = "anthropic_bedrock"

    def check_ready(self) -> list[str]:
        missing: list[str] = []
        if not self.config.model:
            missing.append("model")
        if not self.config.region:
            missing.append("region")
        if not self.config.api_key:
            missing.append("api_key")  # the documented Bedrock bearer credential reference
        return missing

    def _endpoint(self) -> str:
        if self.config.base_url:
            return self.config.base_url.rstrip("/")
        region = self.config.region or "us-east-1"
        return (f"https://bedrock-runtime.{region}.amazonaws.com"
                f"/model/{self.config.model}/invoke")

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key or ''}",
        }

    def _body(self, payload: CoordinatorInput) -> dict:
        body = super()._body(payload)
        body.pop("model", None)  # model is in the URL for Bedrock invoke
        body["anthropic_version"] = "bedrock-2023-05-31"
        return body


class AnthropicVertexProvider(AnthropicProvider):
    """Claude on Google Vertex AI. Auth: a Google Cloud bearer token (documented ADC mechanism)."""

    name: ClassVar[str] = "anthropic_vertex"

    def check_ready(self) -> list[str]:
        missing: list[str] = []
        if not self.config.model:
            missing.append("model")
        if not self.config.project:
            missing.append("project")
        if not self.config.region:
            missing.append("region")
        if not self.config.api_key:
            missing.append("api_key")  # a Google Cloud access token reference
        return missing

    def _endpoint(self) -> str:
        if self.config.base_url:
            return self.config.base_url.rstrip("/")
        region, project = self.config.region or "us-east5", self.config.project
        return (f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}"
                f"/locations/{region}/publishers/anthropic/models/{self.config.model}:rawPredict")

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key or ''}",
        }

    def _body(self, payload: CoordinatorInput) -> dict:
        body = super()._body(payload)
        body.pop("model", None)  # model is in the URL for Vertex rawPredict
        body["anthropic_version"] = _VERTEX_ANTHROPIC_VERSION
        return body


class GeminiVertexProvider(GeminiAPIProvider):
    """Gemini on Google Vertex AI. Auth: a Google Cloud bearer token (documented ADC mechanism)."""

    name: ClassVar[str] = "gemini_vertex"

    def check_ready(self) -> list[str]:
        missing: list[str] = []
        if not self.config.model:
            missing.append("model")
        if not self.config.project:
            missing.append("project")
        if not self.config.region:
            missing.append("region")
        if not self.config.api_key:
            missing.append("api_key")
        return missing

    def _endpoint(self) -> str:
        region, project = self.config.region or "us-central1", self.config.project
        return (f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}"
                f"/locations/{region}/publishers/google/models/{self.config.model}:generateContent")

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key or ''}",
        }
