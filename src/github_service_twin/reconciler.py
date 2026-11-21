"""Upstream GitHub reconciler preserving the independent Validator actor boundary."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .git_backend import GitBackendError, push_branch_to_remote
from .models import (
    OUTBOX_CONFLICT,
    OUTBOX_PENDING,
    OUTBOX_RECONCILED,
    SyncOperationResult,
    SyncReport,
    TwinRepository,
)
from .quota import QuotaGuard
from .store import TwinStore

ValidatorHandoff = Callable[[str, int, str, TwinRepository], tuple[bool, str]]


class UpstreamError(RuntimeError):
    """Raised when an upstream GitHub API request fails."""


@dataclass
class UpstreamGitHubClient:
    """Minimal GitHub REST v3 transport for reconciling outbox items upstream."""

    api_url: str
    auth_header: str = ""
    timeout: int = 15

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[Any, dict[str, str]]:
        url = f"{self.api_url.rstrip('/')}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "github-service-twin/0.1.0",
        }
        if self.auth_header:
            headers["Authorization"] = self.auth_header
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return (json.loads(raw) if raw else {}), resp_headers
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise UpstreamError(f"{method} {path} -> HTTP {exc.code}: {detail[:240]}") from exc

    def rate_limit(self) -> tuple[int, int]:
        payload, headers = self._request("GET", "/rate_limit")
        core = ((payload.get("resources") or {}).get("core")) or {}
        remaining = core.get("remaining")
        reset_at = core.get("reset")
        if not isinstance(remaining, int):
            remaining = int(headers.get("x-ratelimit-remaining", "5000"))
        if not isinstance(reset_at, int):
            reset_at = int(headers.get("x-ratelimit-reset", "0"))
        return remaining, reset_at

    def create_issue(self, full_name: str, *, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        payload, _ = self._request(
            "POST",
            f"/repos/{quote(full_name, safe='/')}/issues",
            {"title": title, "body": body, "labels": labels},
        )
        return payload

    def find_open_pr(self, full_name: str, head_ref: str, base_ref: str) -> dict[str, Any] | None:
        owner = full_name.split("/", 1)[0]
        path = (
            f"/repos/{quote(full_name, safe='/')}/pulls"
            f"?state=open&head={quote(f'{owner}:{head_ref}', safe='')}&base={quote(base_ref, safe='')}"
        )
        rows, _ = self._request("GET", path)
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    def create_pr(
        self,
        full_name: str,
        *,
        title: str,
        body: str,
        head_ref: str,
        base_ref: str,
        draft: bool,
    ) -> dict[str, Any]:
        payload, _ = self._request(
            "POST",
            f"/repos/{quote(full_name, safe='/')}/pulls",
            {"title": title, "body": body, "head": head_ref, "base": base_ref, "draft": draft},
        )
        return payload

    def get_pr(self, full_name: str, number: int) -> dict[str, Any]:
        payload, _ = self._request("GET", f"/repos/{quote(full_name, safe='/')}/pulls/{number}")
        return payload

    def create_status(
        self,
        full_name: str,
        *,
        sha: str,
        context: str,
        state: str,
        description: str,
        target_url: str,
    ) -> dict[str, Any]:
        payload, _ = self._request(
            "POST",
            f"/repos/{quote(full_name, safe='/')}/statuses/{sha}",
            {
                "context": context,
                "state": state,
                "description": description[:140],
                "target_url": target_url,
            },
        )
        return payload


def build_validator_script_handoff(script_path: Path) -> ValidatorHandoff:
    """Delegate real upstream review+merge exclusively to the independent Validator script."""

    def _handoff(full_name: str, pr_number: int, head_sha: str, repo: TwinRepository) -> tuple[bool, str]:
        cmd = [
            str(script_path),
            "--repository",
            full_name,
            "--pull-request",
            str(pr_number),
            "--expected-head-sha",
            head_sha,
            "--target-root",
            repo.local_path,
            "--apply",
            "--merge",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            return True, proc.stdout.strip()[-240:] or "validator handoff succeeded"
        return False, (proc.stderr.strip() or proc.stdout.strip())[-240:]

    return _handoff


class UpstreamReconciler:
    """Replays twin outbox operations to real GitHub once API quota recovers."""

    def __init__(
        self,
        store: TwinStore,
        quota: QuotaGuard,
        upstream: UpstreamGitHubClient | None = None,
        *,
        validator_handoff: ValidatorHandoff | None = None,
        push_branches: bool = True,
    ) -> None:
        self.store = store
        self.quota = quota
        self.upstream = upstream
        self.validator_handoff = validator_handoff
        self.push_branches = push_branches

    def probe_upstream_quota(self) -> None:
        if self.upstream is None:
            return
        try:
            remaining, reset_at = self.upstream.rate_limit()
            self.quota.record_upstream_headers(remaining, reset_at)
        except UpstreamError:
            snap = self.quota.current()
            self.quota.set_quota(remaining=0, reset_at=snap.reset_at, blocked=True, source="upstream-unreachable")

    def sync(self, *, apply: bool = False) -> SyncReport:
        if self.upstream is not None and apply:
            self.probe_upstream_quota()
        now = self.quota.now()
        snap = self.quota.current()
        pending = self.store.list_outbox(status=OUTBOX_PENDING)
        report = SyncReport(mode=snap.mode(now), applied=apply)

        if snap.is_exhausted(now):
            report.deferred = len(pending)
            return report

        for op in pending:
            report.processed += 1
            if not apply or self.upstream is None:
                report.deferred += 1
                report.results.append(
                    SyncOperationResult(
                        op_id=op["id"],
                        kind=op["kind"],
                        repository=op["repository"],
                        status="PLANNED",
                        detail="dry-run: ready to reconcile once --apply is supplied",
                    )
                )
                continue
            item = self._reconcile_one(op)
            report.results.append(item)
            if item.status == OUTBOX_RECONCILED:
                report.reconciled += 1
            elif item.status == OUTBOX_CONFLICT:
                report.conflicts += 1
            else:
                report.deferred += 1
        return report

    def _reconcile_one(self, op: dict[str, Any]) -> SyncOperationResult:
        kind = op["kind"]
        repo = self.store.get_repository(op["repository"])
        if repo is None:
            return self._fail_op(op, OUTBOX_CONFLICT, f"repository not registered: {op['repository']}")
        try:
            if kind == "create_issue":
                return self._sync_issue(op)
            if kind == "ensure_pull_request":
                return self._sync_pull_request(op, repo)
            if kind == "commit_status":
                return self._sync_status(op)
            if kind == "validator_merge_handoff":
                return self._sync_validator_handoff(op, repo)
            return self._fail_op(op, OUTBOX_CONFLICT, f"unsupported outbox kind: {kind}")
        except (UpstreamError, GitBackendError) as exc:
            return self._fail_op(op, OUTBOX_PENDING, str(exc))

    def _sync_issue(self, op: dict[str, Any]) -> SyncOperationResult:
        assert self.upstream is not None
        payload = op["payload"]
        created = self.upstream.create_issue(
            op["repository"],
            title=str(payload["title"]),
            body=str(payload.get("body", "")),
            labels=list(payload.get("labels", [])),
        )
        ref = f"issue#{created.get('number')}"
        self.store.update_outbox_status(op["id"], status=OUTBOX_RECONCILED, upstream_ref=ref)
        return SyncOperationResult(op["id"], op["kind"], op["repository"], OUTBOX_RECONCILED, "synced", ref)

    def _sync_pull_request(self, op: dict[str, Any], repo: TwinRepository) -> SyncOperationResult:
        assert self.upstream is not None
        payload = op["payload"]
        head_ref = str(payload["head_ref"])
        base_ref = str(payload["base_ref"])
        if self.push_branches:
            push_branch_to_remote(Path(repo.local_path), repo.upstream_remote, head_ref)
        existing = self.upstream.find_open_pr(op["repository"], head_ref, base_ref)
        pr_obj = existing or self.upstream.create_pr(
            op["repository"],
            title=str(payload["title"]),
            body=str(payload.get("body", "")),
            head_ref=head_ref,
            base_ref=base_ref,
            draft=bool(payload.get("draft", False)),
        )
        upstream_num = int(pr_obj["number"])
        self.store.set_pr_upstream_number(op["repository"], int(payload["twin_number"]), upstream_num)
        ref = f"pr#{upstream_num}"
        self.store.update_outbox_status(op["id"], status=OUTBOX_RECONCILED, upstream_ref=ref)
        return SyncOperationResult(op["id"], op["kind"], op["repository"], OUTBOX_RECONCILED, "synced", ref)

    def _sync_status(self, op: dict[str, Any]) -> SyncOperationResult:
        assert self.upstream is not None
        payload = op["payload"]
        self.upstream.create_status(
            op["repository"],
            sha=str(payload["sha"]),
            context=str(payload["context"]),
            state=str(payload["state"]),
            description=str(payload.get("description", "")),
            target_url=str(payload.get("target_url", "")),
        )
        ref = f"status:{payload['context']}@{str(payload['sha'])[:12]}"
        self.store.update_outbox_status(op["id"], status=OUTBOX_RECONCILED, upstream_ref=ref)
        return SyncOperationResult(op["id"], op["kind"], op["repository"], OUTBOX_RECONCILED, "synced", ref)

    def _sync_validator_handoff(self, op: dict[str, Any], repo: TwinRepository) -> SyncOperationResult:
        assert self.upstream is not None
        payload = op["payload"]
        twin_pr = self.store.get_pull_request(op["repository"], int(payload["twin_number"]))
        upstream_num = twin_pr["upstream_number"] if twin_pr else None
        if not upstream_num:
            return self._fail_op(op, OUTBOX_PENDING, "waiting for upstream PR mapping before validator handoff")
        if self.validator_handoff is None:
            return self._fail_op(
                op,
                OUTBOX_PENDING,
                "independent validator handoff adapter not configured; personal merge forbidden",
            )
        head_sha = str(payload["head_sha"])
        ok, detail = self.validator_handoff(op["repository"], int(upstream_num), head_sha, repo)
        if not ok:
            return self._fail_op(op, OUTBOX_PENDING, f"validator handoff pending: {detail}")
        readback = self.upstream.get_pr(op["repository"], int(upstream_num))
        if not readback.get("merged"):
            return self._fail_op(op, OUTBOX_PENDING, "upstream readback shows PR not yet merged")
        ref = f"merged:pr#{upstream_num}@{head_sha[:12]}"
        self.store.update_outbox_status(op["id"], status=OUTBOX_RECONCILED, upstream_ref=ref)
        return SyncOperationResult(op["id"], op["kind"], op["repository"], OUTBOX_RECONCILED, detail, ref)

    def _fail_op(self, op: dict[str, Any], status: str, error: str) -> SyncOperationResult:
        self.store.update_outbox_status(op["id"], status=status, last_error=error)
        return SyncOperationResult(op["id"], op["kind"], op["repository"], status, error)
