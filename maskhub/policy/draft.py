"""Editable policy draft.

A draft is the only mutable piece of a policy. Publishing a draft snapshots it
into an immutable :class:`~maskhub.policy.version.PolicyVersion`; the draft
itself never serves traffic directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from maskhub.errors import NotFound
from maskhub.store.kvstore import RecordStore

PREFIX = "policy.draft."


def draft_key(identifier: str) -> str:
    return PREFIX + identifier


@dataclass(frozen=True)
class PolicyDraft:
    identifier: str
    rules: tuple[str, ...]
    proposed_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValueError("policy identifier is required")
        if not self.rules:
            raise ValueError("policy draft must reference at least one rule")

    def as_dict(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "rules": list(self.rules),
            "proposed_scope": list(self.proposed_scope),
        }

    @classmethod
    def from_dict(cls, payload: object) -> "PolicyDraft | None":
        if not isinstance(payload, dict):
            return None
        identifier = payload.get("identifier")
        rules = payload.get("rules")
        scope = payload.get("proposed_scope", [])
        if not isinstance(identifier, str):
            return None
        if not isinstance(rules, list) or not isinstance(scope, list):
            return None
        try:
            return cls(
                identifier=identifier,
                rules=tuple(str(item) for item in rules),
                proposed_scope=tuple(str(item) for item in scope),
            )
        except ValueError:
            return None


class DraftRegistry:
    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def save(self, draft: PolicyDraft) -> PolicyDraft:
        self._store.put_json(draft_key(draft.identifier), draft.as_dict())
        return draft

    def find(self, identifier: str) -> PolicyDraft | None:
        return PolicyDraft.from_dict(self._store.get_json(draft_key(identifier)))

    def get(self, identifier: str) -> PolicyDraft:
        draft = self.find(identifier)
        if draft is None:
            raise NotFound(f"unknown policy {identifier}")
        return draft

    def identifiers(self) -> list[str]:
        from maskhub.store.query import keys_with_prefix

        return [key[len(PREFIX):] for key in keys_with_prefix(self._store, PREFIX)]

    def all(self) -> list[PolicyDraft]:
        return [self.get(identifier) for identifier in self.identifiers()]
