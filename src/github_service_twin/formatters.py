"""GitHub REST v3 JSON payload formatters for GitHub Service Twin."""

from __future__ import annotations

from typing import Any

from .models import TwinRepository


def format_repo(base_web_url: str, repo: TwinRepository) -> dict[str, Any]:
    return {
        "full_name": repo.full_name,
        "name": repo.full_name.split("/", 1)[1],
        "default_branch": repo.default_branch,
        "clone_url": repo.local_path,
        "private": repo.private,
        "archived": repo.archived,
        "html_url": f"{base_web_url}/{repo.full_name}",
    }


def format_issue(base_web_url: str, issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "state": issue["state"],
        "labels": [{"name": name} for name in issue["labels"]],
        "user": {"login": issue["author"]},
        "html_url": f"{base_web_url}/{issue['repository']}/issues/{issue['number']}",
        "upstream_number": issue["upstream_number"],
    }


def format_pr(base_web_url: str, repo: TwinRepository, pr: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": pr["number"],
        "title": pr["title"],
        "body": pr["body"],
        "state": pr["state"],
        "draft": pr["draft"],
        "merged": pr["merged"],
        "merged_at": pr["updated_at"] if pr["merged"] else None,
        "merged_by": {"login": pr["merged_by"]} if pr["merged_by"] else None,
        "user": {"login": pr["author"]},
        "head": {
            "ref": pr["head_ref"],
            "sha": pr["head_sha"],
            "repo": {"full_name": repo.full_name},
        },
        "base": {
            "ref": pr["base_ref"],
            "repo": {"full_name": repo.full_name},
        },
        "html_url": f"{base_web_url}/{repo.full_name}/pull/{pr['number']}",
        "upstream_number": pr["upstream_number"],
    }


def combine_status_states(states: list[str]) -> str:
    if not states:
        return "pending"
    if any(s in {"failure", "error"} for s in states):
        return "failure"
    if any(s == "pending" for s in states):
        return "pending"
    return "success"
