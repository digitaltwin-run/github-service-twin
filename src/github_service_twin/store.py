"""SQLite WAL state store for GitHub Service Twin and hash-chained sync outbox."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import TwinRepository
from .outbox import SCHEMA_SQL, enqueue_outbox_op, utc_now, verify_outbox_rows


class TwinStore:
    """Thread-safe SQLite WAL persistence for twin entities and durable outbox."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def register_repository(self, repo: TwinRepository) -> TwinRepository:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO repositories(
                    full_name, local_path, default_branch, upstream_remote, private, archived, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(full_name) DO UPDATE SET
                    local_path = excluded.local_path,
                    default_branch = excluded.default_branch,
                    upstream_remote = excluded.upstream_remote,
                    private = excluded.private,
                    archived = excluded.archived
                """,
                (
                    repo.full_name,
                    repo.local_path,
                    repo.default_branch,
                    repo.upstream_remote,
                    int(repo.private),
                    int(repo.archived),
                    utc_now(),
                ),
            )
        return repo

    def get_repository(self, full_name: str) -> TwinRepository | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM repositories WHERE full_name = ?", (full_name,)).fetchone()
        return self._row_to_repo(row) if row else None

    def list_repositories(self, owner: str | None = None) -> list[TwinRepository]:
        with self._lock:
            if owner:
                rows = self._conn.execute(
                    "SELECT * FROM repositories WHERE full_name LIKE ? ORDER BY full_name",
                    (f"{owner}/%",),
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM repositories ORDER BY full_name").fetchall()
        return [self._row_to_repo(r) for r in rows]

    def _row_to_repo(self, row: sqlite3.Row) -> TwinRepository:
        return TwinRepository(
            full_name=str(row["full_name"]),
            local_path=str(row["local_path"]),
            default_branch=str(row["default_branch"]),
            upstream_remote=str(row["upstream_remote"]),
            private=bool(row["private"]),
            archived=bool(row["archived"]),
        )

    def _next_number(self, repository: str) -> int:
        row_i = self._conn.execute(
            "SELECT COALESCE(MAX(number), 0) AS n FROM issues WHERE repository = ?", (repository,)
        ).fetchone()
        row_p = self._conn.execute(
            "SELECT COALESCE(MAX(number), 0) AS n FROM pull_requests WHERE repository = ?", (repository,)
        ).fetchone()
        return max(int(row_i["n"]), int(row_p["n"])) + 1

    def create_issue(
        self, repository: str, *, title: str, body: str, labels: list[str], author: str
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._conn:
            number = self._next_number(repository)
            self._conn.execute(
                """
                INSERT INTO issues(
                    repository, number, title, body, state, labels_json, author, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)
                """,
                (repository, number, title, body, json.dumps(labels), author, now, now),
            )
            enqueue_outbox_op(
                self._conn,
                idempotency_key=f"issue:{repository}:{number}",
                repository=repository,
                kind="create_issue",
                payload={"twin_number": number, "title": title, "body": body, "labels": labels},
            )
            return self.get_issue(repository, number)  # type: ignore[return-value]

    def get_issue(self, repository: str, number: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM issues WHERE repository = ? AND number = ?", (repository, number)
            ).fetchone()
        return self._row_to_issue(row) if row else None

    def list_issues(
        self, repository: str, *, state: str = "open", label: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            if state == "all":
                rows = self._conn.execute(
                    "SELECT * FROM issues WHERE repository = ? ORDER BY number DESC", (repository,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM issues WHERE repository = ? AND state = ? ORDER BY number DESC",
                    (repository, state),
                ).fetchall()
        items = [self._row_to_issue(r) for r in rows]
        if label:
            wanted = {part.strip() for part in label.split(",") if part.strip()}
            items = [it for it in items if wanted.issubset(set(it["labels"]))]
        return items

    def _row_to_issue(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "number": int(row["number"]),
            "repository": str(row["repository"]),
            "title": str(row["title"]),
            "body": str(row["body"]),
            "state": str(row["state"]),
            "labels": list(json.loads(str(row["labels_json"]))),
            "author": str(row["author"]),
            "upstream_number": row["upstream_number"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def create_or_get_pull_request(
        self,
        repository: str,
        *,
        title: str,
        body: str,
        head_ref: str,
        base_ref: str,
        head_sha: str,
        draft: bool,
        author: str,
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT * FROM pull_requests WHERE repository = ? AND head_ref = ? AND base_ref = ? AND state = 'open'",
                (repository, head_ref, base_ref),
            ).fetchone()
            if existing is not None:
                self._conn.execute(
                    "UPDATE pull_requests SET head_sha = ?, updated_at = ? WHERE id = ?",
                    (head_sha, now, int(existing["id"])),
                )
                return self.get_pull_request(repository, int(existing["number"])), False  # type: ignore[return-value]
            number = self._next_number(repository)
            self._conn.execute(
                """
                INSERT INTO pull_requests(
                    repository, number, title, body, head_ref, base_ref, head_sha,
                    state, draft, merged, author, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, 0, ?, ?, ?)
                """,
                (repository, number, title, body, head_ref, base_ref, head_sha, int(draft), author, now, now),
            )
            enqueue_outbox_op(
                self._conn,
                idempotency_key=f"pr:{repository}:{head_ref}:{base_ref}:{head_sha}",
                repository=repository,
                kind="ensure_pull_request",
                payload={
                    "twin_number": number,
                    "title": title,
                    "body": body,
                    "head_ref": head_ref,
                    "base_ref": base_ref,
                    "head_sha": head_sha,
                    "draft": draft,
                },
            )
            return self.get_pull_request(repository, number), True  # type: ignore[return-value]

    def get_pull_request(self, repository: str, number: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pull_requests WHERE repository = ? AND number = ?", (repository, number)
            ).fetchone()
        return self._row_to_pr(row) if row else None

    def list_pull_requests(
        self, repository: str, *, state: str = "open", head_ref: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM pull_requests WHERE repository = ?"
        params: list[Any] = [repository]
        if state != "all":
            query += " AND state = ?"
            params.append(state)
        if head_ref:
            branch = head_ref.split(":", 1)[1] if ":" in head_ref else head_ref
            query += " AND head_ref = ?"
            params.append(branch)
        query += " ORDER BY updated_at DESC, number DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_pr(r) for r in rows]

    def set_pr_upstream_number(self, repository: str, twin_number: int, upstream_number: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE pull_requests SET upstream_number = ?, updated_at = ? WHERE repository = ? AND number = ?",
                (upstream_number, utc_now(), repository, twin_number),
            )

    def mark_pr_merged(self, repository: str, number: int, *, merged_by: str) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._conn:
            pr = self.get_pull_request(repository, number)
            if pr is None:
                raise KeyError(f"PR #{number} not found in {repository}")
            self._conn.execute(
                "UPDATE pull_requests SET state = 'closed', merged = 1, merged_by = ?, updated_at = ? WHERE repository = ? AND number = ?",
                (merged_by, now, repository, number),
            )
            enqueue_outbox_op(
                self._conn,
                idempotency_key=f"validator_handoff:{repository}:{number}:{pr['head_sha']}",
                repository=repository,
                kind="validator_merge_handoff",
                payload={
                    "twin_number": number,
                    "head_ref": pr["head_ref"],
                    "base_ref": pr["base_ref"],
                    "head_sha": pr["head_sha"],
                    "twin_merged_by": merged_by,
                },
            )
            return self.get_pull_request(repository, number)  # type: ignore[return-value]

    def _row_to_pr(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "number": int(row["number"]),
            "repository": str(row["repository"]),
            "title": str(row["title"]),
            "body": str(row["body"]),
            "head_ref": str(row["head_ref"]),
            "base_ref": str(row["base_ref"]),
            "head_sha": str(row["head_sha"]),
            "state": str(row["state"]),
            "draft": bool(row["draft"]),
            "merged": bool(row["merged"]),
            "merged_by": row["merged_by"],
            "author": str(row["author"]),
            "upstream_number": row["upstream_number"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def add_commit_status(
        self,
        repository: str,
        *,
        sha: str,
        context: str,
        state: str,
        description: str,
        target_url: str,
        actor: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO commit_statuses(
                    repository, sha, context, state, description, target_url, actor, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (repository, sha, context, state, description, target_url, actor, now),
            )
            status_id = int(cur.lastrowid)
            enqueue_outbox_op(
                self._conn,
                idempotency_key=f"status:{repository}:{sha}:{context}:{state}:{status_id}",
                repository=repository,
                kind="commit_status",
                payload={"sha": sha, "context": context, "state": state, "description": description, "target_url": target_url},
            )
        return {
            "id": status_id,
            "sha": sha,
            "context": context,
            "state": state,
            "description": description,
            "target_url": target_url,
            "creator": {"login": actor},
            "created_at": now,
        }

    def list_commit_statuses(self, repository: str, sha: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM commit_statuses WHERE repository = ? AND sha = ? ORDER BY id DESC",
                (repository, sha),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "sha": str(r["sha"]),
                "context": str(r["context"]),
                "state": str(r["state"]),
                "description": str(r["description"]),
                "target_url": str(r["target_url"]),
                "creator": {"login": str(r["actor"])},
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    def add_review(
        self,
        repository: str,
        *,
        pr_number: int,
        commit_id: str,
        state: str,
        body: str,
        author: str,
        actor_role: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO pr_reviews(
                    repository, pr_number, commit_id, state, body, author, actor_role, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (repository, pr_number, commit_id, state, body, author, actor_role, now),
            )
            review_id = int(cur.lastrowid)
        return {
            "id": review_id,
            "commit_id": commit_id,
            "state": state,
            "body": body,
            "user": {"login": author},
            "actor_role": actor_role,
            "submitted_at": now,
        }

    def list_reviews(self, repository: str, pr_number: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pr_reviews WHERE repository = ? AND pr_number = ? ORDER BY id ASC",
                (repository, pr_number),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "commit_id": str(r["commit_id"]),
                "state": str(r["state"]),
                "body": str(r["body"]),
                "user": {"login": str(r["author"])},
                "actor_role": str(r["actor_role"]),
                "submitted_at": str(r["created_at"]),
            }
            for r in rows
        ]

    def list_outbox(self, *, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._conn.execute("SELECT * FROM outbox_ops WHERE status = ? ORDER BY id ASC", (status,)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM outbox_ops ORDER BY id ASC").fetchall()
        return [
            {
                "id": int(r["id"]),
                "idempotency_key": str(r["idempotency_key"]),
                "repository": str(r["repository"]),
                "kind": str(r["kind"]),
                "payload": json.loads(str(r["payload_json"])),
                "prev_hash": str(r["prev_hash"]),
                "entry_hash": str(r["entry_hash"]),
                "status": str(r["status"]),
                "upstream_ref": str(r["upstream_ref"]),
                "last_error": str(r["last_error"]),
                "created_at": str(r["created_at"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]

    def update_outbox_status(
        self, op_id: int, *, status: str, upstream_ref: str = "", last_error: str = ""
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE outbox_ops SET status = ?, upstream_ref = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status, upstream_ref, last_error, utc_now(), op_id),
            )

    def verify_outbox_chain(self) -> bool:
        return verify_outbox_rows(self.list_outbox())
