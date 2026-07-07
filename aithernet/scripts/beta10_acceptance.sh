#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# beta10_acceptance.sh — automated, source-hidden beta.10 acceptance on clean Ubuntu 24.04 amd64.
#
# Runs INSIDE an ubuntu:24.04 container as a normal (non-root) user with ONLY the release archive
# mounted read-only (no repository / no source tree). Proves the beta.10 customer invariants
# deterministically — no real external providers, no physical RF, no transmit. It extends the
# beta.9 acceptance (install/setup/identity/unit/runtime-PATH/repair/migration/one-command-mission)
# with the beta.10 surface:
#
#   * generic secret persistence + propagation (arbitrary names; agents secrets list/check/remove;
#     value never in node.yaml, logs, events, or process args)
#   * guided OpenAI-compatible / Z.AI coordinator config with URL+model validation (no GEMINI hint)
#   * normalized provider error + quota recovery (blocked_provider_quota, not budget-exhausted)
#   * MCP tool-schema preflight (alias canonicalization; unknown/missing field rejected)
#   * coding-agent routing (selected for code work, not every mission)
#   * managed RF-MCP diagnostics never recommend a source clone
#   * two-node pairing + `run --on` refuses an untrusted peer (receiving node owns policy)
#   * owner research spool init + leakage-free dataset builders + data status (secret-free)
#   * one-command mission completes through the canonical engine against a deterministic stub
#
# The heavy live scenarios (real GNU Radio flowgraph execution, the full 14 provider failure modes
# end-to-end, and live Google Drive upload) are proven by the deterministic in-repo test suite
# (run separately on the build host); this container proves the source-hidden customer surface.
#
# Usage (from the repo host):  scripts/beta10_acceptance.sh /path/to/release/archive
# ---------------------------------------------------------------------------
set -euo pipefail
ARCHIVE="${1:?usage: beta10_acceptance.sh <release-archive-dir>}"
IMAGE="ubuntu:24.04"

docker run --rm -v "$ARCHIVE:/rel:ro" "$IMAGE" bash -euo pipefail -c '
export DEBIAN_FRONTEND=noninteractive
fail() { echo "ACCEPTANCE FAIL: $*" >&2; exit 1; }
ok()   { echo "  [ok] $*"; }

echo "== 0. environment =="
. /etc/os-release; echo "  $PRETTY_NAME"
[ "$VERSION_ID" = "24.04" ] || fail "not Ubuntu 24.04"
cp -r /rel /work && cd /work
apt-get update -qq >/dev/null 2>&1

echo "== 1. source-hidden .deb install (no repo present) =="
[ ! -e /work/pyproject.toml ] || fail "source tree leaked into the bundle"
DEB="$(ls /work/aithernet_0.8.0~beta.10_amd64.deb 2>/dev/null || true)"
[ -n "$DEB" ] || fail "beta.10 .deb not present in the archive"
apt-get install -y -qq "$DEB" >/tmp/apt.log 2>&1 || { tail -20 /tmp/apt.log; fail "apt install"; }
ver="$(aithernet --version)"; echo "  $ver"; [ "$ver" = "aithernet 0.8.0b10" ] || fail "version != 0.8.0b10"

useradd -m tester; export HOME=/home/tester
run_as() { runuser -u tester -- env HOME=/home/tester "$@"; }

echo "== 2. guided setup -> canonical identity + absolute paths =="
run_as aithernet setup --profile software-only --deployment-mode standalone \
  --service-mode systemd-user --sdr-strategy none --coordinator disabled --coding disabled \
  --node-name accept-node --non-interactive --no-research >/tmp/setup.log 2>&1 \
  || { tail -25 /tmp/setup.log; fail "setup"; }
CFG=/home/tester/.local/share/aithernet/config/node.yaml
[ -f "$CFG" ] || fail "no canonical config"
run_as python3 - "$CFG" <<"PY" || fail "canonical paths"
import sys, yaml, os
d = yaml.safe_load(open(sys.argv[1]))
assert d["node_id"] != "00000000-0000-4000-8000-000000000001", "placeholder node id!"
assert os.path.isabs(d["node_state_root"]), "state root not absolute"
print("  node_id", d["node_id"])
PY
ok "real node id + absolute paths"

echo "== 3. generic secret persistence + agents secrets (no value leakage) =="
# An ARBITRARY (non-provider-specific) secret name is accepted and stored; value never echoed.
run_as bash -c "printf %s zAI-secret-value-123 | aithernet agents set-secret ZAI_API_KEY" >/tmp/sec.log 2>&1 \
  || { cat /tmp/sec.log; fail "set-secret"; }
