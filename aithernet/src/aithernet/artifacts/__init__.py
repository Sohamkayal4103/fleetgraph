"""Stage 13D distributed artifact subsystem: managed content-addressed store + transfer.

Binary artifact bytes live ONLY on disk in the managed store (content-addressed by SHA-256) —
never in SQLite, events, prompts, or dashboard JSON. The store enforces path safety
(no traversal / symlink escape / device / FIFO / socket), per-artifact and total/per-peer
quotas, a minimum-free-space floor, atomic commit only after digest+size verification, content
deduplication, and pinning-aware garbage collection.
"""

from aithernet.artifacts.store import ArtifactStore, ArtifactStoreError

__all__ = ["ArtifactStore", "ArtifactStoreError"]
