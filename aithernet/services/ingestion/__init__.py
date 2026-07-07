"""Aithernet ingestion service (Stage 14E, Part 15).

A SEPARATELY runnable service (not coupled to the node process) that receives authenticated,
idempotent, integrity-verified export batches from nodes over a private network, persists batch
+ record metadata durably, stores binary bundles through a blob abstraction, issues receipts,
and supports retention, deletion, and dataset lineage. It requires no public Internet.

It reuses shared crypto/signing helpers from ``aithernet.data`` (a library dependency only — it
does not start or import the node runtime).
"""
