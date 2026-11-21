"""CLI for GitHub Service Twin: register repos, serve, inspect outbox, sync, and demo."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .api_handlers import TwinService
from .git_backend import detect_default_branch, validate_git_repo
from .models import ROLE_CODING_AGENT, ROLE_ONEDEV_VERIFIER, ROLE_VALIDATOR_APP, TwinRepository
from .quota import QuotaGuard
from .reconciler import UpstreamGitHubClient, UpstreamReconciler, build_validator_script_handoff
from .server import TwinHTTPServer, start_background_server
from .store import TwinStore

DEFAULT_STATE_DIR = Path(".twin-state")
DEFAULT_VALIDATOR_SCRIPT = Path("/home/tom/github/subactor/validator-agent/bin/run-local-direct-pr.sh")


def _open_store(state_dir: Path) -> TwinStore:
    return TwinStore(state_dir / "github-twin.sqlite3")


def _http_json(url: str, method: str = "GET", body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    req_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=req_headers, method=method)
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cmd_register(args: argparse.Namespace) -> int:
    store = _open_store(Path(args.state_dir))
    try:
        resolved = validate_git_repo(Path(args.path))
        branch = args.default_branch or detect_default_branch(resolved)
        repo = store.register_repository(
            TwinRepository(
                full_name=args.repo,
                local_path=str(resolved),
                default_branch=branch,
                upstream_remote=args.remote,
            )
        )
        print(json.dumps({"registered": repo.full_name, "local_path": repo.local_path, "default_branch": repo.default_branch}, indent=2))
        return 0
    finally:
        store.close()


def _cmd_serve(args: argparse.Namespace) -> int:
    store = _open_store(Path(args.state_dir))
    budget_path = Path(args.budget_file) if args.budget_file else None
    quota = QuotaGuard(budget_file=budget_path)
    upstream = UpstreamGitHubClient(args.upstream_api_url) if args.upstream_api_url else None
    handoff = build_validator_script_handoff(Path(args.validator_script)) if args.validator_script else None
    reconciler = UpstreamReconciler(store, quota, upstream, validator_handoff=handoff)
    service = TwinService(store, quota, reconciler=reconciler)
    httpd = TwinHTTPServer((args.host, args.port), service)
    print(f"github-service-twin listening on http://{args.host}:{args.port} (state={args.state_dir})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        httpd.server_close()
        store.close()
    return 0


def _cmd_outbox(args: argparse.Namespace) -> int:
    store = _open_store(Path(args.state_dir))
    try:
        print(json.dumps({"chain_valid": store.verify_outbox_chain(), "items": store.list_outbox(status=args.status)}, indent=2))
        return 0
    finally:
        store.close()


def _cmd_sync(args: argparse.Namespace) -> int:
    store = _open_store(Path(args.state_dir))
    try:
        budget_path = Path(args.budget_file) if args.budget_file else None
        quota = QuotaGuard(budget_file=budget_path)
        upstream = UpstreamGitHubClient(args.upstream_api_url) if args.upstream_api_url else None
        script = Path(args.validator_script) if args.validator_script else DEFAULT_VALIDATOR_SCRIPT
        handoff = build_validator_script_handoff(script) if script.is_file() else None
        reconciler = UpstreamReconciler(store, quota, upstream, validator_handoff=handoff)
        report = reconciler.sync(apply=args.apply)
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    finally:
        store.close()


def _init_demo_repos(root: Path) -> tuple[Path, Path, str]:
    bare = root / "upstream.git"
    work = root / "demo-repo"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "demo@twin.local"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "Demo Agent"], check=True)
    (work / "README.md").write_text("# Demo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(bare)], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-u", "origin", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-b", "ticket/001-offline-work"], check=True, capture_output=True)
    (work / "feature.py").write_text("VALUE = 42\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "feature.py"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "feat: offline change"], check=True, capture_output=True)
    sha = subprocess.check_output(["git", "-C", str(work), "rev-parse", "HEAD"], text=True).strip()
    return bare, work, sha


def _cmd_demo(_args: argparse.Namespace) -> int:
    """Demonstrate quota-exhausted twin execution followed by upstream reconciliation."""
    with tempfile.TemporaryDirectory(prefix="gh-twin-demo-") as tmp:
        root = Path(tmp)
        bare, work, head_sha = _init_demo_repos(root)

        upstream_store = TwinStore(root / "upstream.sqlite3")
        upstream_store.register_repository(TwinRepository("demo/app", str(bare), default_branch="main"))
        upstream_quota = QuotaGuard()
        upstream_quota.set_quota(remaining=0, reset_at=int(time.time()) + 900, blocked=True, source="demo-upstream-exhausted")
        upstream_service = TwinService(upstream_store, upstream_quota)
        up_httpd, _, up_url = start_background_server(upstream_service)

        twin_store = TwinStore(root / "twin.sqlite3")
        twin_store.register_repository(TwinRepository("demo/app", str(work), default_branch="main"))
        twin_quota = QuotaGuard()
        twin_quota.set_quota(remaining=0, reset_at=int(time.time()) + 900, blocked=True, source="demo-rate-limit")

        def _validator_handoff(full_name: str, pr_num: int, sha: str, _repo: TwinRepository) -> tuple[bool, str]:
            val_headers = {"X-Twin-Actor": "ifuri-validator-agent[bot]", "X-Twin-Role": ROLE_VALIDATOR_APP}
            _http_json(f"{up_url}/repos/{full_name}/pulls/{pr_num}/reviews", "POST", {"event": "APPROVE", "commit_id": sha, "body": "Independent validation OK"}, val_headers)
            _http_json(f"{up_url}/repos/{full_name}/pulls/{pr_num}/merge", "PUT", {"sha": sha}, val_headers)
            return True, f"independent validator merged upstream PR #{pr_num}"

        reconciler = UpstreamReconciler(twin_store, twin_quota, UpstreamGitHubClient(up_url), validator_handoff=_validator_handoff)
        twin_service = TwinService(twin_store, twin_quota, reconciler=reconciler)
        twin_httpd, _, twin_url = start_background_server(twin_service)
        try:
            pr = _http_json(f"{twin_url}/repos/demo/app/pulls", "POST", {"title": "Offline task (ticket-001)", "head": "ticket/001-offline-work", "base": "main"}, {"X-Twin-Actor": "coding-agent", "X-Twin-Role": ROLE_CODING_AGENT})
            _http_json(f"{twin_url}/repos/demo/app/statuses/{head_sha}", "POST", {"context": "onedev/local-verify", "state": "success", "description": "Local OneDev checks passed"}, {"X-Twin-Actor": "onedev-executor", "X-Twin-Role": ROLE_ONEDEV_VERIFIER})
            val_headers = {"X-Twin-Actor": "ifuri-validator-agent[bot]", "X-Twin-Role": ROLE_VALIDATOR_APP}
            _http_json(f"{twin_url}/repos/demo/app/pulls/{pr['number']}/reviews", "POST", {"event": "APPROVE", "commit_id": head_sha, "body": "Twin validation passed"}, val_headers)
            _http_json(f"{twin_url}/repos/demo/app/pulls/{pr['number']}/merge", "PUT", {"sha": head_sha}, val_headers)
            deferred_sync = _http_json(f"{twin_url}/twin/sync", "POST", {"apply": True})

            _http_json(f"{up_url}/twin/quota", "POST", {"remaining": 4950, "reset_at": 0, "blocked": False, "source": "quota-restored"})
            _http_json(f"{twin_url}/twin/quota", "POST", {"remaining": 4950, "reset_at": 0, "blocked": False, "source": "quota-restored"})
            applied_sync = _http_json(f"{twin_url}/twin/sync", "POST", {"apply": True})
            upstream_pr = _http_json(f"{up_url}/repos/demo/app/pulls/1")
            print(json.dumps({
                "phase_1_offline_pr": {"number": pr["number"], "head_sha": head_sha[:12]},
                "phase_2_sync_while_blocked": deferred_sync,
                "phase_3_sync_after_unlock": applied_sync,
                "phase_4_upstream_readback": {"number": upstream_pr["number"], "merged": upstream_pr["merged"], "merged_by": upstream_pr["merged_by"]},
            }, indent=2))
            return 0
        finally:
            twin_httpd.shutdown()
            up_httpd.shutdown()
            twin_store.close()
            upstream_store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="github-service-twin", description="GitHub Digital Twin & Offline Continuity Engine")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    sub = parser.add_subparsers(dest="command", required=True)

    reg = sub.add_parser("register", help="Register a local Git repository in the twin")
    reg.add_argument("repo", help="Repository full name (owner/repo)")
    reg.add_argument("path", help="Local Git checkout path")
    reg.add_argument("--default-branch", default="")
    reg.add_argument("--remote", default="origin")
    reg.set_defaults(func=_cmd_register)

    srv = sub.add_parser("serve", help="Run the HTTP GitHub Digital Twin server")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8796)
    srv.add_argument("--budget-file", default="")
    srv.add_argument("--upstream-api-url", default="")
    srv.add_argument("--validator-script", default="")
    srv.set_defaults(func=_cmd_serve)

    out = sub.add_parser("outbox", help="Inspect durable hash-chained sync outbox")
    out.add_argument("--status", default=None)
    out.set_defaults(func=_cmd_outbox)

    syn = sub.add_parser("sync", help="Reconcile pending outbox operations with upstream GitHub")
    syn.add_argument("--upstream-api-url", default="")
    syn.add_argument("--budget-file", default="")
    syn.add_argument("--validator-script", default="")
    syn.add_argument("--apply", action="store_true")
    syn.set_defaults(func=_cmd_sync)

    demo = sub.add_parser("demo", help="Run full local offline-twin -> unlock -> upstream-sync demo")
    demo.set_defaults(func=_cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
