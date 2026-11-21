"""Compatibility test against the real subactor/onedev-agent GitHubClient."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from github_service_twin.api_handlers import TwinService
from github_service_twin.models import ROLE_VALIDATOR_APP, TwinRepository
from github_service_twin.quota import QuotaGuard
from github_service_twin.server import start_background_server
from github_service_twin.store import TwinStore

ONEDEV_SRC = Path("/home/tom/github/subactor/onedev-agent/src")
PROCACHE_SRC = Path("/home/tom/github/dockuri/procache/src")


@pytest.mark.skipif(
    not (ONEDEV_SRC.is_dir() and PROCACHE_SRC.is_dir()),
    reason="subactor/onedev-agent or dockuri/procache checkout not present",
)
def test_real_onedev_github_client_works_against_local_twin(tmp_path: Path, git_repo_pair) -> None:
    for p in (str(ONEDEV_SRC), str(PROCACHE_SRC)):
        if p not in sys.path:
            sys.path.insert(0, p)

    from onedev_agent.github import GitHubClient  # type: ignore[import-not-found]

    store = TwinStore(tmp_path / "twin.sqlite3")
    store.register_repository(TwinRepository("maskservice/viewer", str(git_repo_pair.worktree)))
    store.create_issue(
        "maskservice/viewer",
        title="Autonomous issue",
        body="Fix hotspot",
        labels=["autonomy-ready"],
        author="owner",
    )
    quota = QuotaGuard()
    quota.set_quota(remaining=0, reset_at=9999999999, blocked=True)
    service = TwinService(store, quota)
    httpd, _, base_url = start_background_server(service)
    try:
        client = GitHubClient(base_url, "local-twin-placeholder", cache_path=tmp_path / "cache.sqlite3")

        repo = client.repository("maskservice/viewer")
        assert repo.full_name == "maskservice/viewer"
        assert repo.default_branch == "main"

        org_repos = client.organization_repositories("maskservice")
        assert [r.full_name for r in org_repos] == ["maskservice/viewer"]

        assert client.branch_sha("maskservice/viewer", "main") == git_repo_pair.main_sha
        assert client.branch_sha("maskservice/viewer", git_repo_pair.branch_name) == git_repo_pair.head_sha
        assert client.commit_is_ancestor("maskservice/viewer", git_repo_pair.main_sha, git_repo_pair.head_sha) is True

        pr = client.ensure_pull_request(
            "maskservice/viewer",
            title="Offline PR via OneDev client",
            body="Delivered during GitHub API outage",
            head_ref=git_repo_pair.branch_name,
            head_sha=git_repo_pair.head_sha,
        )
        assert pr.head_sha == git_repo_pair.head_sha
        assert pr.head_ref == git_repo_pair.branch_name

        open_prs = client.open_pull_requests("maskservice/viewer")
        assert [p.number for p in open_prs] == [pr.number]

        client.create_commit_status(
            "maskservice/viewer",
            pr.head_sha,
            context="onedev/local-verify",
            state="success",
            description="3 local gates passed",
        )

        # Submit independent validator review on exact head
        req = Request(
            f"{base_url}/repos/maskservice/viewer/pulls/{pr.number}/reviews",
            data=json.dumps({"event": "APPROVE", "commit_id": pr.head_sha, "body": "OK"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Twin-Actor": "ifuri-validator-agent[bot]",
                "X-Twin-Role": ROLE_VALIDATOR_APP,
            },
            method="POST",
        )
        with urlopen(req, timeout=5) as resp:
            assert resp.status == 201

        approval_id, submitted_at = client.validator_approval_for_head(
            "maskservice/viewer",
            pr.number,
            head_sha=pr.head_sha,
            reviewer_login="ifuri-validator-agent[bot]",
        )
        assert approval_id >= 1
        assert submitted_at.endswith("Z")

        issues = client.labeled_open_issues("maskservice/viewer", label="autonomy-ready")
        assert len(issues) == 1
        assert issues[0].title == "Autonomous issue"

        snap = client.branch_lifecycle_snapshot("maskservice/viewer")
        assert snap["schema"] == "new-project.branch-lifecycle-snapshot/v1"
        assert snap["defaultBranch"] == "main"
        assert snap["deleteBranchOnMerge"] is True
        assert git_repo_pair.branch_name in snap["branches"]
    finally:
        httpd.shutdown()
        store.close()
