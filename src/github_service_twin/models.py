"""Typed domain models and actor-role constants for GitHub Service Twin."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ROLE_CODING_AGENT = "coding_agent"
ROLE_ONEDEV_VERIFIER = "onedev_verifier"
ROLE_VALIDATOR_APP = "validator_app"
VALID_ROLES = frozenset({ROLE_CODING_AGENT, ROLE_ONEDEV_VERIFIER, ROLE_VALIDATOR_APP})

REQUIRED_CHECK_CONTEXT = "onedev/local-verify"
OUTBOX_PENDING = "PENDING"
OUTBOX_RECONCILED = "RECONCILED"
OUTBOX_CONFLICT = "CONFLICT"
OUTBOX_SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class ActorIdentity:
    """Opaque caller identity; never stores raw credentials."""

    login: str
    role: str

    def is_validator(self) -> bool:
        return self.role == ROLE_VALIDATOR_APP

    def is_verifier(self) -> bool:
        return self.role in {ROLE_ONEDEV_VERIFIER, ROLE_VALIDATOR_APP}


@dataclass(frozen=True)
class TwinRepository:
    full_name: str
    local_path: str
    default_branch: str = "main"
    upstream_remote: str = "origin"
    private: bool = True
    archived: bool = False


@dataclass(frozen=True)
class QuotaSnapshot:
    remaining: int
    limit: int
    reset_at: int
    blocked: bool
    source: str = "twin-local"
    reserve: int = 50

    def is_exhausted(self, now: int) -> bool:
        if self.blocked:
            return True
        return bool(self.remaining <= self.reserve and (self.reset_at == 0 or now < self.reset_at))

    def mode(self, now: int) -> str:
        return "quota-deferred" if self.is_exhausted(now) else "live-ready"

    def to_dict(self, now: int) -> dict[str, Any]:
        return {
            "remaining": self.remaining,
            "limit": self.limit,
            "reset_at": self.reset_at,
            "blocked": self.blocked,
            "reserve": self.reserve,
            "source": self.source,
            "mode": self.mode(now),
        }


@dataclass(frozen=True)
class SyncOperationResult:
    op_id: int
    kind: str
    repository: str
    status: str
    detail: str
    upstream_ref: str = ""


@dataclass
class SyncReport:
    mode: str
    applied: bool
    processed: int = 0
    reconciled: int = 0
    conflicts: int = 0
    deferred: int = 0
    results: list[SyncOperationResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "applied": self.applied,
            "processed": self.processed,
            "reconciled": self.reconciled,
            "conflicts": self.conflicts,
            "deferred": self.deferred,
            "results": [
                {
                    "op_id": item.op_id,
                    "kind": item.kind,
                    "repository": item.repository,
                    "status": item.status,
                    "detail": item.detail,
                    "upstream_ref": item.upstream_ref,
                }
                for item in self.results
            ],
        }
