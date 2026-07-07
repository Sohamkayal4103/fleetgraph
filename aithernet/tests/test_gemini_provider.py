"""Part J: the Gemini CLI coordinator provider.

A fake ``gemini`` script emulates the locally observed CLI JSON shape
(``{session_id, response, stats:{models, tools:{totalCalls, byName}}}``). It answers
``--version`` (so the identity probe passes) and, headless, reads the prompt from stdin and
prints a configurable JSON document. No real Gemini CLI, network, or Google account is
required.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aithernet.config.settings import CoordinatorConfig
from aithernet.coordinator.contracts import (
    CoordinatorConfigurationError,
    CoordinatorInput,
    CoordinatorProviderError,
)
from aithernet.coordinator.providers import build_provider
from aithernet.coordinator.providers.gemini_cli import GeminiCLIProvider

_VALID_DECISION = {
    "summary": "Reviewed the node state.",
    "next_target": "respond",
    "action": "acknowledge",
    "message": "Node status summarized.",
    "structured_payload": {},
    "expected_result": "The mission source is informed.",
    "confidence": 0.9,
}


def _write_fake_gemini(
    path: Path,
    *,
    version: str = "0.46.0-fake",
    response: str | None = json.dumps(_VALID_DECISION),
    include_response: bool = True,
    total_calls: int = 0,
    by_name: dict | None = None,
    exit_code: int = 0,
    sleep: float = 0.0,
    outer_error: dict | None = None,
    raw_stdout: str | None = None,
    stderr: str = "",
    stdin_capture: Path | None = None,
    started_marker: Path | None = None,
    finished_marker: Path | None = None,
) -> Path:
    payload: dict = {"session_id": "fake-session"}
    if outer_error is not None:
        payload["error"] = outer_error
    if include_response and response is not None:
        payload["response"] = response
    payload["stats"] = {
        "models": {"gemini-3-flash-preview": {}, "gemini-3.1-flash-lite": {}},
        "tools": {"totalCalls": total_calls, "byName": by_name or {}},
        "files": {},
    }
    out = raw_stdout if raw_stdout is not None else json.dumps(payload)

    lines = [
        "#!/usr/bin/env python3",
        "import sys, os, time",
        "args = sys.argv[1:]",
        "if '--version' in args:",
        f"    print({version!r})",
        "    sys.exit(0)",
        "data = sys.stdin.read()",
    ]
    if stdin_capture is not None:
        lines.append(f"open(r{str(stdin_capture)!r}, 'w', encoding='utf-8').write(data)")
    if started_marker is not None:
        lines.append(f"open(r{str(started_marker)!r}, 'w').write('started')")
    if sleep:
        lines.append(f"time.sleep({sleep})")
    if finished_marker is not None:
        lines.append(f"open(r{str(finished_marker)!r}, 'w').write('finished')")
    if stderr:
        lines.append(f"sys.stderr.write({stderr!r})")
    lines.append(f"sys.stdout.write({out!r})")
    lines.append(f"sys.exit({exit_code})")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o755)
    return path


def _provider(executable: Path | str, **overrides) -> GeminiCLIProvider:
    cfg = CoordinatorConfig(provider="gemini_cli", executable=str(executable), **overrides)
    return GeminiCLIProvider(cfg)


def _input() -> CoordinatorInput:
    return CoordinatorInput(
        mission_id="m-1",
        mission_content="Summarize the current node status.",
        mission_source_type="user",
        mission_status="received",
        current_time=datetime.now(UTC),
    )


def _decide(provider: GeminiCLIProvider):
    return asyncio.run(provider.decide(_input()))


# -- readiness (Part E) ----------------------------------------------------------


def test_executable_not_found() -> None:
    assert _provider("definitely-not-a-real-gemini-xyz").check_ready() == [
        "executable_not_found"
    ]


def test_executable_provider_mismatch(tmp_path) -> None:
    fake = _write_fake_gemini(tmp_path / "gemini", version="codex-cli 0.137.0")
    assert _provider(fake).check_ready() == ["executable_provider_mismatch"]


def test_version_probe_success_caches_version(tmp_path) -> None:
    fake = _write_fake_gemini(tmp_path / "gemini", version="0.46.0-fake")
    provider = _provider(fake)
    assert provider.check_ready() == []
    assert provider.cli_version() == "0.46.0-fake"


def test_readiness_probe_is_cached(tmp_path) -> None:
    calls = tmp_path / "version_calls.txt"
    # A fake whose --version appends to a counter file lets us prove it is probed once.
    fake = tmp_path / "gemini"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--version' in sys.argv[1:]:\n"
        f"    open(r{str(calls)!r}, 'a').write('x')\n"
        "    print('0.46.0-fake'); sys.exit(0)\n"
        "sys.exit(0)\n"
    )
    fake.chmod(0o755)
    provider = _provider(fake)
    for _ in range(5):
        provider.check_ready()
    assert calls.read_text() == "x"  # probed exactly once, then cached


def test_provider_mismatch_when_version_not_semver(tmp_path) -> None:
    fake = _write_fake_gemini(tmp_path / "gemini", version="not-a-version")
    assert _provider(fake).check_ready() == ["executable_provider_mismatch"]


# -- invocation shape (Parts C/D) ------------------------------------------------


def test_optional_model_flag(tmp_path) -> None:
    fake = _write_fake_gemini(tmp_path / "gemini")
    resolved = str(fake)
    # No model -> no --model.
    assert "--model" not in _provider(fake)._argv(resolved)
    # Model set -> --model passed.
    argv = _provider(fake, model="gemini-3-pro")._argv(resolved)
    assert argv[argv.index("--model") + 1] == "gemini-3-pro"
    # Always headless JSON, never YOLO/auto-approval.
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert "--skip-trust" in argv
    assert "--yolo" not in argv and "yolo" not in argv


def test_prompt_delivered_through_stdin(tmp_path) -> None:
    captured = tmp_path / "stdin.txt"
    fake = _write_fake_gemini(tmp_path / "gemini", stdin_capture=captured)
    decision = _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
    assert decision.next_target == "respond"
    text = captured.read_text()
    # The full coordinator prompt (system + user + no-tools instruction) went via stdin.
    assert "Coordinator Agent" in text
    assert "do NOT call, use, or attempt any tools" in text.replace("\n", " ").lower() or \
        "do not call, use, or attempt any tools" in text.lower()
    assert "Summarize the current node status." in text


# -- output parsing (Part C) -----------------------------------------------------


def test_valid_decision(tmp_path) -> None:
    fake = _write_fake_gemini(
        tmp_path / "gemini",
        response=json.dumps({**_VALID_DECISION, "next_target": "node_state"}),
    )
    decision = _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
    assert decision.next_target == "node_state"
    assert decision.mission_id == "m-1"


def test_decision_in_markdown_fence_is_extracted(tmp_path) -> None:
    fenced = "```json\n" + json.dumps(_VALID_DECISION) + "\n```"
    fake = _write_fake_gemini(tmp_path / "gemini", response=fenced)
    decision = _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
    assert decision.next_target == "respond"


def test_malformed_outer_cli_json(tmp_path) -> None:
    fake = _write_fake_gemini(tmp_path / "gemini", raw_stdout="this is not json at all")
    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
    assert "malformed JSON" in str(exc.value)


def test_outer_cli_error_object(tmp_path) -> None:
    fake = _write_fake_gemini(
        tmp_path / "gemini",
        outer_error={"type": "AuthError", "message": "not authenticated"},
        include_response=False,
    )
    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
    message = str(exc.value)
    assert "reported an error" in message and "AuthError" in message


def test_missing_response_field(tmp_path) -> None:
    fake = _write_fake_gemini(tmp_path / "gemini", include_response=False)
    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
    assert "no usable 'response'" in str(exc.value)


def test_malformed_decision_extraction_stage(tmp_path) -> None:
    fake = _write_fake_gemini(tmp_path / "gemini", response="not a json decision")
    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
    assert "extraction stage" in str(exc.value)


def test_malformed_decision_validation_stage(tmp_path) -> None:
    fake = _write_fake_gemini(tmp_path / "gemini", response=json.dumps({"summary": "only"}))
    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
    assert "validation stage" in str(exc.value)


def test_nonzero_exit(tmp_path) -> None:
    fake = _write_fake_gemini(
        tmp_path / "gemini", raw_stdout="", exit_code=7, stderr="boom on stderr"
    )
    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
    message = str(exc.value)
    assert "no JSON output" in message and "exit code 7" in message


# -- subprocess lifecycle (Part D) -----------------------------------------------


def test_timeout_terminates_process(tmp_path) -> None:
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    fake = _write_fake_gemini(
        tmp_path / "gemini", sleep=5.0, started_marker=started, finished_marker=finished
    )
    provider = _provider(fake, timeout_seconds=0.5, working_directory=str(tmp_path / "rt"))
    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(provider)
    assert "timed out" in str(exc.value)
    assert started.exists()  # the process started...
    assert not finished.exists()  # ...and was killed before finishing (no orphan)


# -- secret redaction (Parts C/Quality) ------------------------------------------


def test_stderr_secret_is_redacted(tmp_path) -> None:
    secret = "sk-supersecretvalue999"
    fake = _write_fake_gemini(
        tmp_path / "gemini",
        raw_stdout="",
        exit_code=1,
        stderr=f"failure: Bearer {secret} rejected; api_key={secret}",
    )
    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
    message = str(exc.value)
    assert secret not in message
    assert "[REDACTED]" in message


# -- tool-call enforcement (Part C) ----------------------------------------------


def test_reported_tool_call_causes_failure(tmp_path) -> None:
    fake = _write_fake_gemini(
        tmp_path / "gemini",
        total_calls=2,
        by_name={"grep_search": {"count": 2}},
    )
    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
    message = str(exc.value)
    assert "attempted 2 tool call" in message
    assert "grep_search" in message


# -- factory / unknown provider (Part F) -----------------------------------------


def test_unknown_provider_builds_to_none() -> None:
    assert build_provider(CoordinatorConfig(provider="does-not-exist")) is None


def test_mismatched_executable_raises_configuration_error(tmp_path) -> None:
    fake = _write_fake_gemini(tmp_path / "gemini", version="codex-cli 1.0")
    with pytest.raises(CoordinatorConfigurationError):
        _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
