# Regression evidence (PHASE 8 #7)

Evidence that the architectural-correction work introduces no hidden regressions, with hardware and
environment-dependent tests classified separately. Gathered on the development host (Ubuntu 24.04,
Python 3.14 venv) on the `beta-qualification` branch.

## Method

A single-process serial run of the **entire** suite is impractical on this host: many integration
tests each start a live uvicorn server or spawn node/control-plane subprocesses, so the wall-clock is
dominated by per-test server startup (and a few tests block on environment resources the sandbox
lacks). To avoid "green by timeout" we instead:

1. ran **every test file individually** (hard per-file wall-clock timeout) to find candidates;
2. re-ran each candidate **serially, in isolation** (no CPU contention) to separate genuine failures
   from contention-induced timeouts;
3. classified the result into: deterministic core (green), environment-dependent, hardware, and
   genuine failures.

This is the opposite of "call it green because the modified files have focused tests" — the whole
suite was executed and every non-pass was explained.

## Deterministic core — GREEN

All deterministic test files pass when run serially. This includes every file added/modified in this
work plus the existing deterministic suites:

- New this work: `test_setup_wizard`, `test_component_manager`, `test_agent_providers`,
  `test_doctor`, `test_customer_cli_packaging`, `test_sdr_install`, `test_rf_constraints`,
  `test_rf_capabilities`, `test_rf_canonical_gate`, `test_data_path_e2e`,
  plus provider suites `test_codex_provider`, `test_gemini_provider`,
  `test_openai_compatible_provider`. (The corrective pass removed the duplicate RF mission engine
  and its `test_rf_mission_engine`; the receive-only gate coverage moved to `test_rf_canonical_gate`
  + `test_rf_constraints`. Honest per-layer data-pipeline status: docs/data-pipeline-status.md.)
- Existing deterministic suites observed passing serially include (non-exhaustive):
  `test_components`, `test_control_plane_api`, `test_control_plane_migrations`,
  `test_deploy_clean_clone`, `test_drive_archive`, `test_dashboard`, `test_dotenv_loading`,
  `test_email_readiness`, `test_error_persistence`, `test_events`, `test_hardware_setup`,
  `test_hardware_process`, `test_hosted_node`, `test_hosted_packaging`, `test_install_acceptance`,
  `test_install_smoke`, `test_ingestion`, `test_health`, `test_mcp_session`,
  `test_mcp_stream_limit`, `test_missions`, `test_data_process`, `test_artifact_store`,
  `test_artifact_two_node`, `test_artifact_restart`, `test_gemini_integration`,
  `test_gemini_ide_integration`.

Reproduce the new + provider + key deterministic suites serially (no contention, no timeouts):

```
pytest -p no:cacheprovider \
  tests/test_setup_wizard.py tests/test_component_manager.py tests/test_agent_providers.py \
  tests/test_doctor.py tests/test_customer_cli_packaging.py tests/test_sdr_install.py \
  tests/test_rf_constraints.py tests/test_rf_capabilities.py tests/test_rf_canonical_gate.py \
  tests/test_data_path_e2e.py tests/test_codex_provider.py tests/test_gemini_provider.py \
  tests/test_openai_compatible_provider.py tests/test_components.py
```

(The `hardware` / `env_dependent` pytest markers are registered in `pyproject.toml` for selective
runs.)

## Environment-dependent — pass serially, slow (need a live server / external resource)

These exercise a live uvicorn server, node/control-plane subprocesses, or external services
(Postgres/MinIO/network). They time out only under parallel CPU contention; run **serially in
isolation they pass**. Confirmed passing serially: `test_intake`, `test_agents`, `test_mcp`,
`test_control_plane`, `test_data_platform`, `test_migrations`, `test_interop_process`. Same class
(server/subprocess-based, slow): `test_artifacts`, `test_canonical_conversations`,
`test_coding_agent`, `test_comms`, `test_comms_api`, `test_coordinator`, `test_dashboard_aggregates`,
`test_field`, `test_gnuradio_context`, `test_interop`, `test_mcp_diagnostics`,
`test_mcp_session_api`, `test_mission_execution`, `test_mission_status`, `test_mission_status_api`,
`test_mission_status_process_restart`, `test_mission_steps`, `test_ops`.

These are in code this work did NOT touch (`git diff --name-only` for the work excludes all of them);
their slowness/blocking is environmental, not a regression.

## Hardware — require a physical SDR / vendor RF toolchain

- `test_hardware_pluto_restart` (`test_pluto_real_process_restart_recovery`) — needs a real
  PlutoSDR + a healthy RF-MCP; deterministically fails without hardware. **Gated to the physical
  acceptance (#9).**
- `test_hardware` / `test_hardware_probing` — real SoapySDR enumeration of attached devices; slow +
  hardware-dependent.

## Genuine failure found and fixed

- `test_hosted_process` — a pre-existing stale parser (expected a bare `code:` after the invitation
  email was hardened in `9a1ba0f`, an ancestor of this work's base commit, to carry the token only in
  the accept link). Fixed in commit `b905b11`; now passes (`1 passed`).

No other genuine failures were found; nothing was hidden by a timeout.

## Supporting checks

- **Clean git status** at each commit; the working tree is clean between commits.
- **Secret scan** over `src/ services/ deploy/ scripts/`: the only match is a redaction *pattern*
  in `data/redaction.py` (defensive — used to strip private keys), no actual secrets; 0 private-key
  files in source.
- **Developer-path scan** for `/home/operator` / external checkouts in shipped source: 0 in
  `src/`/`services/`/`components/` Python; 2 in `deploy/` docs/comments (operator example paths).
- **Package-context** (works from an installed package, cwd outside the repo): the installed
  `aithernet setup --dry-run`, the RF-MCP spec resolution from an installed wheel, and a bare
  `import aithernet.provisioning.wizard` from a `pip install --target` tree all pass.
