# Agent providers (1.0.0-beta.5)

Aithernet is provider-neutral. The **coordinator** (planning, routing, recovery, review) and the
**coding agent** (bounded workspace implementation and testing) are configured independently, behind
one canonical provider interface, so you can mix providers freely. No single vendor, subscription,
API, cloud, or authentication mechanism is required.

## Roles

| Role | What it does | Required capabilities |
|------|--------------|-----------------------|
| coordinator | Reasons over a mission and returns a structured decision | `reasoning`, `structured_decisions` |
| coding | Implements + tests in an authorized workspace | `coding`, `workspace_editing` |

A provider is eligible for a role only if it declares the role's capabilities. Coding is **never**
inferred from the ability to produce text — a provider must explicitly support coding.

## Supported coordinator providers

| Provider | Auth | Billing |
|----------|------|---------|
| `claude_cli` | Claude.ai Pro/Max login (via the `claude` executable) | subscription allowance |
| `anthropic` | Anthropic API key | separate API billing |
| `anthropic_bedrock` | AWS Bedrock credentials | cloud-provider billing |
| `anthropic_vertex` | Google Vertex AI credentials | cloud-provider billing |
| `openai_api` | OpenAI API key | separate API billing |
| `openai_compatible` | Any OpenAI-compatible endpoint + key | separate API billing |
| `openai_compatible_local` | Local OpenAI-compatible server | local compute |
| `catgpt_gateway` | Local CatGPT-Gateway browser session (subscription-backed, OpenAI-compatible) — **coordinator only**; api key optional | local compute (your subscription) |
| `gemini_api` | Gemini API key | separate API billing |
| `gemini_vertex` | Google Vertex AI credentials | cloud-provider billing |
| `gemini_cli` | Installed Google CLI (`gemini`) login | cloud-provider billing |
| `disabled` | — | none |

## Supported coding providers

| Provider | Auth | Billing |
|----------|------|---------|
| `codex_cli` | ChatGPT subscription sign-in (via `codex`) | subscription allowance |
| `codex_api` | OpenAI API key | separate API billing |
| `claude_code` | Claude.ai Pro/Max login (via `claude`) | subscription allowance |
| `local_coding` | Local OpenAI-compatible coding endpoint | local compute |
| `disabled` | — | none |

## Billing — read this

* **Claude Pro/Max and Claude Code share the allowance of the authenticated Claude account** when
  both roles use that account.
* **Codex** may use a supported ChatGPT subscription login.
* **Anthropic, OpenAI, and Gemini API usage is billed separately** by the corresponding API or cloud
  account. A consumer subscription does **not** include unrelated API credits.
* **Local providers use your own compute** and need no account or internet connection.

## Configure

```bash
aithernet agents list-providers              # capabilities + auth + billing for every provider
aithernet agents inspect-provider claude_cli # one provider in detail
aithernet agents connect coordinator         # guided coordinator setup
aithernet agents connect coding              # guided coding setup
aithernet agents disconnect coding           # set a role to disabled
aithernet agents provider-status             # readiness, no secrets
aithernet agents test coordinator --live     # one bounded real request
```

Credentials are referenced by an environment-variable **name** (e.g. `env:ANTHROPIC_API_KEY`) and
stored only in the protected secret store — never in `node.yaml`, datasets, logs, artifacts, peers,
Drive, or the portal.

## Model selection

Pick a model explicitly, or set a capability policy (`strongest_reasoning_available`, `balanced`,
`fast`, `lowest_cost`, `operator_selected`). The coordinator and coding models may differ. Aithernet
records the requested model and the effective model the provider used; it never hard-codes model
names that change over time.

## Capacity pools

Capacity is the real account relationship. Two roles on the **same account share one pool** (e.g. a
Claude coordinator + Claude Code coding share one Claude allowance); roles on different accounts have
independent pools. Pool ids are non-secret fingerprints.

```bash
aithernet agents capacity            # pools for the configured roles
aithernet agents capacity --all
```

A pool surfaces quota-exhausted / rate-limited state and a retry-after where the provider exposes it.
Aithernet never invents exact remaining usage a provider does not report.

## Fallback chains

Each role may declare an **explicit** fallback chain. Fallback is only triggered by provider-
availability conditions (quota exhausted, rate limited, subscription/credit unavailable, endpoint or
model unavailable), is always **recorded**, and is **never silent**. Each fallback provider uses its
**own** configuration and credential — one provider's credentials are never used to call another.

```bash
aithernet agents fallbacks coordinator
aithernet agents set-fallbacks coordinator anthropic,openai_api,openai_compatible_local
```

## Presets (recommendations only)

```bash
aithernet agents presets
```

`reasoning_first`, `coding_first`, `lowest_cost`, `fully_local`, `provider_diverse`,
`single_provider_claude`. Presets never restrict manual combinations and are never auto-applied to a
billable provider without your explicit choice.

## Privacy & credential handling

* Instructions are passed on stdin where possible; secrets never appear in command arguments.
* No provider credentials are written to logs, research records, Drive, peer messages, or the portal.
* Aithernet does not collect any model's hidden chain-of-thought — only observable plans, structured
  decisions, tool calls, patches, tests, usage, and outcomes.

## Deprecated paths

The previous consumer Google-login flow for the Gemini CLI is **not** offered as a current
authentication path. `gemini_cli` uses the installed official CLI's current Google Cloud
authentication. Google AI Pro alone does not guarantee Gemini CLI access.
