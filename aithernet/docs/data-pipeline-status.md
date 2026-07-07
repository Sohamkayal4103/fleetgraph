# Data pipeline — honest per-layer test status (PHASE 8 #11 / corrective COMMIT 4)

Code presence ≠ acceptance. This corrects any earlier "end-to-end data path proven" wording: the
fixture test in `test_data_path_e2e` proves the **local** seal → spool → verify → restore legs
(B/F/H/K) and the node↔operator-Drive separation — it does **not** cross the authenticated hosted
ingestion, real PostgreSQL/MinIO, real Google Drive, or real-consented-data boundaries.

Status flags per layer:
- **impl** — implemented in canonical code
- **auto** — covered by automated tests (with the stand-in noted)
- **fixture** — exercised end-to-end in `test_data_path_e2e` with non-sensitive fixtures
- **external** — tested against the real external service (real Postgres / MinIO / Google)
- **real-data** — tested with real consented beta-user data
- **stand-in** — the substitute used in automated tests

| Layer | Canonical code | impl | auto | fixture | external | real-data |
|------|----------------|:----:|:----:|:------:|:--------:|:---------:|
| A. local mission trajectory / event recording | `state` Event, `data/trajectories.py`, `data/events.py` | ✅ | ✅ | ✅ | — | — |
| B. local durable export spool | `data/batch.py`, `data/worker.py`, `destinations/local.py` | ✅ | ✅ | ✅ | n/a | — |
| C. authenticated node→hosted ingestion | `services/ingestion`, `data/ingest_auth.py`, `destinations/http.py` | ✅ | ✅ (stand-in: SQLite + in-process service; signed-request auth + replay + idempotency) | — | ❌ not over a real network | — |
| D. PostgreSQL metadata persistence | control plane / ingestion repositories | ✅ | ✅ (stand-in: **SQLite**, not real Postgres) | — | ❌ not real Postgres | — |
| E. MinIO / object artifact persistence | `data/blobstore.py`, ingestion blob store | ✅ | ✅ (stand-in: **local filesystem** blob root, not real MinIO) | — | ❌ not real MinIO | — |
| F. immutable dataset construction | `data/batch.py` seal_batch | ✅ | ✅ | ✅ | n/a | — |
| G. dataset validation + lineage | `data/service.py` dataset versions | ✅ | ✅ | — | — | — |
| H. encryption + archive packaging | `data/crypto.py`, `data/batch.py` | ✅ | ✅ | ✅ | n/a | — |
| I. real Google Drive upload | `data/destinations/google_drive_client.py`, `destinations/drive.py` | ✅ | ✅ (stand-in: FakeDriveClient + RealGoogleDriveClient over an httpx **MockTransport**, no Google account) | — | ❌ no real Drive | — |
| J. Drive digest/byte verification | `drive verify` | ✅ | ✅ (stand-in: mock transport) | — | ❌ | — |
| K. isolated restore | `data/batch.py` open_bundle | ✅ | ✅ | ✅ | n/a | — |

## What is and is not proven
- **Proven (fixtures, this work):** B, F, H, K end to end — a canonical-trajectory dataset is sealed
  + encrypted (the plaintext objective is absent from the bundle), spooled to the operator-side
  archive destination, byte/digest verified, and restored in isolation; tamper + wrong-key fail
  closed. The customer node does **not** carry the operator Google Drive OAuth credentials and does
  **not** upload directly to Drive (Drive destination disabled by default; credentials by env-ref).
- **Implemented + auto-tested against stand-ins (not real services):** C (ingestion, SQLite +
  in-process), D (SQLite, not Postgres), E (local FS, not MinIO), I/J (Drive mock transport).
- **Not tested:** the real node→hosted→Postgres/MinIO→dataset→**real** Drive crossing, and anything
  with real consented beta-user data. Those are operator/network/consent gates (real ingestion
  endpoint, real Postgres/MinIO, real Google Drive OAuth, real participants) and are **not** claimed.

No real beta-user data is uploaded; Drive remains a secondary encrypted archive reached only via the
hosted side, never directly from a customer node.
