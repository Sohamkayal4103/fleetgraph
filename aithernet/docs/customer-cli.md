# Aithernet customer CLI: setup, components, providers, doctor

This page documents the customer-facing commands that drive installation and readiness on a node.
Every command below works from the installed package (the `.deb` / wheel) — you do **not** need the
source repository. None of these commands store credentials in any project directory, and none
print secrets.

> Conventions: `<…>` is a value you supply. Commands that change the system tell you exactly what
> needs `sudo` before doing anything. Re-running a command is always safe (idempotent).

## 1. Guided setup — `aithernet setup`

The installation coordinator. It runs an ordered, resumable set of steps and records progress so an
interrupted run can continue.

```
aithernet setup --dry-run                 # show the full plan; make NO changes
aithernet setup                           # interactive wizard
aithernet setup --non-interactive \       # answer from flags only, never prompt
    --deployment-mode standalone \
    --profile plutosdr \
    --service-mode systemd-user
aithernet setup --resume                  # continue an interrupted run
aithernet setup --repair                  # re-run failed / incomplete steps
aithernet setup --status                  # show recorded progress
```

Hardware profiles: `software-only`, `simulation`, `plutosdr`, `usrp`, `generic-soapy`, `full-lab`
(the older names `core`, `pluto`, `soapy-generic`, `full` remain accepted as aliases).

`--dry-run` reports the OS packages, managed components, download sources, estimated download and
installed size, every privileged operation, service changes, and any restart/logout/reboot
requirement — without touching the system.

## 2. Managed components — `aithernet components`

Managed components are built reproducibly from a pinned upstream commit + a tracked patch series,
then installed into a managed prefix. Aithernet runs the **installed** component, never a checkout.

```
aithernet components list                 # installed components + provenance
aithernet components plan rf-mcp          # provenance + what install would do (no changes)
aithernet components install rf-mcp --bundle <dir>        # install a signed bundle (verified)
aithernet components install rf-mcp --upstream-git <dir>  # build from a pinned source mirror
aithernet components verify rf-mcp        # inventory integrity + signature
aithernet components repair rf-mcp        # restore owned files from the recorded source
aithernet components remove rf-mcp        # remove ONLY files this component owns
```

`install` verifies provenance (archive digest, upstream commit, detached signature) before writing
anything. `remove` never deletes shared system packages or files it does not own.

## 3. AI providers — `aithernet agents`

Aithernet is **bring-your-own-agent**: the coordinator and coding agents are selectable providers
behind canonical interfaces, chosen during setup. The preferred beta defaults are **Gemini CLI**
(coordinator) and **Codex CLI** (coding) — both still user-selectable, not hardcoded. Claude Code,
Anthropic, OpenAI-compatible and local endpoints remain genuine supported options. Each provider is
labeled with its truthful support level: `fully-supported-real-tested`, `implemented-auto-tested`,
`configurable-not-externally-tested`, or `unsupported`.

```
aithernet agents providers                          # supported providers + current readiness
aithernet agents configure coordinator --provider gemini_cli
aithernet agents configure coding --provider codex_cli
aithernet agents test coordinator                   # bounded readiness check (no token spend)
aithernet agents test coordinator --live            # one minimal real inference (coordinator)
aithernet agents provider-status                    # sanitised status (no secrets/paths)
aithernet agents remove coordinator                 # reset a role to disabled
```

Two runtime identity models are supported: **workstation** (provider auth belongs to the desktop
Linux user; CLI tools inherit that login) and **appliance** (headless; credentials supplied through
a service-configured environment variable, referenced by **name** — never stored as a value). A
provider is reported authenticated only after a bounded readiness request succeeds.

## 4. Doctor — `aithernet doctor`

A read-only readiness report aggregating core/identity/enrollment/setup/service/RF-stack/permissions
/devices/components/providers/disk/hosted-connectivity/mission state.

```
aithernet doctor                          # human-readable report
aithernet doctor --json                   # machine-readable
aithernet doctor --fix-plan               # deterministic remediation plan (makes no changes)
aithernet doctor --online                 # also probe hosted control-plane connectivity
```

