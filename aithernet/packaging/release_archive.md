# Aithernet Release Archive Format (Stage 14F)

A release is a **deterministic** directory of artifacts plus integrity and
authenticity metadata. The same inputs always produce the same `manifest.json`
bytes (sorted keys, sorted artifact list), so signatures are reproducible.

## Layout

```
<DOWNLOAD_BASE_URL>/<version>/
├── manifest.json                          # authoritative metadata (signed)
├── manifest.sig                           # detached Ed25519 signature over manifest.json
├── SHA256SUMS                             # convenience checksums (coreutils format)
├── aithernet-0.8.0b1-py3-none-any.whl     # artifact (kind: wheel)
├── aithernet-0.8.0b1.tar.gz               # artifact (kind: sdist)
└── aithernet_0.8.0~beta.1_amd64.deb       # artifact (kind: deb)
```

The installer fetches everything relative to
`${DOWNLOAD_BASE_URL}/${AITHERNET_VERSION}/` (default base
`https://downloads.example.invalid`, a placeholder — never a real domain).
`file://` paths and `--offline <dir>` work identically for local testing.

## `manifest.json` schema

```jsonc
{
  "version": "0.8.0-beta.1",          // release version
  "channel": "beta",                   // stable | beta | nightly
  "signing_key_id": "aithernet-rel-2026-01",  // identifies the signing key
  "created": "2026-06-18T00:00:00Z",   // ISO-8601 UTC (informational)
  "artifacts": [                       // sorted by name for determinism
    {
      "name": "aithernet-0.8.0b1-py3-none-any.whl",
      "kind": "wheel",                 // wheel | sdist | deb | container
      "sha256": "<64-hex>",            // lowercase hex SHA-256 of the file
      "byte_size": 123456              // exact size in bytes
    },
    {
      "name": "aithernet_0.8.0~beta.1_amd64.deb",
      "kind": "deb",
      "sha256": "<64-hex>",
      "byte_size": 234567
    }
  ]
}
```

Field rules:

- `sha256` is the lowercase-hex digest of the **exact** file bytes.
- `byte_size` must equal the file size; the installer may cross-check it.
- `signing_key_id` lets clients pick the right public key when keys rotate.
- The `artifacts` array is sorted by `name`; object keys are emitted sorted.

## `SHA256SUMS`

Standard coreutils format (authoritative source is still `manifest.json`):

```
<64-hex>  aithernet-0.8.0b1-py3-none-any.whl
<64-hex>  aithernet-0.8.0b1.tar.gz
<64-hex>  aithernet_0.8.0~beta.1_amd64.deb
```

Verify locally with: `sha256sum -c SHA256SUMS`

## `manifest.sig`

A **detached Ed25519** signature computed over the raw bytes of
`manifest.json`. Only `manifest.json` is signed; artifact authenticity flows
transitively through the signed SHA-256 values inside it.

---

## Offline signing (release engineer)

Performed on an **air-gapped / offline** signing host. The private key never
leaves that host and is never bundled into any artifact.

Generate a signing keypair (one time):

```bash
# Ed25519 private key (keep OFFLINE, never commit, never ship):
openssl genpkey -algorithm ed25519 -out aithernet-release.key
# Public verification key (this one ships with the installer):
openssl pkey -in aithernet-release.key -pubout -out aithernet-release.pub
```

Build the manifest deterministically, then sign it:

```bash
# 1. Compute checksums for every artifact:
sha256sum *.whl *.tar.gz *.deb > SHA256SUMS

# 2. Emit manifest.json with sorted keys + sorted artifacts (use your tooling;
#    a small python script that json.dumps(..., sort_keys=True) is sufficient).

# 3. Sign the manifest bytes (detached Ed25519):
openssl pkeyutl -sign -inkey aithernet-release.key \
  -rawin -in manifest.json -out manifest.sig
```

Publish `manifest.json`, `manifest.sig`, `SHA256SUMS`, and the artifacts under
`<base>/<version>/`. Ship `aithernet-release.pub` inside the installer
(`scripts/aithernet-release.pub`).

---

## Verification (client / installer)

`scripts/install.sh` does this automatically (signature first, then per-artifact
SHA-256). The equivalent manual commands:

```bash
# 1. Verify the manifest signature with the bundled PUBLIC key:
openssl pkeyutl -verify -pubin -inkey aithernet-release.pub \
  -rawin -in manifest.json -sigfile manifest.sig

# 2. Only after that passes, verify artifact checksums:
sha256sum -c SHA256SUMS
```

`python3 -m` fallback (when `openssl` is unavailable), using the same key:

```python
from cryptography.hazmat.primitives.serialization import load_pem_public_key
pub = load_pem_public_key(open("aithernet-release.pub", "rb").read())
pub.verify(open("manifest.sig", "rb").read(),
           open("manifest.json", "rb").read())   # raises InvalidSignature on failure
```

If signature verification fails, the installer refuses to unpack or execute any
downloaded content.
