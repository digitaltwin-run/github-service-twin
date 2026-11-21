"""Read-only local Git inspection and explicit upstream push helpers."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitBackendError(RuntimeError):
    """Raised when a local Git repository cannot answer an inspection query."""


@dataclass(frozen=True)
class GitBranchRef:
    name: str
    sha: str


@dataclass(frozen=True)
class GitCompareResult:
    status: str
    ahead_by: int
    behind_by: int
    total_commits: int
    base_sha: str
    head_sha: str


def _run_git(repo_path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip() or f"git exited {proc.returncode}"
        raise GitBackendError(msg)
    return proc.stdout.strip()


def validate_git_repo(repo_path: Path) -> Path:
    resolved = repo_path.expanduser().resolve()
    if not resolved.exists():
        raise GitBackendError(f"Repository path does not exist: {resolved}")
    inside = _run_git(resolved, "rev-parse", "--is-inside-work-tree")
    if inside not in {"true", "false"}:
        raise GitBackendError(f"Path is not a Git repository: {resolved}")
    return resolved


def detect_default_branch(repo_path: Path) -> str:
    for candidate in ("main", "master"):
        if resolve_ref_sha(repo_path, candidate) is not None:
            return candidate
    try:
        head = _run_git(repo_path, "symbolic-ref", "--short", "HEAD")
        return head or "main"
    except GitBackendError:
        return "main"


def list_branches(repo_path: Path) -> list[GitBranchRef]:
    raw = _run_git(repo_path, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
    branches: list[GitBranchRef] = []
    for line in raw.splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            branches.append(GitBranchRef(name=parts[0], sha=parts[1]))
    return branches


def resolve_ref_sha(repo_path: Path, ref: str) -> str | None:
    if not ref:
        return None
    for candidate in (f"refs/heads/{ref}", ref):
        try:
            return _run_git(repo_path, "rev-parse", "--verify", f"{candidate}^{{commit}}")
        except GitBackendError:
            continue
    return None


def compare_refs(repo_path: Path, base: str, head: str) -> GitCompareResult:
    base_sha = resolve_ref_sha(repo_path, base)
    head_sha = resolve_ref_sha(repo_path, head)
    if not base_sha or not head_sha:
        raise GitBackendError(f"Cannot resolve compare refs {base!r}...{head!r}")
    counts = _run_git(repo_path, "rev-list", "--left-right", "--count", f"{base_sha}...{head_sha}")
    behind_str, ahead_str = counts.split()
    behind_by = int(behind_str)
    ahead_by = int(ahead_str)
    status = _compare_status(ahead_by, behind_by)
    return GitCompareResult(
        status=status,
        ahead_by=ahead_by,
        behind_by=behind_by,
        total_commits=ahead_by,
        base_sha=base_sha,
        head_sha=head_sha,
    )


def _compare_status(ahead_by: int, behind_by: int) -> str:
    if ahead_by == 0 and behind_by == 0:
        return "identical"
    if ahead_by > 0 and behind_by == 0:
        return "ahead"
    if ahead_by == 0 and behind_by > 0:
        return "behind"
    return "diverged"


def push_branch_to_remote(repo_path: Path, remote: str, branch: str) -> str:
    """Explicitly push a local branch to upstream remote during reconciliation."""
    _run_git(repo_path, "push", remote, f"refs/heads/{branch}:refs/heads/{branch}")
    sha = resolve_ref_sha(repo_path, branch)
    if not sha:
        raise GitBackendError(f"Branch {branch!r} missing after push")
    return sha
