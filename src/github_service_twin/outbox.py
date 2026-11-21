"""SQLite schema and hash-chained durable outbox helpers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any

from .models import OUTBOX_PENDING

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS repositories (
    full_name TEXT PRIMARY KEY,
    local_path TEXT NOT NULL,
    default_branch TEXT NOT NULL,
    upstream_remote TEXT NOT NULL,
    private INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository TEXT NOT NULL,
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    state TEXT NOT NULL,
    labels_json TEXT NOT NULL,
    author TEXT NOT NULL,
    upstream_number INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(repository, number)
);

CREATE TABLE IF NOT EXISTS pull_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository TEXT NOT NULL,
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    head_ref TEXT NOT NULL,
    base_ref TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    state TEXT NOT NULL,
    draft INTEGER NOT NULL DEFAULT 0,
    merged INTEGER NOT NULL DEFAULT 0,
    merged_by TEXT,
    author TEXT NOT NULL,
    upstream_number INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(repository, number)
);

CREATE TABLE IF NOT EXISTS commit_statuses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository TEXT NOT NULL,
    sha TEXT NOT NULL,
    context TEXT NOT NULL,
    state TEXT NOT NULL,
    description TEXT NOT NULL,
    target_url TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pr_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    commit_id TEXT NOT NULL,
    state TEXT NOT NULL,
    body TEXT NOT NULL,
    author TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    repository TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    upstream_ref TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_entry_hash(prev_hash: str, idempotency_key: str, repository: str, kind: str, payload_json: str) -> str:
    raw = f"{prev_hash}|{idempotency_key}|{repository}|{kind}|{payload_json}".encode()
    return hashlib.sha256(raw).hexdigest()


def enqueue_outbox_op(
    conn: sqlite3.Connection,
    *,
    idempotency_key: str,
    repository: str,
    kind: str,
    payload: dict[str, Any],
) -> None:
    exists = conn.execute(
        "SELECT 1 FROM outbox_ops WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if exists is not None:
        return
    prev_row = conn.execute("SELECT entry_hash FROM outbox_ops ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = str(prev_row["entry_hash"]) if prev_row else "0" * 64
    payload_json = canonical_json(payload)
    entry_hash = compute_entry_hash(prev_hash, idempotency_key, repository, kind, payload_json)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO outbox_ops(
            idempotency_key, repository, kind, payload_json, prev_hash, entry_hash,
            status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (idempotency_key, repository, kind, payload_json, prev_hash, entry_hash, OUTBOX_PENDING, now, now),
    )


def verify_outbox_rows(ops: list[dict[str, Any]]) -> bool:
    prev_hash = "0" * 64
    for op in ops:
        payload_json = canonical_json(op["payload"])
        expected = compute_entry_hash(
            prev_hash,
            op["idempotency_key"],
            op["repository"],
            op["kind"],
            payload_json,
        )
        if op["prev_hash"] != prev_hash or op["entry_hash"] != expected:
            return False
        prev_hash = expected
    return True
