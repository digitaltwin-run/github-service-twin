"""End-to-end test: quota exhausted -> local Twin execution -> quota unlocked -> upstream sync."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from github_service_twin.api_handlers import TwinService
from github_service_twin.models import (
    OUTBOX_PENDING,
    OUTBOX_RECONCILED,
    ROLE_CODING_AGENT,
    ROLE_ONEDEV_VERIFIER,
    ROLE_VALIDATOR_APP,
    TwinRepository,
)
from github_service_twin.quota import QuotaGuard
from github_service_twin.reconciler import UpstreamGitHubClient, UpstreamReconciler
from github_service_twin.server import start_background_server
from github_service_twin.store import TwinStore


def _json_call(url: str, method: str = "GET", body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
    hdrs = {"Accept": "application/json", **(headers or {})}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=hdrs, method=method)
    with urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_full_offline_twin_to_upstream_reconciliation(tmp_path: Path, git_repo_pair) -> None:
    # 1. Start a simulated real GitHub upstream backed by the bare remote repo
    up_store = TwinStore(tmp_path / "upstream.sqlite3")
    up_store.register_repository(TwinRepository("maskservice/viewer", str(git_repo_pair.bare_upstream)))
    up_quota = QuotaGuard()
    up_quota.set_quota(remaining=0, reset_at=9999999999, blocked=True, source="upstream-sim")
    up_httpd, _, up_url = start_background_server(TwinService(up_store, up_quota))

    # 2. Start local GitHub Digital Twin pointing at working checkout while API quota is 0
    twin_store = TwinStore(tmp_path / "twin.sqlite3")
    twin_store.register_repository(TwinRepository("maskservice/viewer", str(git_repo_pair.worktree)))
    twin_quota = QuotaGuard()
    twin_quota.set_quota(remaining=0, reset_at=9999999999, blocked=True, source="rate-limit-hit")

    handoff_calls: list[tuple[str, int, str]] = []

    def _independent_validator(full_name: str, pr_number: int, sha: str, _repo: TwinRepository) -> tuple[bool, str]:
        handoff_calls.append((full_name, pr_number, sha))
        val_h = {"X-Twin-Actor": "ifuri-validator-agent[bot]", "X-Twin-Role": ROLE_VALIDATOR_APP}
        _json_call(
            f"{up_url}/repos/{full_name}/pulls/{pr_number}/reviews",
            "POST",
            {"event": "APPROVE", "commit_id": sha, "body": "Independent upstream approval"},
            val_h,
        )
        _json_call(
            f"{up_url}/repos/{full_name}/pulls/{pr_number}/merge",
            "PUT",
            {"sha": sha},
            val_h,
        )
        return True, f"Validator App merged upstream PR #{pr_number}"

    reconciler = UpstreamReconciler(
        twin_store,
        twin_quota,
        UpstreamGitHubClient(up_url),
        validator_handoff=_independent_validator,
        push_branches=True,
    )
    twin_httpd, _, twin_url = start_background_server(TwinService(twin_store, twin_quota, reconciler=reconciler))

    try:
        coder_h = {"X-Twin-Actor": "coding-agent", "X-Twin-Role": ROLE_CODING_AGENT}
        onedev_h = {"X-Twin-Actor": "onedev-executor", "X-Twin-Role": ROLE_ONEDEV_VERIFIER}
        val_h = {"X-Twin-Actor": "ifuri-validator-agent[bot]", "X-Twin-Role": ROLE_VALIDATOR_APP}

        # Execute full task offline in the Digital Twin while GitHub API is exhausted
        issue = _json_call(
            f"{twin_url}/repos/maskservice/viewer/issues",
            "POST",
            {"title": "Offline task issue", "body": "Created while GitHub API down", "labels": ["offline"]},
            coder_h,
        )
        assert issue["number"] == 1

        pr = _json_call(
            f"{twin_url}/repos/maskservice/viewer/pulls",
            "POST",
            {"title": "Deliver ticket-007", "head": git_repo_pair.branch_name, "base": "main"},
            coder_h,
        )
        assert pr["head"]["sha"] == git_repo_pair.head_sha

        _json_call(
            f"{twin_url}/repos/maskservice/viewer/statuses/{git_repo_pair.head_sha}",
            "POST",
            {"context": "onedev/local-verify", "state": "success", "description": "All tests green"},
            onedev_h,
        )
        _json_call(
            f"{twin_url}/repos/maskservice/viewer/pulls/{pr['number']}/reviews",
            "POST",
            {"event": "APPROVE", "commit_id": git_repo_pair.head_sha, "body": "Twin validated"},
            val_h,
        )
        _json_call(
            f"{twin_url}/repos/maskservice/viewer/pulls/{pr['number']}/merge",
            "PUT",
            {"sha": git_repo_pair.head_sha},
            val_h,
        )

        # Verify sync stays deferred while upstream quota is still 0
        blocked_sync = _json_call(f"{twin_url}/twin/sync", "POST", {"apply": True})
        assert blocked_sync["mode"] == "quota-deferred"
        assert blocked_sync["reconciled"] == 0
        assert blocked_sync["deferred"] == 4

        # Unlock upstream GitHub API quota
        _json_call(f"{up_url}/twin/quota", "POST", {"remaining": 4980, "reset_at": 0, "blocked": False})
        _json_call(f"{twin_url}/twin/quota", "POST", {"remaining": 4980, "reset_at": 0, "blocked": False})

        # Run reconciliation: pushes git branch, creates upstream issue/PR, replays status, delegates merge to Validator App
        applied = _json_call(f"{twin_url}/twin/sync", "POST", {"apply": True})
        assert applied["mode"] == "live-ready"
        assert applied["reconciled"] == 4
        assert applied["conflicts"] == 0
        assert applied["deferred"] == 0
        assert len(handoff_calls) == 1
        assert handoff_calls[0][2] == git_repo_pair.head_sha

        # Confirm branch was actually pushed to the bare upstream Git repository
        pushed_sha = subprocess.check_output(
            ["git", "-C", str(git_repo_pair.bare_upstream), "rev-parse", f"refs/heads/{git_repo_pair.branch_name}"],
            text=True,
        ).strip()
        assert pushed_sha == git_repo_pair.head_sha

        # Confirm upstream GitHub state matches exact head & validator merge actor
        up_prs = _json_call(f"{up_url}/repos/maskservice/viewer/pulls?state=all")
        assert len(up_prs) == 1
        assert up_prs[0]["merged"] is True
        assert up_prs[0]["merged_by"]["login"] == "ifuri-validator-agent[bot]"
        assert up_prs[0]["head"]["sha"] == git_repo_pair.head_sha

        # Confirm all outbox entries are RECONCILED and hash chain is intact
        assert twin_store.list_outbox(status=OUTBOX_PENDING) == []
        assert len(twin_store.list_outbox(status=OUTBOX_RECONCILED)) == 4
        assert twin_store.verify_outbox_chain() is True
    finally:
        twin_httpd.shutdown()
        up_httpd.shutdown()
        twin_store.close()
        up_store.close()
