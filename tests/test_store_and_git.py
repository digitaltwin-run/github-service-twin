"""Tests for Git backend, QuotaGuard, and SQLite hash-chained outbox."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from github_service_twin.git_backend import compare_refs, list_branches, resolve_ref_sha
from github_service_twin.models import TwinRepository
from github_service_twin.quota import QuotaGuard
from github_service_twin.store import TwinStore


def test_git_backend_reads_real_branches_and_compare(git_repo_pair) -> None:
    branches = {b.name: b.sha for b in list_branches(git_repo_pair.worktree)}
    assert branches["main"] == git_repo_pair.main_sha
    assert branches[git_repo_pair.branch_name] == git_repo_pair.head_sha
    assert resolve_ref_sha(git_repo_pair.worktree, git_repo_pair.branch_name) == git_repo_pair.head_sha

    cmp = compare_refs(git_repo_pair.worktree, "main", git_repo_pair.branch_name)
    assert cmp.status == "ahead"
    assert cmp.ahead_by == 1
    assert cmp.behind_by == 0


def test_quota_guard_reads_budget_file_and_recovers_after_reset(tmp_path: Path) -> None:
    budget_file = tmp_path / "api-budget-github.json"
    budget_file.write_text(
        json.dumps({"provider": "github", "remaining": 0, "reset_at": 2000}),
        encoding="utf-8",
    )
    now = [1500]
    guard = QuotaGuard(budget_file=budget_file, reserve=50, clock=lambda: now[0])

    snap_blocked = guard.current()
    assert snap_blocked.is_exhausted(now[0]) is True
    assert snap_blocked.mode(now[0]) == "quota-deferred"

    now[0] = 2005
    snap_recovered = guard.current()
    assert snap_recovered.is_exhausted(now[0]) is False
    assert snap_recovered.mode(now[0]) == "live-ready"


def test_outbox_hash_chain_detects_tampering(tmp_path: Path, git_repo_pair) -> None:
    db_path = tmp_path / "twin.sqlite3"
    store = TwinStore(db_path)
    try:
        store.register_repository(TwinRepository("maskservice/viewer", str(git_repo_pair.worktree)))
        store.create_issue("maskservice/viewer", title="Bug", body="desc", labels=["auto"], author="agent")
        store.add_commit_status(
            "maskservice/viewer",
            sha=git_repo_pair.head_sha,
            context="onedev/local-verify",
            state="success",
            description="passed",
            target_url="",
            actor="onedev",
        )
        assert store.verify_outbox_chain() is True
    finally:
        store.close()

    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE outbox_ops SET payload_json = '{\"tampered\":true}' WHERE id = 1")
    conn.commit()
    conn.close()

    reopened = TwinStore(db_path)
    try:
        assert reopened.verify_outbox_chain() is False
    finally:
        reopened.close()
