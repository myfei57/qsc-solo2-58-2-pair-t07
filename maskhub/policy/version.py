"""Immutable policy versions, serving assignments, and batch attribution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

CAUSE_PUBLISH = "publish"
CAUSE_ROLLBACK = "rollback"
CAUSE_RETIRE = "retire"
CAUSES = (CAUSE_PUBLISH, CAUSE_ROLLBACK, CAUSE_RETIRE)


def utcnow() -> str:
    # Microsecond precision: several pointer moves can land in the same second
    # (publish + rollback), and the history must keep them distinguishable.
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PolicyVersion:
    """An immutable snapshot of a policy, created by publishing a draft."""

    policy: str
    version: int
    datasets: tuple[str, ...]
    rules: tuple[str, ...]
    published_at: str
    note: str = ""

    def __post_init__(self) -> None:
        if not self.policy.strip():
            raise ValueError("policy identifier is required")
        if self.version < 1:
            raise ValueError("version must be a positive integer")
        if not self.datasets:
            raise ValueError("version must target at least one dataset")
        if not self.rules:
            raise ValueError("version must reference at least one rule")

    @property
    def ref(self) -> str:
        return f"{self.policy}@{self.version}"

    def as_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy,
            "version": self.version,
            "ref": self.ref,
            "datasets": list(self.datasets),
            "rules": list(self.rules),
            "published_at": self.published_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "PolicyVersion | None":
        if not isinstance(payload, dict):
            return None
        policy = payload.get("policy")
        version = payload.get("version")
        datasets = payload.get("datasets")
        rules = payload.get("rules")
        published_at = payload.get("published_at")
        note = payload.get("note", "")
        if not isinstance(policy, str) or not isinstance(version, int):
            return None
        if not isinstance(datasets, list) or not isinstance(rules, list):
            return None
        if not isinstance(published_at, str) or not isinstance(note, str):
            return None
        try:
            return cls(
                policy=policy,
                version=version,
                datasets=tuple(str(item) for item in datasets),
                rules=tuple(str(item) for item in rules),
                published_at=published_at,
                note=note,
            )
        except ValueError:
            return None


@dataclass(frozen=True)
class Assignment:
    """The single policy version currently serving one dataset."""

    dataset: str
    policy: str
    version: int
    since: str

    @property
    def ref(self) -> str:
        return f"{self.policy}@{self.version}"

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "policy": self.policy,
            "version": self.version,
            "ref": self.ref,
            "since": self.since,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "Assignment | None":
        if not isinstance(payload, dict):
            return None
        dataset = payload.get("dataset")
        policy = payload.get("policy")
        version = payload.get("version")
        since = payload.get("since")
        if not isinstance(dataset, str) or not isinstance(policy, str):
            return None
        if not isinstance(version, int) or not isinstance(since, str):
            return None
        return cls(dataset=dataset, policy=policy, version=version, since=since)


@dataclass(frozen=True)
class AssignmentEvent:
    """One entry in the append-only history of assignment pointer moves."""

    at: str
    dataset: str
    policy: str
    cause: str
    from_version: int | None
    to_version: int | None  # None means the dataset left service

    def as_dict(self) -> dict[str, object]:
        return {
            "at": self.at,
            "dataset": self.dataset,
            "policy": self.policy,
            "cause": self.cause,
            "from_version": self.from_version,
            "to_version": self.to_version,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "AssignmentEvent | None":
        if not isinstance(payload, dict):
            return None
        at = payload.get("at")
        dataset = payload.get("dataset")
        policy = payload.get("policy")
        cause = payload.get("cause")
        from_version = payload.get("from_version")
        to_version = payload.get("to_version")
        if not all(isinstance(item, str) for item in (at, dataset, policy, cause)):
            return None
        if cause not in CAUSES:
            return None
        if not _optional_int(from_version) or not _optional_int(to_version):
            return None
        return cls(
            at=at,
            dataset=dataset,
            policy=policy,
            cause=cause,
            from_version=from_version,
            to_version=to_version,
        )


@dataclass(frozen=True)
class BatchRecord:
    """Immutable attribution of a processed data batch to a policy version.

    Created when a batch is processed and never rewritten afterwards, so a
    rollback can never change which version a finished batch answers to.
    """

    batch: str
    dataset: str
    policy: str
    version: int
    recorded_at: str

    @property
    def ref(self) -> str:
        return f"{self.policy}@{self.version}"

    def as_dict(self) -> dict[str, object]:
        return {
            "batch": self.batch,
            "dataset": self.dataset,
            "policy": self.policy,
            "version": self.version,
            "ref": self.ref,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "BatchRecord | None":
        if not isinstance(payload, dict):
            return None
        batch = payload.get("batch")
        dataset = payload.get("dataset")
        policy = payload.get("policy")
        version = payload.get("version")
        recorded_at = payload.get("recorded_at")
        if not all(isinstance(item, str) for item in (batch, dataset, policy, recorded_at)):
            return None
        if not isinstance(version, int):
            return None
        return cls(
            batch=batch,
            dataset=dataset,
            policy=policy,
            version=version,
            recorded_at=recorded_at,
        )


def _optional_int(value: object) -> bool:
    return value is None or isinstance(value, int)
