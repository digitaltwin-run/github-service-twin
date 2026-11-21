"""GitHub REST v3 and Twin control route handlers with strict actor boundaries."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

from .formatters import combine_status_states, format_issue, format_pr, format_repo
from .git_backend import (
    GitBackendError,
    compare_refs,
    detect_default_branch,
    list_branches,
    resolve_ref_sha,
    validate_git_repo,
)
from .models import (
    OUTBOX_PENDING,
    OUTBOX_RECONCILED,
    REQUIRED_CHECK_CONTEXT,
    ActorIdentity,
    TwinRepository,
)
from .quota import QuotaGuard
from .reconciler import UpstreamReconciler
from .store import TwinStore

Response = tuple[int, dict[str, Any] | list[Any], dict[str, str]]


class TwinService:
    """HTTP handler layer backed by TwinStore, local Git, and QuotaGuard."""

    def __init__(
        self,
        store: TwinStore,
        quota: QuotaGuard,
        *,
        reconciler: UpstreamReconciler | None = None,
        base_web_url: str = "http://twin.local",
    ) -> None:
        self.store = store
        self.quota = quota
        self.reconciler = reconciler
        self.base_web_url = base_web_url.rstrip("/")

    def base_headers(self) -> dict[str, str]:
        snap = self.quota.current()
        now = self.quota.now()
        return {
            "Cache-Control": "no-store",
            "X-GitHub-Twin-Mode": snap.mode(now),
            "X-RateLimit-Limit": str(snap.limit),
            "X-RateLimit-Remaining": str(snap.remaining if not snap.is_exhausted(now) else snap.limit),
            "X-RateLimit-Reset": str(snap.reset_at),
            "X-Upstream-RateLimit-Remaining": str(snap.remaining),
        }

    def dispatch(
        self,
        method: str,
        raw_path: str,
        query_string: str,
        body: dict[str, Any],
        actor: ActorIdentity,
    ) -> Response:
        path = raw_path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(query_string).items() if v}
        status, payload = self._route(method.upper(), path, query, body, actor)
        return status, payload, self.base_headers()

    def _route(
        self, method: str, path: str, query: dict[str, str], body: dict[str, Any], actor: ActorIdentity
    ) -> tuple[int, dict[str, Any] | list[Any]]:
        if path == "/health" and method == "GET":
            return self._handle_health()
        if path == "/rate_limit" and method == "GET":
            return self._handle_rate_limit()
        if path.startswith("/twin/"):
            return self._route_twin(method, path, query, body)
        if path.startswith("/orgs/"):
            return self._route_orgs(method, path)
        if path.startswith("/repos/"):
            return self._route_repos(method, path, query, body, actor)
        return 404, {"message": f"Not Found: {method} {path}"}

    def _handle_health(self) -> tuple[int, dict[str, Any]]:
        now = self.quota.now()
        snap = self.quota.current()
        return 200, {
            "status": "ok",
            "service": "github-service-twin",
            "mode": snap.mode(now),
            "repositories": len(self.store.list_repositories()),
            "outbox_pending": len(self.store.list_outbox(status=OUTBOX_PENDING)),
            "outbox_chain_valid": self.store.verify_outbox_chain(),
        }

    def _handle_rate_limit(self) -> tuple[int, dict[str, Any]]:
        snap = self.quota.current()
        now = self.quota.now()
        return 200, {
            "resources": {
                "core": {
                    "limit": snap.limit,
                    "remaining": snap.remaining,
                    "reset": snap.reset_at,
                    "used": max(0, snap.limit - snap.remaining),
                }
            },
            "twin_mode": snap.mode(now),
        }

    def _route_twin(
        self, method: str, path: str, query: dict[str, str], body: dict[str, Any]
    ) -> tuple[int, dict[str, Any] | list[Any]]:
        if path == "/twin/status" and method == "GET":
            return self._twin_status()
        if path == "/twin/facts" and method == "GET":
            return self._twin_facts()
        if path == "/twin/outbox" and method == "GET":
            return 200, self.store.list_outbox(status=query.get("status"))
        if path == "/twin/repos" and method == "POST":
            return self._twin_register_repo(body)
        if path == "/twin/quota" and method == "POST":
            snap = self.quota.set_quota(
                remaining=int(body.get("remaining", 0)),
                reset_at=int(body.get("reset_at", 0)),
                blocked=bool(body.get("blocked", False)),
                source=str(body.get("source", "api")),
            )
            return 200, snap.to_dict(self.quota.now())
        if path == "/twin/sync" and method == "POST":
            if self.reconciler is None:
                return 409, {"message": "Upstream reconciler is not configured on this server"}
            return 200, self.reconciler.sync(apply=bool(body.get("apply", False))).to_dict()
        return 404, {"message": f"Unknown twin endpoint: {method} {path}"}

    def _twin_status(self) -> tuple[int, dict[str, Any]]:
        return 200, {
            "quota": self.quota.current().to_dict(self.quota.now()),
            "repositories": [r.full_name for r in self.store.list_repositories()],
            "outbox": {
                "pending": len(self.store.list_outbox(status=OUTBOX_PENDING)),
                "reconciled": len(self.store.list_outbox(status=OUTBOX_RECONCILED)),
                "chain_valid": self.store.verify_outbox_chain(),
            },
        }

    def _twin_facts(self) -> tuple[int, dict[str, Any]]:
        now = self.quota.now()
        snap = self.quota.current()
        payload = {
            "mode": snap.mode(now),
            "upstream_remaining": snap.remaining,
            "upstream_reset_at": snap.reset_at,
            "repositories": [r.full_name for r in self.store.list_repositories()],
            "outbox_pending": len(self.store.list_outbox(status=OUTBOX_PENDING)),
            "outbox_chain_valid": self.store.verify_outbox_chain(),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return 200, {
            "schema": "subactor.twin-fact/v1",
            "uri": "twin://github-service-twin/continuity-state",
            "payload_sha256": hashlib.sha256(canonical).hexdigest(),
            "payload": payload,
        }

    def _twin_register_repo(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        full_name = str(body.get("full_name", "")).strip()
        local_path = str(body.get("local_path", "")).strip()
        if "/" not in full_name or not local_path:
            return 422, {"message": "full_name (owner/repo) and local_path are required"}
        try:
            resolved = validate_git_repo(Path(local_path))
        except GitBackendError as exc:
            return 422, {"message": str(exc)}
        repo = TwinRepository(
            full_name=full_name,
            local_path=str(resolved),
            default_branch=str(body.get("default_branch") or detect_default_branch(resolved)),
            upstream_remote=str(body.get("upstream_remote") or "origin"),
        )
        self.store.register_repository(repo)
        return 201, format_repo(self.base_web_url, repo)

    def _route_orgs(self, method: str, path: str) -> tuple[int, dict[str, Any] | list[Any]]:
        match = re.fullmatch(r"/orgs/([^/]+)/repos", path)
        if match and method == "GET":
            owner = unquote(match.group(1))
            return 200, [format_repo(self.base_web_url, r) for r in self.store.list_repositories(owner=owner)]
        return 404, {"message": f"Not Found: {method} {path}"}

    def _route_repo_git_reads(self, repo: TwinRepository, subpath: str) -> tuple[int, dict[str, Any] | list[Any]] | None:
        if subpath == "":
            return 200, format_repo(self.base_web_url, repo)
        if subpath == "/branches":
            return self._handle_branches(repo)
        if subpath.startswith("/git/ref/heads/"):
            return self._handle_git_ref(repo, unquote(subpath[len("/git/ref/heads/") :]))
        if subpath.startswith("/compare/"):
            return self._handle_compare(repo, unquote(subpath[len("/compare/") :]))
        return None

    def _route_repos(
        self, method: str, path: str, query: dict[str, str], body: dict[str, Any], actor: ActorIdentity
    ) -> tuple[int, dict[str, Any] | list[Any]]:
        match = re.match(r"^/repos/([^/]+)/([^/]+)(/.*)?$", path)
        if not match:
            return 404, {"message": f"Not Found: {path}"}
        full_name = f"{unquote(match.group(1))}/{unquote(match.group(2))}"
        subpath = match.group(3) or ""
        repo = self.store.get_repository(full_name)
        if repo is None:
            return 404, {"message": f"Repository not registered in twin: {full_name}"}
        if method == "GET":
            git_resp = self._route_repo_git_reads(repo, subpath)
            if git_resp is not None:
                return git_resp
        if subpath == "/issues" or subpath.startswith("/issues/"):
            return self._route_issues(method, repo, subpath, query, body, actor)
        if subpath == "/pulls" or subpath.startswith("/pulls/"):
            return self._route_pulls(method, repo, subpath, query, body, actor)
        if subpath.startswith(("/statuses/", "/commits/")):
            return self._route_statuses(method, repo, subpath, body, actor)
        return 404, {"message": f"Not Found: {method} /repos/{full_name}{subpath}"}

    def _handle_branches(self, repo: TwinRepository) -> tuple[int, dict[str, Any] | list[Any]]:
        try:
            branches = list_branches(Path(repo.local_path))
        except GitBackendError as exc:
            return 500, {"message": str(exc)}
        return 200, [{"name": b.name, "commit": {"sha": b.sha}} for b in branches]

    def _handle_git_ref(self, repo: TwinRepository, branch: str) -> tuple[int, dict[str, Any]]:
        sha = resolve_ref_sha(Path(repo.local_path), branch)
        if not sha:
            return 404, {"message": f"Branch not found: {branch}"}
        return 200, {"ref": f"refs/heads/{branch}", "object": {"sha": sha, "type": "commit"}}

    def _handle_compare(self, repo: TwinRepository, spec: str) -> tuple[int, dict[str, Any]]:
        if "..." not in spec:
            return 422, {"message": "Compare spec must use base...head"}
        base_ref, head_ref = spec.split("...", 1)
        try:
            cmp = compare_refs(Path(repo.local_path), base_ref, head_ref)
        except GitBackendError as exc:
            return 404, {"message": str(exc)}
        return 200, {
            "status": cmp.status,
            "ahead_by": cmp.ahead_by,
            "behind_by": cmp.behind_by,
            "total_commits": cmp.total_commits,
            "base_commit": {"sha": cmp.base_sha},
            "merge_base_commit": {"sha": cmp.base_sha},
        }

    def _route_issues(
        self, method: str, repo: TwinRepository, subpath: str, query: dict[str, str], body: dict[str, Any], actor: ActorIdentity
    ) -> tuple[int, dict[str, Any] | list[Any]]:
        if subpath == "/issues" and method == "GET":
            rows = self.store.list_issues(repo.full_name, state=query.get("state", "open"), label=query.get("labels"))
            return 200, [format_issue(self.base_web_url, r) for r in rows]
        if subpath == "/issues" and method == "POST":
            title = str(body.get("title", "")).strip()
            if not title:
                return 422, {"message": "title is required"}
            issue = self.store.create_issue(
                repo.full_name,
                title=title,
                body=str(body.get("body", "")),
                labels=[str(x) for x in (body.get("labels") or [])],
                author=actor.login,
            )
            return 201, format_issue(self.base_web_url, issue)
        match = re.fullmatch(r"/issues/(\d+)", subpath)
        if match and method == "GET":
            issue = self.store.get_issue(repo.full_name, int(match.group(1)))
            return (200, format_issue(self.base_web_url, issue)) if issue else (404, {"message": "Issue not found"})
        return 404, {"message": f"Not Found: {method} {subpath}"}

    def _route_pulls(
        self, method: str, repo: TwinRepository, subpath: str, query: dict[str, str], body: dict[str, Any], actor: ActorIdentity
    ) -> tuple[int, dict[str, Any] | list[Any]]:
        if subpath == "/pulls" and method == "GET":
            rows = self.store.list_pull_requests(repo.full_name, state=query.get("state", "open"), head_ref=query.get("head"))
            return 200, [format_pr(self.base_web_url, repo, r) for r in rows]
        if subpath == "/pulls" and method == "POST":
            return self._create_pull(repo, body, actor)
        match = re.fullmatch(r"/pulls/(\d+)(/reviews|/merge)?", subpath)
        if not match:
            return 404, {"message": f"Not Found: {method} {subpath}"}
        number, tail = int(match.group(1)), match.group(2) or ""
        if tail == "" and method == "GET":
            pr = self.store.get_pull_request(repo.full_name, number)
            return (200, format_pr(self.base_web_url, repo, pr)) if pr else (404, {"message": "PR not found"})
        if tail == "/reviews":
            return self._handle_reviews(method, repo, number, body, actor)
        if tail == "/merge" and method == "PUT":
            return self._handle_merge(repo, number, body, actor)
        return 404, {"message": f"Not Found: {method} {subpath}"}

    def _create_pull(self, repo: TwinRepository, body: dict[str, Any], actor: ActorIdentity) -> tuple[int, dict[str, Any]]:
        title = str(body.get("title", "")).strip()
        head_ref = str(body.get("head", "")).strip()
        base_ref = str(body.get("base", repo.default_branch)).strip()
        if not title or not head_ref:
            return 422, {"message": "title and head are required"}
        if ":" in head_ref:
            head_ref = head_ref.split(":", 1)[1]
        head_sha = resolve_ref_sha(Path(repo.local_path), head_ref)
        if not head_sha:
            return 422, {"message": f"Head branch does not exist in local Git: {head_ref}"}
        pr, created = self.store.create_or_get_pull_request(
            repo.full_name,
            title=title,
            body=str(body.get("body", "")),
            head_ref=head_ref,
            base_ref=base_ref,
            head_sha=head_sha,
            draft=bool(body.get("draft", False)),
            author=actor.login,
        )
        return (201 if created else 200), format_pr(self.base_web_url, repo, pr)

    def _handle_reviews(
        self, method: str, repo: TwinRepository, number: int, body: dict[str, Any], actor: ActorIdentity
    ) -> tuple[int, dict[str, Any] | list[Any]]:
        pr = self.store.get_pull_request(repo.full_name, number)
        if pr is None:
            return 404, {"message": "PR not found"}
        if method == "GET":
            return 200, self.store.list_reviews(repo.full_name, number)
        if method != "POST":
            return 405, {"message": "Method Not Allowed"}
        event = str(body.get("event", "COMMENT")).upper()
        state = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}.get(event, event)
        commit_id = str(body.get("commit_id") or pr["head_sha"])
        if state == "APPROVED":
            if not actor.is_validator():
                return 403, {"message": "Only independent validator_app role may submit APPROVED reviews"}
            if actor.login == pr["author"]:
                return 403, {"message": "Self-approval forbidden by actor boundary"}
            if commit_id != pr["head_sha"]:
                return 409, {"message": "Approval commit_id does not match frozen PR head_sha"}
        return 201, self.store.add_review(
            repo.full_name,
            pr_number=number,
            commit_id=commit_id,
            state=state,
            body=str(body.get("body", "")),
            author=actor.login,
            actor_role=actor.role,
        )

    def _handle_merge(
        self, repo: TwinRepository, number: int, body: dict[str, Any], actor: ActorIdentity
    ) -> tuple[int, dict[str, Any]]:
        pr = self.store.get_pull_request(repo.full_name, number)
        if pr is None:
            return 404, {"message": "PR not found"}
        if not actor.is_validator():
            return 403, {"message": "Only independent validator_app role may merge PRs"}
        if actor.login == pr["author"]:
            return 403, {"message": "PR author cannot merge own pull request"}
        expected_sha = str(body.get("sha") or "")
        if expected_sha and expected_sha != pr["head_sha"]:
            return 409, {"message": f"Head SHA mismatch: expected {expected_sha}, actual {pr['head_sha']}"}
        if not self._has_passing_check(repo.full_name, pr["head_sha"]):
            return 409, {"message": f"Required check '{REQUIRED_CHECK_CONTEXT}' is not SUCCESS on {pr['head_sha']}"}
        if not self._has_validator_approval(repo.full_name, number, pr["head_sha"]):
            return 403, {"message": f"Independent validator APPROVED review required on {pr['head_sha']}"}
        merged_pr = self.store.mark_pr_merged(repo.full_name, number, merged_by=actor.login)
        return 200, {
            "sha": merged_pr["head_sha"],
            "merged": True,
            "message": "Twin merge recorded; queued for independent Validator handoff on upstream GitHub",
        }

    def _has_passing_check(self, full_name: str, sha: str) -> bool:
        for row in self.store.list_commit_statuses(full_name, sha):
            if row["context"] == REQUIRED_CHECK_CONTEXT:
                return row["state"] == "success"
        return False

    def _has_validator_approval(self, full_name: str, pr_number: int, sha: str) -> bool:
        for row in reversed(self.store.list_reviews(full_name, pr_number)):
            if row["commit_id"] == sha and row["actor_role"] == "validator_app":
                return row["state"] == "APPROVED"
        return False

    def _route_statuses(
        self, method: str, repo: TwinRepository, subpath: str, body: dict[str, Any], actor: ActorIdentity
    ) -> tuple[int, dict[str, Any] | list[Any]]:
        post_match = re.fullmatch(r"/statuses/([0-9a-fA-F]{7,40})", subpath)
        if post_match and method == "POST":
            sha = post_match.group(1)
            state = str(body.get("state", "")).lower()
            context = str(body.get("context", "")).strip()
            if state not in {"pending", "success", "failure", "error"} or not context:
                return 422, {"message": "valid state and context are required"}
            return 201, self.store.add_commit_status(
                repo.full_name,
                sha=sha,
                context=context,
                state=state,
                description=str(body.get("description", ""))[:140],
                target_url=str(body.get("target_url", "")),
                actor=actor.login,
            )
        get_match = re.fullmatch(r"/commits/([0-9a-fA-F]{7,40})/(status|statuses)", subpath)
        if get_match and method == "GET":
            sha, kind = get_match.group(1), get_match.group(2)
            rows = self.store.list_commit_statuses(repo.full_name, sha)
            if kind == "statuses":
                return 200, rows
            latest_by_ctx: dict[str, dict[str, Any]] = {}
            for row in rows:
                latest_by_ctx.setdefault(row["context"], row)
            return 200, {
                "state": combine_status_states([r["state"] for r in latest_by_ctx.values()]),
                "sha": sha,
                "statuses": list(latest_by_ctx.values()),
            }
        return 404, {"message": f"Not Found: {method} {subpath}"}