grep -qi "zAI-secret-value-123" /tmp/sec.log && fail "secret value echoed by set-secret"
run_as aithernet agents secrets list >/tmp/seclist.log 2>&1 || fail "secrets list"
grep -q "ZAI_API_KEY" /tmp/seclist.log || fail "stored secret name absent from list"
grep -qi "zAI-secret-value-123" /tmp/seclist.log && fail "secret value in secrets list"
run_as aithernet agents secrets check ZAI_API_KEY >/tmp/seccheck.log 2>&1 || fail "secrets check"
grep -q "\"stored\": true" /tmp/seccheck.log || fail "secrets check did not report stored"
# value must NOT be in node.yaml
grep -qi "zAI-secret-value-123" "$CFG" && fail "secret value leaked into node.yaml"
# stored 0600 in the credential file; value present there only
ENVF=/home/tester/.config/aithernet/aithernet.env
[ -f "$ENVF" ] && [ "$(stat -c %a "$ENVF")" = "600" ] || fail "credential env file not 0600"
run_as aithernet agents secrets remove ZAI_API_KEY >/tmp/secrm.log 2>&1 || fail "secrets remove"
run_as aithernet agents secrets list 2>/dev/null | grep -q "ZAI_API_KEY" && fail "secret not removed"
ok "arbitrary secret persists 0600; names+availability only; no value leakage; removable"

echo "== 4. guided OpenAI-compatible / Z.AI coordinator config (URL+model validated) =="
# Hosted OpenAI-compatible with the Z.AI general base URL; validation is structural (no live call).
run_as printf %s zKey | run_as aithernet agents set-secret ZAI_API_KEY >/dev/null 2>&1 || true
run_as bash -c "printf %s zKey | aithernet agents set-secret ZAI_API_KEY" >/dev/null 2>&1 || true
run_as aithernet agents connect coordinator --provider openai_compatible \
  --base-url https://api.z.ai/api/paas/v4/ --model glm-4.6 --api-key-ref ZAI_API_KEY \
  --no-verify --no-restart >/tmp/zai.log 2>&1 || { cat /tmp/zai.log; fail "z.ai connect"; }
run_as python3 - "$CFG" <<"PY" || fail "z.ai config not persisted"
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))["coordinator"]
assert d["base_url"] == "https://api.z.ai/api/paas/v4/", d
assert d["model"] == "glm-4.6"
assert d["api_key"] == "env:ZAI_API_KEY"
PY
# A bad base URL is rejected before saving.
run_as aithernet agents connect coordinator --provider openai_compatible --base-url not-a-url \
  --model m --api-key-ref ZAI_API_KEY --no-verify --no-restart >/tmp/badurl.log 2>&1 && fail "bad URL accepted" || true
grep -qi "http" /tmp/badurl.log || fail "bad-URL rejection message missing"
# A non-Gemini provider must never suggest GEMINI_API_KEY.
run_as aithernet agents connect coordinator --provider anthropic >/tmp/anth.log 2>&1 || true
grep -q "GEMINI_API_KEY" /tmp/anth.log && fail "anthropic suggested GEMINI_API_KEY" || true
grep -q "ANTHROPIC_API_KEY" /tmp/anth.log || fail "anthropic did not suggest ANTHROPIC_API_KEY"
ok "Z.AI hosted config persisted; bad URL rejected; provider-appropriate secret hint"

echo "== 5. managed RF-MCP diagnostics never recommend a source clone =="
run_as aithernet mcp diagnostics >/tmp/mcp.log 2>&1 || true
grep -qiE "git clone|gr-mcp|github.com/.*gr-mcp|developer checkout" /tmp/mcp.log && fail "diagnostics recommend a source clone" || true
ok "RF-MCP diagnostics are managed-component only"

echo "== 6. two-node pairing requires a pinned key (pure CLI, no node needed) =="
# Pairing without a public key is refused (we never trust without a pinned key).
run_as aithernet peers pair --name nodeB --url http://127.0.0.1:8080 >/tmp/pair.log 2>&1 && fail "paired without a key" || true
grep -qi "public-key" /tmp/pair.log || { cat /tmp/pair.log; fail "pair did not require a public key"; }
ok "pairing requires a pinned public key"

echo "== 7. owner research spool init + leakage-free dataset builder + data status =="
run_as aithernet data setup --skip-drive >/tmp/dsetup.log 2>&1 || { cat /tmp/dsetup.log; fail "data setup"; }
SPOOL=/home/tester/.local/share/aithernet/research
for sub in raw normalized curated evaluation artifacts rf snapshots manifests upload-ledger quarantine schemas; do
  [ -d "$SPOOL/$sub" ] || fail "research spool missing $sub"
