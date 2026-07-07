"""beta.5: CatGPT-Gateway coordinator provider — a local, subscription-backed OpenAI-compatible
browser gateway used for coordinator reasoning only.

Covers: registry/catalogue/capability wiring (coordinator-only), local-gateway defaults + env
overrides, api-key-optional readiness, the provider-status shape (auth_readiness /
endpoint_reachable / mission_ready), structured-JSON decision output for the mission engine, and
live-test error handling (unreachable / non-JSON / success / quota) with no secret leakage.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import tempfile
from pathlib import Path

import httpx
import pytest

from aithernet.agents import capabilities as caps
from aithernet.agents import providers as prov
from aithernet.config.settings import CoordinatorConfig
from aithernet.coordinator.contracts import (
    CoordinatorConfigurationError,
    CoordinatorInput,
    CoordinatorProviderError,
)
from aithernet.coordinator.providers import PROVIDERS, build_provider
from aithernet.coordinator.providers.catgpt_gateway import (
    DEFAULT_BASE_URL,
    ENV_API_KEY_REF,
    ENV_BASE_URL,
    ENV_MODEL,
    CatGPTGatewayProvider,
)


def _decision_json() -> str:
    return json.dumps({
        "summary": "plan", "next_target": "gnuradio_mcp", "action": "build",
        "message": "ok", "structured_payload": {}, "expected_result": "a flowgraph",
        "confidence": 0.9, "mission_control": {"disposition": "continue"}})


def _payload() -> CoordinatorInput:
    return CoordinatorInput(
        mission_id="m1", mission_content="build a signal generator", mission_source_type="user",
        mission_status="running", current_time=datetime.datetime.now(datetime.UTC),
        available_capabilities=["gnuradio_mcp"])


# -- registry / catalogue / capability wiring -------------------------------------------------

def test_registry_and_catalogue_wire_catgpt_gateway_as_coordinator():
    assert PROVIDERS.get("catgpt_gateway") is CatGPTGatewayProvider
    info = prov.get_info("coordinator", "catgpt_gateway")
    assert info is not None
    assert info.kind == "local"
    assert "CatGPT Gateway" in info.display


def test_catgpt_gateway_is_coordinator_only_not_coding():
    # eligible for coordinator (reasoning + structured decisions), NOT for coding.
    assert caps.satisfies_role("catgpt_gateway", "coordinator") is True
    assert caps.satisfies_role("catgpt_gateway", "coding") is False
    # not present in the coding catalogue at all — keeps the coding agent a separate selection.
    assert prov.get_info("coding", "catgpt_gateway") is None
    assert "catgpt_gateway" not in [p.key for p in prov.providers_for("coding")]


def test_capability_auth_is_local_no_cloud_billing():
    assert caps.auth_category("catgpt_gateway") == caps.AUTH_LOCAL
    assert caps.billing_model("catgpt_gateway") == caps.BILL_LOCAL


# -- local-gateway defaults + env overrides ---------------------------------------------------

def test_endpoint_default_applied_but_model_is_never_fabricated():
    p = build_provider(CoordinatorConfig(provider="catgpt_gateway"))
    assert isinstance(p, CatGPTGatewayProvider)
    assert p.config.base_url == DEFAULT_BASE_URL       # endpoint has a documented local default
    assert p.config.model is None                      # model is NEVER defaulted/fabricated
    # a local gateway needs no external key, but an unset model makes it not-ready (model required).
    assert p.config.allow_unauthenticated is True
    assert p.check_ready() == ["model"]


def test_explicit_config_wins_over_defaults():
    p = build_provider(CoordinatorConfig(
        provider="catgpt_gateway", base_url="http://127.0.0.1:9001/v1", model="claude-x"))
    assert p.config.base_url == "http://127.0.0.1:9001/v1"
    assert p.config.model == "claude-x"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv(ENV_BASE_URL, "http://127.0.0.1:7000/v1")
    monkeypatch.setenv(ENV_MODEL, "gpt-5-env")
    p = build_provider(CoordinatorConfig(provider="catgpt_gateway"))
    assert p.config.base_url == "http://127.0.0.1:7000/v1"
    assert p.config.model == "gpt-5-env"


def test_env_api_key_ref_resolves_named_env_var(monkeypatch):
    monkeypatch.setenv(ENV_API_KEY_REF, "MY_LOCAL_KEY")
    monkeypatch.setenv("MY_LOCAL_KEY", "dummy-local-value")
    p = build_provider(CoordinatorConfig(provider="catgpt_gateway"))
    assert p.config.api_key == "dummy-local-value"
    # a provided key means auth is sent, so we no longer force allow_unauthenticated.
    assert p.config.allow_unauthenticated is False


# -- provider-status shape --------------------------------------------------------------------

def test_status_reports_local_gateway_configured_and_not_mission_ready(monkeypatch):
    # Hermetic: don't depend on whether a real gateway happens to listen on :8000.
    monkeypatch.setattr(prov, "_endpoint_reachable", lambda *a, **k: False)
    root = Path(tempfile.mkdtemp())
    prov.configure("coordinator", "catgpt_gateway", model="gpt-configured", state_root=root)
    st = prov.status("coordinator", state_root=root)
    assert st["provider"] == "catgpt_gateway"
    assert st["configured"] is True
    assert st["endpoint"] == DEFAULT_BASE_URL
    assert st["model"] == "gpt-configured"         # exactly what was configured, never fabricated
    assert st["auth_readiness"] == "local-gateway-configured"
    r = st["readiness"]
    assert r["supported"] is True and r["selected"] is True and r["configured"] is True
    assert r["endpoint_reachable"] is False        # (probe monkeypatched — deterministic)
    assert r["live_verified"] is False and r["mission_ready"] is False


def test_status_shows_no_model_when_unconfigured():
    root = Path(tempfile.mkdtemp())
    prov.configure("coordinator", "catgpt_gateway", state_root=root)  # no model
    st = prov.status("coordinator", state_root=root)
    assert st["model"] is None                     # honest: no fabricated model
    assert st["readiness"]["mission_ready"] is False


def test_status_flips_after_recorded_quota_failure():
    root = Path(tempfile.mkdtemp())
    prov.configure("coordinator", "catgpt_gateway", state_root=root)
    prov.record_provider_readiness("coordinator", "quota_exhausted", live=False, state_root=root)
    st = prov.status("coordinator", state_root=root)
    assert st["last_test"]["status"] == "quota_exhausted"
    assert st["readiness"]["mission_ready"] is False


# -- structured decision output (mission-engine contract) -------------------------------------

def test_decide_uses_plain_request_and_parses_json_from_text():
    # CatGPT-Gateway may not support response_format/tools — the mission decide() must NOT send them
    # and must parse the JSON object from the assistant text into the SAME CoordinatorDecision.
    seen = {}

    def handler(request):
        body = json.loads(request.content)
        seen["model"] = body["model"]
        seen["keys"] = set(body.keys())
        return httpx.Response(200, json={"choices": [{"message": {"content": _decision_json()}}]})

    p = CatGPTGatewayProvider(CoordinatorConfig(provider="catgpt_gateway", model="my-model"),
                              transport=httpx.MockTransport(handler))
    decision = asyncio.run(p.decide(_payload()))
    assert decision.next_target == "gnuradio_mcp"          # identical CoordinatorDecision contract
    assert seen["model"] == "my-model"
    assert "response_format" not in seen["keys"]           # advanced feature NOT sent
    assert "tools" not in seen["keys"] and "tool_choice" not in seen["keys"]


def test_decide_rejects_freeform_non_json_as_a_decision():
    # Free-form natural language must NOT pass through as a decision — the parser raises.
    p = CatGPTGatewayProvider(
        CoordinatorConfig(provider="catgpt_gateway", model="my-model"),
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={
                "choices": [{"message": {"content": "Sure, I'll build a flowgraph!"}}]})))
    with pytest.raises(CoordinatorProviderError):
        asyncio.run(p.decide(_payload()))


def test_healthcheck_reports_reachability():
    p = CatGPTGatewayProvider(
        CoordinatorConfig(provider="catgpt_gateway", model="my-model"),
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})))
    meta = asyncio.run(p.healthcheck())
    assert meta["provider"] == "catgpt_gateway"
    assert meta["response_received"] is True


def test_list_models_discovers_ids():
    def handler(request):
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}, {"id": "claude-3.7"}]})

    p = CatGPTGatewayProvider(CoordinatorConfig(provider="catgpt_gateway"),
                              transport=httpx.MockTransport(handler))
    assert asyncio.run(p.list_models()) == ["gpt-4o", "claude-3.7"]


def test_list_models_empty_on_unsupported_endpoint():
    p = CatGPTGatewayProvider(
        CoordinatorConfig(provider="catgpt_gateway"),
        transport=httpx.MockTransport(lambda req: httpx.Response(404, text="not found")))
    assert asyncio.run(p.list_models()) == []


# -- live-test error handling -----------------------------------------------------------------

def test_unreachable_endpoint_raises_sanitized_error():
    def handler(request):
        raise httpx.ConnectError("Connection refused", request=request)

    p = CatGPTGatewayProvider(CoordinatorConfig(provider="catgpt_gateway", model="my-model"),
                              transport=httpx.MockTransport(handler))
    with pytest.raises(CoordinatorProviderError) as ei:
        asyncio.run(p.decide(_payload()))
    assert "127.0.0.1:8000" in str(ei.value)  # sanitized endpoint host present


def test_non_json_response_reports_parse_failure():
    p = CatGPTGatewayProvider(
        CoordinatorConfig(provider="catgpt_gateway", model="my-model"),
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text="<html>oops</html>")))
    with pytest.raises(CoordinatorProviderError) as ei:
        asyncio.run(p.decide(_payload()))
    assert "not valid" in str(ei.value).lower() or "json" in str(ei.value).lower()


def test_quota_error_surfaces_and_classifies():
    from aithernet.missions import failure_classification as fc
    p = CatGPTGatewayProvider(
        CoordinatorConfig(provider="catgpt_gateway", model="my-model"),
        transport=httpx.MockTransport(
            lambda req: httpx.Response(429, text="RESOURCE_EXHAUSTED: quota exhausted")))
    with pytest.raises(CoordinatorProviderError) as ei:
        asyncio.run(p.decide(_payload()))
    assert fc.category(None, str(ei.value)) == fc.QUOTA_EXHAUSTED


def test_provider_test_live_gives_actionable_unreachable_detail(monkeypatch):
    # A CONFIGURED-model gateway that is genuinely stopped must give an actionable, honest
    # "unreachable" message — classified as catgpt_gateway_unreachable, not a blanket error.
    import aithernet.coordinator.providers as cp

    def refused(request):
        raise httpx.ConnectError("Connection refused", request=request)

    root = Path(tempfile.mkdtemp())
    prov.configure("coordinator", "catgpt_gateway", model="gpt-configured", state_root=root)
    monkeypatch.setattr(cp, "build_provider", lambda cfg: CatGPTGatewayProvider(
        cfg, transport=httpx.MockTransport(refused)))
    r = prov.test("coordinator", live=True, state_root=root)
    assert r["ready"] is False
    assert r["status"] == "catgpt_gateway_unreachable"
    assert "not reachable" in r["detail"] and "local gateway running" in r["detail"]
    assert "127.0.0.1:8000" in r["detail"]
    assert r["endpoint_reachable"] is False


def test_no_secret_leakage_in_errors():
    # An upstream error body echoing a bearer token must be redacted in the surfaced error.
    p = CatGPTGatewayProvider(
        CoordinatorConfig(provider="catgpt_gateway", model="my-model",
                          api_key="sk-supersecret012345"),
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                401, text="Authorization: Bearer sk-supersecret012345 rejected")))
    with pytest.raises(CoordinatorProviderError) as ei:
        asyncio.run(p.decide(_payload()))
    assert "sk-supersecret012345" not in str(ei.value)


# -- model selection: never fabricated; discovery-driven or a clear failure --------------------

def test_healthcheck_requires_choice_when_discovery_lists_models():
    # No model configured, but the gateway REPORTS models -> fail asking the user to CHOOSE one.
    def handler(request):
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "gpt-5"}, {"id": "claude-sonnet-4"}]})

    p = CatGPTGatewayProvider(CoordinatorConfig(provider="catgpt_gateway"),
                              transport=httpx.MockTransport(handler))
    with pytest.raises(CoordinatorConfigurationError) as ei:
        asyncio.run(p.healthcheck())
    msg = str(ei.value)
    assert "not configured" in msg
    assert "gpt-5" in msg and "claude-sonnet-4" in msg   # lists the ACTUAL gateway models
    assert "gpt-4o" not in msg                           # never a fabricated placeholder


def test_healthcheck_fails_clearly_when_no_model_and_discovery_unavailable():
    # No model configured AND /models unavailable -> the exact, honest failure message.
    p = CatGPTGatewayProvider(
        CoordinatorConfig(provider="catgpt_gateway"),
        transport=httpx.MockTransport(lambda req: httpx.Response(404, text="no such route")))
    with pytest.raises(CoordinatorConfigurationError) as ei:
        asyncio.run(p.healthcheck())
    assert str(ei.value) == (
        "CatGPT-Gateway model not configured and model discovery is unavailable.")


def test_live_test_no_model_discovery_unavailable_fails_clearly(monkeypatch):
    # End-to-end via `agents test --live`: no model AND discovery unavailable -> honest message.
    # Hermetic: force discovery to fail regardless of any real gateway on :8000.
    import aithernet.coordinator.providers as cp
    root = Path(tempfile.mkdtemp())
    prov.configure("coordinator", "catgpt_gateway", state_root=root)  # no model
    monkeypatch.setattr(cp, "build_provider", lambda cfg: CatGPTGatewayProvider(
        cfg, transport=httpx.MockTransport(lambda req: httpx.Response(404, text="no /models"))))
    r = prov.test("coordinator", live=True, state_root=root)
    assert r["ready"] is False and r["status"] == "not_ready"
    assert "model" in r.get("missing", [])
    assert r["detail"] == (
        "CatGPT-Gateway model not configured and model discovery is unavailable.")


def test_configured_model_accepted_but_inference_can_return_model_unavailable():
    # A configured model is used as-is, but the gateway may still reject it (model_unavailable).
    from aithernet.missions import failure_classification as fc

    def handler(request):
        # discovery says the model isn't there; inference returns a 404 model-not-found.
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "some-other-model"}]})
        return httpx.Response(404, text="model not found: gpt-missing")

    p = CatGPTGatewayProvider(CoordinatorConfig(provider="catgpt_gateway", model="gpt-missing"),
                              transport=httpx.MockTransport(handler))
    # The configured model is honoured (no config error); the gateway's rejection is surfaced.
    assert p.check_ready() == []
    with pytest.raises(CoordinatorProviderError) as ei:
        asyncio.run(p.healthcheck())
    assert fc.category(None, str(ei.value)) == fc.MODEL_UNAVAILABLE


# =============================================================================================
# Manual-acceptance scenarios (the exact CatGPT-Gateway session verified by hand): a gateway that
# requires a local bearer token for /models AND /chat/completions, returns catgpt-browser, and
# emits parseable JSON text. These exercise the WHOLE `agents test coordinator --live` path.
# =============================================================================================

TOKEN = "dummy123"
MODEL = "catgpt-browser"


def _gateway(*, require_auth=True, chat_status=200,
             chat_content='{"status":"ready","message":"ok"}', models_status=200,
             record=None):
    """A MockTransport emulating CatGPT-Gateway. Records Authorization headers when `record` set."""
    def handler(request):
        auth = request.headers.get("authorization")
        if record is not None:
            record[request.url.path] = auth
        if require_auth and auth != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "invalid or missing token"})
        if request.url.path.endswith("/models"):
            return httpx.Response(models_status,
                                  json={"object": "list", "data": [{"id": MODEL}]})
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            # The live/mission path must never send advanced OpenAI features to CatGPT-Gateway.
            assert "response_format" not in body
            assert "tools" not in body and "tool_choice" not in body
            if chat_status == 200:
                return httpx.Response(
                    200, json={"choices": [{"message": {"content": chat_content}}]})
            return httpx.Response(chat_status, text=chat_content)
        return httpx.Response(404, text="no such route")
    return httpx.MockTransport(handler)


def _configure_and_test_live(monkeypatch, *, transport, model=MODEL, token=TOKEN, api_key_ref=None):
    """Configure catgpt in a temp node + managed store, point build_provider at `transport`, and
    run the real `prov.test(live=True)` end-to-end."""
    import aithernet.coordinator.providers as cp
    root = Path(tempfile.mkdtemp())
    secret_file = root / "aithernet.env"
    if token is not None:
        secret_file.write_text(f"CATGPT_GATEWAY_API_KEY={token}\n")
    monkeypatch.setattr(prov, "secret_env_file", lambda: secret_file)
    prov.configure("coordinator", "catgpt_gateway", model=model or "",
                   api_key_ref=(api_key_ref or ("CATGPT_GATEWAY_API_KEY" if token else "")),
                   state_root=root)
    monkeypatch.setattr(cp, "build_provider",
                        lambda cfg: CatGPTGatewayProvider(cfg, transport=transport))
    return prov.test("coordinator", live=True, state_root=root), root


# -- A: gateway works with bearer token -> ready ----------------------------------------------

def test_A_live_test_ready_with_bearer_token(monkeypatch):
    r, _ = _configure_and_test_live(monkeypatch, transport=_gateway())
    assert r["provider"] == "catgpt_gateway"
    assert r["live"] is True and r["ready"] is True and r["status"] == "ready"
    assert r["detail"] == "live bounded inference succeeded"
    assert r["model"] == MODEL
    assert r["endpoint"] == "http://127.0.0.1:8000/v1"
    assert r["endpoint_reachable"] is True


# -- B: wrong/missing token -> credential_rejected (NOT unreachable) ---------------------------

def test_B_wrong_token_is_credential_rejected_not_unreachable(monkeypatch):
    # No managed token stored -> the probe sends no/invalid bearer -> gateway 401.
    r, _ = _configure_and_test_live(monkeypatch, transport=_gateway(require_auth=True), token=None,
                                    api_key_ref="CATGPT_GATEWAY_API_KEY")
    assert r["ready"] is False
    assert r["status"] == "credential_rejected"          # NOT catgpt_gateway_unreachable
    assert r["endpoint_reachable"] is True               # /models was reachable
    assert "not reachable" not in r["detail"].lower()


# -- C: unsupported advanced features -> live-test never sends them; a 400 is a compat error ---

def test_C_live_probe_omits_advanced_features_and_classifies_400():
    # The _gateway handler asserts no response_format/tools are sent, so a passing run proves it.
    p = CatGPTGatewayProvider(CoordinatorConfig(provider="catgpt_gateway", model=MODEL,
                                                api_key=TOKEN), transport=_gateway())
    assert asyncio.run(p.live_probe())["ok"] is True
    # A gateway that 400s on an advanced feature is a compatibility error, never "unreachable".
    p2 = CatGPTGatewayProvider(
        CoordinatorConfig(provider="catgpt_gateway", model=MODEL, api_key=TOKEN),
        transport=_gateway(chat_status=400, chat_content="unsupported field: response_format"))
    assert asyncio.run(p2.live_probe())["category"] == "unsupported_gateway_feature"


# -- D: invalid JSON content -> structured_output_incompatible --------------------------------

def test_D_invalid_json_is_structured_output_incompatible(monkeypatch):
    r, _ = _configure_and_test_live(
        monkeypatch, transport=_gateway(chat_content="hello, I am a chatbot"))
    assert r["ready"] is False
    assert r["status"] == "structured_output_incompatible"
    assert r["endpoint_reachable"] is True


# -- E: browser session not ready -------------------------------------------------------------

def test_E_browser_session_not_ready(monkeypatch):
    r, _ = _configure_and_test_live(
        monkeypatch,
        transport=_gateway(chat_status=401, chat_content="Please log in to ChatGPT in the browser"))
    assert r["ready"] is False
    assert r["status"] == "browser_session_not_ready"


# -- F: connect flow prompts for and stores the bearer token ----------------------------------

def test_F_connect_flow_prompts_and_stores_bearer_token(monkeypatch):
    import typer

    from aithernet.cli import _guide_catgpt_gateway
    stored = {}
    # Gateway needs a token: unauth discovery empty, authed discovery lists catgpt-browser.
    monkeypatch.setattr(prov, "discover_models",
                        lambda base, api_key_ref="": ([MODEL] if api_key_ref else []))
    monkeypatch.setattr(prov, "set_secret",
                        lambda name, value, **k: stored.__setitem__(name, value))
    monkeypatch.setattr(prov, "list_secret_names", lambda: ["CATGPT_GATEWAY_API_KEY"])

    def fake_prompt(text, default=None, hide_input=False):
        t = text.lower()
        if "endpoint" in t:
            return "http://127.0.0.1:8000/v1"
        if "token" in t:
            return TOKEN
        if "model number" in t or "choose a model" in t:
            return "1"
        return default if default is not None else ""
    monkeypatch.setattr(typer, "prompt", fake_prompt)

    base_url, model, api_key_ref = _guide_catgpt_gateway(None, None, None, interactive=True)
    assert api_key_ref == "CATGPT_GATEWAY_API_KEY"        # saved into node config downstream
    assert stored.get("CATGPT_GATEWAY_API_KEY") == TOKEN  # stored via managed secrets (not printed)
    assert model == MODEL                                 # chosen from the authed discovery


# -- G: Authorization header propagated to /models AND /chat/completions -----------------------

def test_G_authorization_header_sent_everywhere():
    seen = {}
    p = CatGPTGatewayProvider(
        CoordinatorConfig(provider="catgpt_gateway", model=MODEL, api_key=TOKEN),
        transport=_gateway(record=seen))
    asyncio.run(p.live_probe())
    assert seen.get("/v1/models") == f"Bearer {TOKEN}"
    assert seen.get("/v1/chat/completions") == f"Bearer {TOKEN}"
    # discovery too
    seen.clear()
    asyncio.run(p.list_models())
    assert seen.get("/v1/models") == f"Bearer {TOKEN}"


# -- managed-secret resolution (the root-cause fix) -------------------------------------------

def test_resolve_secret_value_reads_managed_store(monkeypatch, tmp_path):
    f = tmp_path / "aithernet.env"
    f.write_text("CATGPT_GATEWAY_API_KEY=abc123\nOTHER=zzz\n")
    monkeypatch.setattr(prov, "secret_env_file", lambda: f)
    monkeypatch.delenv("CATGPT_GATEWAY_API_KEY", raising=False)
    assert prov.resolve_secret_value("CATGPT_GATEWAY_API_KEY") == "abc123"
    assert prov.resolve_secret_value("MISSING") is None
    # process env wins over the file
    monkeypatch.setenv("CATGPT_GATEWAY_API_KEY", "from-env")
    assert prov.resolve_secret_value("CATGPT_GATEWAY_API_KEY") == "from-env"
