"""Release records: one immutable fact per publish or rollback.

A release describes what happened to the serving pointers at a point in time.
Publishes mint a new version; rollbacks point scope back at an existing one and
are recorded as releases of kind ``rollback`` without minting anything.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

from maskhub.errors import Conflict, NotFound
from maskhub.store.kvstore import RecordStore
from maskhub.timeutil import utc_now

PREFIX = "policy.release."
# Note: deliberately outside PREFIX so release listing never picks it up.
SEQUENCE_KEY = "policy.release_seq"


class ReleaseKind(str, Enum):
    PUBLISH = "publish"
    ROLLBACK = "rollback"


@dataclass(frozen=True)
class BindingChange:
    """One scope pointer before/after pair inside a release."""

    dataset: str
    policy_identifier: str
    from_version: int | None
    to_version: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: object) -> "BindingChange | None":
        if not isinstance(payload, dict):
            return None
        dataset = payload.get("dataset")
        identifier = payload.get("policy_identifier")
        from_version = payload.get("from_version")
        to_version = payload.get("to_version")
        if not isinstance(dataset, str) or not isinstance(identifier, str):
            return None
        if not isinstance(to_version, int):
            return None
        if from_version is not None and not isinstance(from_version, int):
            return None
        return cls(
            dataset=dataset,
            policy_identifier=identifier,
            from_version=from_version,
            to_version=to_version,
        )


@dataclass(frozen=True)
class Release:
    identifier: str
    kind: str
    policy_identifier: str
    created_at: str
    changes: tuple[BindingChange, ...]
    new_version: int | None = None
    target_version: int | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValueError("release identifier is required")
        if self.kind not in {item.value for item in ReleaseKind}:
            raise ValueError(f"unknown release kind {self.kind!r}")
        if not self.policy_identifier.strip():
            raise ValueError("policy identifier is required")
        if not self.changes:
            raise ValueError("release must affect at least one dataset")
        if self.kind == ReleaseKind.PUBLISH.value and self.new_version is None:
            raise ValueError("publish release must record a new_version")
        if self.kind == ReleaseKind.ROLLBACK.value and self.target_version is None:
            raise ValueError("rollback release must record a target_version")

    def as_dict(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "kind": self.kind,
            "policy_identifier": self.policy_identifier,
            "created_at": self.created_at,
            "changes": [change.as_dict() for change in self.changes],
            "new_version": self.new_version,
            "target_version": self.target_version,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "Release | None":
        if not isinstance(payload, dict):
            return None
        identifier = payload.get("identifier")
        kind = payload.get("kind")
        policy_identifier = payload.get("policy_identifier")
        created_at = payload.get("created_at")
        raw_changes = payload.get("changes")
        new_version = payload.get("new_version")
        target_version = payload.get("target_version")
        note = payload.get("note", "")
        if not all(isinstance(item, str) for item in (identifier, kind, policy_identifier, created_at)):
            return None
        if not isinstance(raw_changes, list):
            return None
        changes = tuple(
            item
            for item in (BindingChange.from_dict(raw) for raw in raw_changes)
            if item is not None
        )
        if len(changes) != len(raw_changes):
            return None
        if new_version is not None and not isinstance(new_version, int):
            return None
        if target_version is not None and not isinstance(target_version, int):
            return None
        if not isinstance(note, str):
            return None
        try:
            return cls(
                identifier=identifier,
                kind=kind,
                policy_identifier=policy_identifier,
                created_at=created_at,
                changes=changes,
                new_version=new_version,
                target_version=target_version,
                note=note,
            )
        except ValueError:
            return None


def release_key(identifier: str) -> str:
    return PREFIX + identifier


class ReleaseRegistry:
    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def add(self, release: Release) -> Release:
        if self._store.has(release_key(release.identifier)):
            raise Conflict(f"release {release.identifier} already exists")
        self._store.put_json(release_key(release.identifier), release.as_dict())
        return release

    def get(self, identifier: str) -> Release:
        release = Release.from_dict(self._store.get_json(release_key(identifier)))
        if release is None:
            raise NotFound(f"unknown release {identifier}")
        return release

    def identifiers(self) -> list[str]:
        from maskhub.store.query import keys_with_prefix

        return [key[len(PREFIX):] for key in keys_with_prefix(self._store, PREFIX)]

    def all(self) -> list[Release]:
        releases = [self.get(identifier) for identifier in self.identifiers()]
        releases.sort(key=lambda item: (item.created_at, item.identifier))
        return releases

    def for_policy(self, policy_identifier: str) -> list[Release]:
        return [
            release
            for release in self.all()
            if release.policy_identifier == policy_identifier
        ]


def new_release_id(store: RecordStore) -> str:
    """Monotonic, time-sortable release identifier unique within the store."""
    stamp_part = utc_now().strftime("%Y%m%dT%H%M%S")
    with store.transaction():
        sequence = store.get_json(SEQUENCE_KEY)
        next_value = 1 if not isinstance(sequence, int) else sequence + 1
        store.put_many_json({SEQUENCE_KEY: next_value})
    return f"rel-{stamp_part}Z-{next_value:06d}"
