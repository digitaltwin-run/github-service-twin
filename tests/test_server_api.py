"""HTTP API and strict actor-boundary tests for github-service-twin."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from github_service_twin.api_handlers import TwinService
from github_service_twin.models import (
    ROLE_CODING_AGENT,
    ROLE_ONEDEV_VERIFIER,
    ROLE_VALIDATOR_APP,
    TwinRepository,
)
from github_service_twin.quota import QuotaGuard
from github_service_twin.server import start_background_server
from github_service_twin.store import TwinStore


def _req(
    url: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any, dict[str, str]]:
    req_headers = {"Accept": "application/json", **(headers or {})}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data is not None:
        req_headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=req_headers, method=method)
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), dict(resp.headers.items())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8")), dict(exc.headers.items())


def test_actor_boundary_and_twin_facts(tmp_path: Path, git_repo_pair) -> None:
    store = TwinStore(tmp_path / "twin.sqlite3")
    store.register_repository(TwinRepository("maskservice/viewer", str(git_repo_pair.worktree)))
    quota = QuotaGuard()
    quota.set_quota(remaining=0, reset_at=9999999999, blocked=True)
    service = TwinService(store, quota)
    httpd, _, base_url = start_background_server(service)
    try:
        status, facts, _ = _req(f"{base_url}/twin/facts")
        assert status == 200
        assert facts["schema"] == "subactor.twin-fact/v1"
        assert facts["payload"]["mode"] == "quota-deferred"
        assert len(facts["payload_sha256"]) == 64

        coder_h = {"X-Twin-Actor": "coding-agent", "X-Twin-Role": ROLE_CODING_AGENT}
        onedev_h = {"X-Twin-Actor": "onedev-executor", "X-Twin-Role": ROLE_ONEDEV_VERIFIER}
        val_h = {"X-Twin-Actor": "ifuri-validator-agent[bot]", "X-Twin-Role": ROLE_VALIDATOR_APP}

        code, pr, _ = _req(
            f"{base_url}/repos/maskservice/viewer/pulls",
            "POST",
            {"title": "Fix CC", "head": git_repo_pair.branch_name, "base": "main"},
            coder_h,
        )
        assert code == 201
        pr_num = pr["number"]
        head_sha = pr["head"]["sha"]
        assert head_sha == git_repo_pair.head_sha

        # Coding agent cannot self-approve or merge
        code, _, _ = _req(
            f"{base_url}/repos/maskservice/viewer/pulls/{pr_num}/reviews",
            "POST",
            {"event": "APPROVE", "commit_id": head_sha},
            coder_h,
        )
        assert code == 403

        # Validator cannot merge before onedev/local-verify PASS and APPROVED review
        code, _, _ = _req(
            f"{base_url}/repos/maskservice/viewer/pulls/{pr_num}/merge",
            "PUT",
            {"sha": head_sha},
            val_h,
        )
        assert code == 409

        # Post onedev/local-verify success
        code, _, _ = _req(
            f"{base_url}/repos/maskservice/viewer/statuses/{head_sha}",
            "POST",
            {"context": "onedev/local-verify", "state": "success", "description": "3 gates passed"},
            onedev_h,
        )
        assert code == 201

        # Still cannot merge without validator APPROVED review
        code, _, _ = _req(
            f"{base_url}/repos/maskservice/viewer/pulls/{pr_num}/merge",
            "PUT",
            {"sha": head_sha},
            val_h,
        )
        assert code == 403

        # Validator approves exact head SHA
        code, _, _ = _req(
            f"{base_url}/repos/maskservice/viewer/pulls/{pr_num}/reviews",
            "POST",
            {"event": "APPROVE", "commit_id": head_sha, "body": "Verified"},
            val_h,
        )
        assert code == 201

        # Wrong SHA rejected (head freeze)
        code, _, _ = _req(
            f"{base_url}/repos/maskservice/viewer/pulls/{pr_num}/merge",
            "PUT",
            {"sha": "0" * 40},
            val_h,
        )
        assert code == 409

        # Exact SHA merge succeeds for independent validator
        code, merged, _ = _req(
            f"{base_url}/repos/maskservice/viewer/pulls/{pr_num}/merge",
            "PUT",
            {"sha": head_sha},
            val_h,
        )
        assert code == 200
        assert merged["merged"] is True
    finally:
        httpd.shutdown()
        store.close()