States are explicit: `ready`, `optional`, `missing`, `misconfigured`, `unsupported`,
`permission-denied`, `device-absent`, `unverified`, `degraded`. Doctor never installs anything,
never invents a device it cannot see, never marks an executable authenticated merely because it
exists, and never prints secrets or full private paths.

## 5. SDR dependencies — `aithernet sdr`

Install and verify the SDR runtime for a hardware profile. Dependencies are scoped to the
**selected profile only** — never the whole Soapy/Pothosware ecosystem.

```
aithernet sdr plan --profile plutosdr                 # show the plan (pure, no changes)
aithernet sdr plan --profile plutosdr --mode source   # pinned source-build plan
aithernet sdr install --profile plutosdr              # plan only (no --yes)
aithernet sdr install --profile plutosdr --yes        # run it (apt; privileged)
aithernet sdr verify --profile plutosdr               # bounded runtime probes (read-only)
aithernet sdr status --profile plutosdr               # which RF runtimes are present
```

Modes: `recommended` (validated Ubuntu packages via apt), `source` (pinned, hashed source builds
into an Aithernet-owned prefix — never a moving branch), `offline` (a pre-staged local set, no
network). `plan` makes no changes and shows packages, sources, estimates, and every privileged
operation. `install` without `--yes` only prints the plan; the apt/source execution is privileged
and runs only with `--yes`.

**PlutoSDR runtime path** (what Aithernet actually uses): capture runs through **GNU Radio IIO**
(`gnuradio.iio.fmcomms2_source_fc32` over libiio, addressed by an IIO URI such as `ip:pluto.local`),
and discovery runs through **SoapySDR + the SoapyPlutoSDR module**. The Pluto profile therefore
installs exactly `gnuradio`, `gr-iio`, `libiio-*`, `soapysdr-tools`, and `soapysdr-module-plutosdr`
— no UHD, no `soapysdr-module-all`. Receive-only.

## 6. Missions — `aithernet mission`

Missions run on the **canonical** Aithernet mission engine (the coordinator loop, action router,
coding-task system, MCP/RF routes, events, artifacts, cancellation, and recovery). The customer
verbs are thin aliases over that one engine — there is no second mission system:

```
aithernet mission create "Survey 2.4 GHz Wi-Fi occupancy"   # alias of `mission submit`
aithernet mission run <mission-id>                          # alias of `mission start`
aithernet mission status <mission-id>                       # alias of `mission show`
aithernet mission events <mission-id>                       # canonical event timeline
aithernet mission artifacts <mission-id>                    # canonical RF artifacts (metadata)
aithernet mission cancel <mission-id>
aithernet mission resume <mission-id>
aithernet mission export <mission-id>                       # canonical record + timeline
```

### Receive-only RF pre-flight — `aithernet mission rf-plan`

Every RF mission is **receive-only**. `rf-plan` validates a capture request against the same
deterministic gate the canonical RF route + capture boundary enforce — the AI coordinator cannot
widen it. (The gate also runs automatically inside the engine; `rf-plan` is a no-hardware preview.)

```
aithernet mission rf-plan "Survey 2.4 GHz Wi-Fi occupancy" \
    --adapter plutosdr --device pluto-0 \
    --freq-hz 2437000000 --sample-rate 2000000 --duration-s 3 \
    --authorization "lab bench, antenna terminated" --show-bounds
```

The effective envelope is the **intersection** of the global receive-only policy, the selected
hardware **adapter** capabilities (device-specific limits like the Pluto 70 MHz–6 GHz / 61.44 MS/s
band live in the adapter — not the generic layer), the discovered device, and any operator policy.
Adapters: `software-only` (cannot receive), `simulation`, `plutosdr`, `usrp`, `generic-soapy`
(device-derived). No adapter is reported physically qualified until tested on real hardware.

The gate enforces: receive-only (any transmit direction is rejected — there is no transmit path);
the frequency band; sample-rate, duration and gain bounds; device authorization; and artifact
size/count limits (clamped down to the envelope, never widened). The natural-language objective is
recorded for audit but is **not** interpreted — safety is enforced at the parameter level, so a
hostile-sounding objective with a receive-only request still cannot transmit. The command emits a
frozen authorized plan with a SHA-256 digest, makes no changes, and touches no hardware.
