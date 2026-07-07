"""Part B: OpenAI-compatible/Ollama coordinator provider diagnostics & compatibility.

All tests use ``httpx.MockTransport`` — they never require a running Ollama. They prove
failures carry actionable, secret-free detail (no more empty ``502``), that response
content in string or block form is accepted, malformed responses are rejected clearly, and
that the narrow ``response_format`` retry fires only when the server explicitly rejects it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from aithernet.config.settings import CoordinatorConfig
from aithernet.coordinator.contracts import CoordinatorInput, CoordinatorProviderError
from aithernet.coordinator.providers.openai_compatible import OpenAICompatibleProvider

_DECISION = json.dumps(
    {
        "summary": "Reviewed node state.",
        "next_target": "respond",
        "action": "acknowledge",
        "message": "Node state summarized.",
        "structured_payload": {},
        "expected_result": "The mission source is informed.",
    }
)


def _config() -> CoordinatorConfig:
    return CoordinatorConfig(
        provider="openai_compatible",
        model="qwen2.5:7b",
        base_url="http://127.0.0.1:11434/v1",
        api_key="ollama",
    )


def _input() -> CoordinatorInput:
    return CoordinatorInput(
        mission_id="m-1",
        mission_content="Summarize the current node status.",
        mission_source_type="user",
        mission_status="received",
        current_time=datetime.now(UTC),
    )


def _completion(content: object) -> dict:
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}


def _decide(handler) -> object:
    provider = OpenAICompatibleProvider(_config(), transport=httpx.MockTransport(handler))
    return asyncio.run(provider.decide(_input()))


# -- transport failures ----------------------------------------------------------


def test_connect_failure_has_nonempty_diagnostic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(handler)
    message = str(exc.value)
    assert "[ConnectError]" in message
    assert "Connection refused" in message
    assert "127.0.0.1:11434" in message  # sanitized endpoint included
    assert not message.rstrip().endswith(":")  # never a blank detail


def test_timeout_is_categorized_even_with_empty_str() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)  # str(exc) == ""

    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(handler)
    message = str(exc.value)
    assert "[ReadTimeout]" in message
    assert "timeout after" in message
    # repr fallback guarantees a non-empty description even when str() is empty.
    assert "ReadTimeout" in message.split("timeout after", 1)[1]


# -- HTTP errors -----------------------------------------------------------------


def test_http_error_with_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "internal boom"}})

    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(handler)
    message = str(exc.value)
    assert "HTTP 500" in message
    assert "127.0.0.1:11434" in message
    assert "application/json" in message
    assert "internal boom" in message


def test_http_error_with_empty_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"")

    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(handler)
    message = str(exc.value)
    assert "HTTP 502" in message
    assert "<empty response body>" in message


def test_secret_is_redacted_from_error_body() -> None:
    secret = "sk-supersecretvalue999"

    def handler(request: httpx.Request) -> httpx.Response:
        # 403 (not 400/422) so the response_format retry is not involved.
        return httpx.Response(403, text=f"Invalid credentials: Bearer {secret} rejected")

    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(handler)
    message = str(exc.value)
    assert secret not in message
    assert "[REDACTED]" in message
    assert "HTTP 403" in message


# -- malformed responses ---------------------------------------------------------


def test_malformed_response_shape_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(handler)
    assert "missing choices[0].message" in str(exc.value)


def test_malformed_decision_json_reports_stage_and_preview() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("this is definitely not json"))

    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(handler)
    message = str(exc.value)
    assert "transport succeeded" in message
    assert "extraction stage" in message
    assert "this is definitely not json" in message


def test_decision_schema_mismatch_reports_validation_stage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Valid JSON object, but not a valid decision (missing required fields).
        return httpx.Response(200, json=_completion(json.dumps({"summary": "only this"})))

    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(handler)
    assert "validation stage" in str(exc.value)


# -- success forms ---------------------------------------------------------------


def test_successful_string_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(_DECISION))

    decision = _decide(handler)
    assert decision.next_target == "respond"
    assert decision.mission_id == "m-1"


def test_successful_block_list_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        blocks = [{"type": "text", "text": _DECISION}]
        return httpx.Response(200, json=_completion(blocks))

    decision = _decide(handler)
    assert decision.next_target == "respond"


# -- narrow response_format retry ------------------------------------------------


def test_retries_without_response_format_when_server_rejects_field() -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "response_format" in body:
            return httpx.Response(
                400, json={"error": {"message": "response_format is not supported"}}
            )
        return httpx.Response(200, json=_completion(_DECISION))

    decision = _decide(handler)
    assert decision.next_target == "respond"
    assert len(calls) == 2  # one rejected, one retried
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]  # retry dropped the field


def test_generic_400_does_not_trigger_retry() -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "bad request, unrelated"}})

    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(handler)
    assert len(calls) == 1  # narrow: no retry for an unrelated 400
    assert "HTTP 400" in str(exc.value)
