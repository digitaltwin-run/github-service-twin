"""Shared Git repo fixtures for github-service-twin tests."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class LocalGitFixture:
    bare_upstream: Path
    worktree: Path
    main_sha: str
    branch_name: str
    head_sha: str


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture()
def git_repo_pair(tmp_path: Path) -> LocalGitFixture:
    bare = tmp_path / "upstream.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "agent@twin.local")
    _git(work, "config", "user.name", "Twin Agent")
    (work / "README.md").write_text("# Sample\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "chore: init")
    main_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")

    branch = "ticket/007-offline-continuity"
    _git(work, "checkout", "-b", branch)
    (work / "module.py").write_text("ANSWER = 42\n", encoding="utf-8")
    _git(work, "add", "module.py")
    _git(work, "commit", "-m", "feat: offline delivery")
    head_sha = _git(work, "rev-parse", "HEAD")

    return LocalGitFixture(
        bare_upstream=bare,
        worktree=work,
        main_sha=main_sha,
        branch_name=branch,
        head_sha=head_sha,
    )
