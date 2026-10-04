"""Immutable policy versions.

A version is a frozen snapshot of a draft's rule list at publish time. Versions
are never edited or deleted: rollback points scope back at an older version, it
does not recreate or remove anything.
"""

from __future__ import annotations

from dataclasses import dataclass

from maskhub.errors import Conflict, NotFound
from maskhub.store.kvstore import RecordStore
from maskhub.timeutil import utc_now

PREFIX = "policy.version."
INITIAL_VERSION = 1


def version_key(policy_identifier: str, version: int) -> str:
    return f"{PREFIX}{policy_identifier}.v{version}"


def parse_version_key(key: str) -> tuple[str, int] | None:
    if not key.startswith(PREFIX):
        return None
    body = key[len(PREFIX):]
    if ".v" not in body:
        return None
    identifier, raw_version = body.rsplit(".v", 1)
    if not identifier or not raw_version.isdigit():
        return None
    return identifier, int(raw_version)


@dataclass(frozen=True)
class PolicyVersion:
    policy_identifier: str
    version: int
    rules: tuple[str, ...]
    created_at: str
    release_id: str
    note: str = ""

    def __post_init__(self) -> None:
        if not self.policy_identifier.strip():
            raise ValueError("policy identifier is required")
        if self.version < INITIAL_VERSION:
            raise ValueError("version number must be >= 1")
        if not self.rules:
            raise ValueError("policy version must reference at least one rule")
        if not self.created_at:
            raise ValueError("created_at is required")
        if not self.release_id:
            raise ValueError("release_id is required")

    @property
    def ref(self) -> str:
        return f"{self.policy_identifier}@v{self.version}"

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_identifier": self.policy_identifier,
            "version": self.version,
            "rules": list(self.rules),
            "created_at": self.created_at,
            "release_id": self.release_id,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "PolicyVersion | None":
        if not isinstance(payload, dict):
            return None
        identifier = payload.get("policy_identifier")
        version = payload.get("version")
        rules = payload.get("rules")
        created_at = payload.get("created_at")
        release_id = payload.get("release_id")
        note = payload.get("note", "")
        if not isinstance(identifier, str) or not isinstance(version, int):
            return None
        if not isinstance(rules, list):
            return None
        if not isinstance(created_at, str) or not isinstance(release_id, str):
            return None
        if not isinstance(note, str):
            return None
        try:
            return cls(
                policy_identifier=identifier,
                version=version,
                rules=tuple(str(item) for item in rules),
                created_at=created_at,
                release_id=release_id,
                note=note,
            )
        except ValueError:
            return None


class VersionRegistry:
    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def put(self, version: PolicyVersion) -> PolicyVersion:
        if self._store.has(version_key(version.policy_identifier, version.version)):
            raise Conflict(
                f"policy version {version.ref} already exists and is immutable"
            )
        self._store.put_json(
            version_key(version.policy_identifier, version.version),
            version.as_dict(),
        )
        return version

    def get(self, policy_identifier: str, version: int) -> PolicyVersion:
        found = self.find(policy_identifier, version)
        if found is None:
            raise NotFound(f"unknown policy version {policy_identifier}@v{version}")
        return found

    def find(self, policy_identifier: str, version: int) -> PolicyVersion | None:
        return PolicyVersion.from_dict(
            self._store.get_json(version_key(policy_identifier, version))
        )

    def versions(self, policy_identifier: str) -> list[PolicyVersion]:
        from maskhub.store.query import keys_with_prefix

        result = []
        for key in keys_with_prefix(self._store, PREFIX):
            parsed = parse_version_key(key)
            if parsed is not None and parsed[0] == policy_identifier:
                result.append(self.get(parsed[0], parsed[1]))
        result.sort(key=lambda item: item.version)
        return result

    def latest(self, policy_identifier: str) -> PolicyVersion | None:
        versions = self.versions(policy_identifier)
        return versions[-1] if versions else None

    def next_number(self, policy_identifier: str) -> int:
        latest = self.latest(policy_identifier)
        return INITIAL_VERSION if latest is None else latest.version + 1

    def all(self) -> list[PolicyVersion]:
        from maskhub.store.query import keys_with_prefix

        refs = []
        for key in keys_with_prefix(self._store, PREFIX):
            parsed = parse_version_key(key)
            if parsed is not None:
                refs.append(parsed)
        result = [self.get(identifier, number) for identifier, number in refs]
        result.sort(key=lambda item: (item.policy_identifier, item.version))
        return result


def snapshot(
    policy_identifier: str,
    rules: tuple[str, ...],
    release_id: str,
    version: int,
    note: str = "",
    created_at: str | None = None,
) -> PolicyVersion:
    return PolicyVersion(
        policy_identifier=policy_identifier,
        version=version,
        rules=rules,
        created_at=created_at or utc_now(),
        release_id=release_id,
        note=note,
    )
