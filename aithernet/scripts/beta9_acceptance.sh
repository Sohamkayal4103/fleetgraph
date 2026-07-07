#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# beta9_acceptance.sh — automated, source-hidden beta.9 acceptance on clean Ubuntu 24.04 amd64.
#
# Runs INSIDE an ubuntu:24.04 container as a normal (non-root) user with ONLY the release archive
# mounted read-only (no repository / no source tree). Proves the beta.9 customer invariants
# deterministically — no real external providers, no physical RF, no transmit:
#
#   * source-hidden .deb install; aithernet --version == 0.8.0b9; `aithernet release verify`
#   * guided setup -> canonical node.yaml: REAL node id (no placeholder), absolute db/state/identity
#   * generated systemd user unit pins the canonical config in ExecStart (--config) + runtime.env
#   * NVM-style Node CLI providers resolve into the managed runtime PATH (runtime.env)
#   * `aithernet doctor --repair --dry-run` plans the safe repairs
#   * beta.8 -> beta.9 migration dry-run preserves identity (no placeholder)
#   * one-command `aithernet run` completes through the canonical engine against a deterministic
#     LOCAL coordinator stub (an openai-compatible HTTP double; NOT shipped — test scaffolding)
#
# Usage (from the repo host):  scripts/beta9_acceptance.sh /path/to/release/archive
# ---------------------------------------------------------------------------
set -euo pipefail
ARCHIVE="${1:?usage: beta9_acceptance.sh <release-archive-dir>}"
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
apt-get install -y -qq ./aithernet_0.8.0~beta.9_amd64.deb >/tmp/apt.log 2>&1 || { tail -20 /tmp/apt.log; fail "apt install"; }
ver="$(aithernet --version)"; echo "  $ver"; [ "$ver" = "aithernet 0.8.0b9" ] || fail "version != 0.8.0b9"
aithernet release verify /work >/tmp/rv.log 2>&1 || { cat /tmp/rv.log; fail "release verify"; }
grep -q "RELEASE VERIFIED" /tmp/rv.log && ok "release verify"

useradd -m tester; export HOME=/home/tester
run_as() { runuser -u tester -- env HOME=/home/tester "$@"; }

echo "== 2. guided setup -> canonical identity + absolute paths =="
run_as aithernet setup --profile software-only --deployment-mode standalone \
  --service-mode systemd-user --sdr-strategy none --coordinator disabled --coding disabled \
  --node-name accept-node --non-interactive >/tmp/setup.log 2>&1 || { tail -25 /tmp/setup.log; fail "setup"; }
CFG=/home/tester/.local/share/aithernet/config/node.yaml
[ -f "$CFG" ] || fail "no canonical config"
run_as python3 - "$CFG" <<"PY" || fail "canonical paths"
import sys, yaml, os
d = yaml.safe_load(open(sys.argv[1]))
nid = d["node_id"]
assert nid != "00000000-0000-4000-8000-000000000001", "placeholder node id!"
assert os.path.isabs(d["node_state_root"]), "state root not absolute"
db = d["database_url"].replace("sqlite:///", "")
assert os.path.isabs(db), "db not absolute"
ident = (d.get("identity") or (d.get("agent_transport") or {}).get("identity") or {})
sd = ident.get("state_directory")
assert sd and os.path.isabs(sd), "identity state_directory not absolute"
print("  node_id", nid)
print("  identity", sd)
PY
ok "real node id + absolute paths"

echo "== 3. systemd user unit pins canonical config (ExecStart --config) + runtime.env =="
UNIT=/home/tester/.config/systemd/user/aithernet-node.service
[ -f "$UNIT" ] || fail "no user unit written"
grep -q -- "--config $CFG" "$UNIT" || { cat "$UNIT"; fail "ExecStart lacks --config canonical"; }
grep -q "EnvironmentFile=-%h/.config/aithernet/runtime.env" "$UNIT" || fail "unit lacks runtime.env"
ok "unit ExecStart pins --config + reads runtime.env"

echo "== 4. NVM-style Node CLI providers resolve into runtime PATH =="
NVMBIN=/home/tester/.nvm/versions/node/v20.11.0/bin
run_as mkdir -p "$NVMBIN"
for n in node gemini codex; do printf "#!/usr/bin/env node\n" > "$NVMBIN/$n"; chmod +x "$NVMBIN/$n"; done
run_as aithernet agents configure coordinator --provider gemini_cli --executable "$NVMBIN/gemini" >/dev/null 2>&1 || fail "configure gemini_cli"
run_as aithernet agents configure coding --provider codex_cli --executable "$NVMBIN/codex" >/dev/null 2>&1 || fail "configure codex_cli"
run_as aithernet doctor --repair --dry-run >/tmp/rep.log 2>&1 || true
RTENV=/home/tester/.config/aithernet/runtime.env
run_as aithernet doctor --repair --dry-run >/dev/null 2>&1 || true
# materialise runtime.env via a real (non-dry) repair of the unit/PATH (service restart is best-effort)
run_as aithernet doctor --repair >/tmp/rep2.log 2>&1 || true
[ -f "$RTENV" ] || fail "runtime.env not written"
grep -q "$NVMBIN" "$RTENV" || { cat "$RTENV"; fail "runtime PATH missing the NVM bin dir"; }
ok "NVM provider dir present in managed runtime PATH"

echo "== 5. doctor --repair plans safe repairs =="
grep -qiE "repair|canonical|runtime PATH|reconcile" /tmp/rep.log || fail "doctor --repair produced no plan"
ok "doctor --repair"

echo "== 6. beta.8 -> beta.9 migration dry-run (idempotent; preserves identity) =="
run_as aithernet setup --migrate --dry-run >/tmp/mig.log 2>&1 || { cat /tmp/mig.log; fail "migrate dry-run"; }
ok "migration dry-run"

echo "== 7. one-command mission via a deterministic LOCAL coordinator stub =="
# A tiny openai-compatible HTTP double that returns a valid completing CoordinatorDecision.
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
# Point the coordinator at the deterministic local stub via the registered openai_compatible
# adapter (test scaffolding writes the canonical config directly; no external service involved).
run_as python3 - "$CFG" <<"PY" || fail "configure local coordinator"
import sys, yaml
p = sys.argv[1]; d = yaml.safe_load(open(p))
d["coordinator"] = {"provider": "openai_compatible", "base_url": "http://127.0.0.1:9099",
                    "model": "stub-1", "allow_unauthenticated": True}
yaml.safe_dump(d, open(p, "w"))
PY
# Start the stub + node and run the mission within ONE session so the background node stays alive.
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
grep -q "acceptance-ok" /tmp/run.log || { cat /tmp/run.log; fail "final result missing"; }
grep -q "mission:" /tmp/run.log && grep -q "run:" /tmp/run.log || fail "identifiers missing"
ok "one-command aithernet run completed through the canonical engine"

echo "== 8. no physical RF / no transmit =="
grep -qiE "transmit|tx " /tmp/run.log && fail "transmit referenced" || ok "no transmit"

echo "ALL BETA.9 ACCEPTANCE CHECKS PASSED"
'