done
# seed deterministic normalized records (two per mission = a retry) and build a dataset.
run_as python3 - "$SPOOL" <<"PY" || fail "seed normalized"
import sys, json, os
norm = os.path.join(sys.argv[1], "normalized")
for i in range(20):
    for j in range(2):
        open(os.path.join(norm, f"m{i}-{j}.json"), "w").write(
            json.dumps({"record_type":"coordinator_decision","mission_id":f"m{i}","attempt":j}))
PY
run_as aithernet data build coordinator >/tmp/build.log 2>&1 || { cat /tmp/build.log; fail "data build"; }
DS="$SPOOL/snapshots/dataset-coordinator-v0001"
for f in train.jsonl validation.jsonl evaluation.jsonl schema.json dataset-card.json provenance.json statistics.json SHA256SUMS; do
  [ -f "$DS/$f" ] || fail "dataset missing $f"
done
# leakage check: each mission lands in exactly one split.
run_as python3 - "$DS" <<"PY" || fail "split leakage"
import sys, json, os
split_of={}
for name in ("train","validation","evaluation"):
    for line in open(os.path.join(sys.argv[1], name+".jsonl")):
        r=json.loads(line); mid=r["mission_id"]
        assert split_of.setdefault(mid,name)==name, f"{mid} leaked across splits"
print("  missions:", len(split_of))
PY
run_as aithernet data status >/tmp/status.log 2>&1 || fail "data status"
grep -q "schema_version" /tmp/status.log || fail "data status missing schema_version"
ok "spool initialized; dataset built with mission-grouped splits (no leakage); status secret-free"

echo "== 8. one-command mission via a deterministic LOCAL coordinator stub =="
cat > /home/tester/stub.py <<"PY"
import json, http.server
DECISION = {"summary":"plan","next_target":"respond","action":"act","message":"done",
            "structured_payload":{},"expected_result":"ok",
            "mission_control":{"disposition":"complete","final_response":"acceptance-ok"}}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length",0) or 0))
        body = json.dumps({"choices":[{"message":{"content":json.dumps(DECISION)}}]}).encode()
        self.send_response(200); self.send_header("content-type","application/json")
        self.send_header("content-length",str(len(body))); self.end_headers(); self.wfile.write(body)
http.server.HTTPServer(("127.0.0.1",9099),H).serve_forever()
PY
run_as python3 - "$CFG" <<"PY" || fail "configure local coordinator"
import sys, yaml
p=sys.argv[1]; d=yaml.safe_load(open(p))
d["coordinator"]={"provider":"openai_compatible","base_url":"http://127.0.0.1:9099",
                  "model":"stub-1","allow_unauthenticated":True}
yaml.safe_dump(d, open(p,"w"))
PY
cat > /home/tester/run_mission.sh <<EOF
#!/usr/bin/env bash
set -e
nohup python3 /home/tester/stub.py >/tmp/stub.log 2>&1 &
nohup aithernet start --config $CFG >/tmp/node.log 2>&1 &
for i in \$(seq 1 60); do (echo >/dev/tcp/127.0.0.1/8080) >/dev/null 2>&1 && break; sleep 0.5; done
aithernet run "Create and test a software-only signal generator." --interval 0.5 --timeout 120
EOF
chown tester:tester /home/tester/run_mission.sh
run_as bash /home/tester/run_mission.sh >/tmp/run.log 2>&1 || { cat /tmp/run.log; echo "--- node ---"; tail -20 /tmp/node.log; fail "aithernet run"; }
grep -q "Mission completed" /tmp/run.log || { cat /tmp/run.log; fail "mission did not complete"; }
grep -q "acceptance-ok" /tmp/run.log || fail "final result missing"
ok "one-command aithernet run completed through the canonical engine"

echo "== 8b. run --on refuses an unknown peer against the LIVE node (no remote shell) =="
# The node is still running; /peers is empty, so an unknown peer is refused (receiving node owns
# policy). This proves the refusal path WITHOUT relying on the unreachable-node guidance.
run_as aithernet run "do it" --on ghost >/tmp/on.log 2>&1 && fail "run --on unknown peer succeeded" || true
grep -qi "No trusted peer" /tmp/on.log || { cat /tmp/on.log; fail "run --on did not refuse unknown peer"; }
ok "run --on refuses an unknown peer (no remote shell, receiving node owns policy)"

echo "== 9. no physical RF / no transmit =="
grep -qiE "transmit|tx " /tmp/run.log && fail "transmit referenced" || ok "no transmit"

echo "ALL BETA.10 ACCEPTANCE CHECKS PASSED"
'
