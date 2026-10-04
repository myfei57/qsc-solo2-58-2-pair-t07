"""Serving scope: one immutable binding pointer per dataset.

Each dataset has exactly one *current* binding (the policy/version that serves
it right now). Every pointer change appends an immutable history entry, which is
what makes "which version governed this dataset during that window" answerable
after the fact.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from maskhub.store.kvstore import RecordStore
from maskhub.timeutil import utc_now

if TYPE_CHECKING:
    from maskhub.policy.release import BindingChange

CURRENT_KEY = "policy.bindings"
HISTORY_KEY = "policy.binding.history"


@dataclass(frozen=True)
class Binding:
    dataset: str
    policy_identifier: str
    version: int
    release_id: str
    updated_at: str

    @property
    def ref(self) -> str:
        return f"{self.policy_identifier}@v{self.version}"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: object) -> "Binding | None":
        if not isinstance(payload, dict):
            return None
        dataset = payload.get("dataset")
        identifier = payload.get("policy_identifier")
        version = payload.get("version")
        release_id = payload.get("release_id")
        updated_at = payload.get("updated_at")
        if not all(
            isinstance(item, str)
            for item in (dataset, identifier, release_id, updated_at)
        ):
            return None
        if not isinstance(version, int):
            return None
        return cls(
            dataset=dataset,
            policy_identifier=identifier,
            version=version,
            release_id=release_id,
            updated_at=updated_at,
        )


@dataclass(frozen=True)
class BindingHistoryEntry:
    dataset: str
    policy_identifier: str
    version: int
    release_id: str
    kind: str
    at: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: object) -> "BindingHistoryEntry | None":
        if not isinstance(payload, dict):
            return None
        dataset = payload.get("dataset")
        identifier = payload.get("policy_identifier")
        version = payload.get("version")
        release_id = payload.get("release_id")
        kind = payload.get("kind")
        at = payload.get("at")
        if not all(
            isinstance(item, str)
            for item in (dataset, identifier, release_id, kind, at)
        ):
            return None
        if not isinstance(version, int):
            return None
        return cls(
            dataset=dataset,
            policy_identifier=identifier,
            version=version,
            release_id=release_id,
            kind=kind,
            at=at,
        )


def _bindings_from_document(document: object) -> dict[str, Binding]:
    if not isinstance(document, dict):
        return {}
    return {
        item.dataset: item
        for item in (Binding.from_dict(value) for value in document.values())
        if item is not None
    }


def _history_from_document(document: object) -> list[BindingHistoryEntry]:
    if not isinstance(document, list):
        return []
    return [
        item
        for item in (BindingHistoryEntry.from_dict(raw) for raw in document)
        if item is not None
    ]


class BindingLedger:
    """Read helpers plus one atomic apply used by the policy registry."""

    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def current(self) -> dict[str, Binding]:
        return _bindings_from_document(self._store.get_json(CURRENT_KEY))

    def for_dataset(self, dataset: str) -> Binding | None:
        return self.current().get(dataset)

    def for_policy(self, policy_identifier: str) -> list[Binding]:
        return [
            binding
            for binding in self.current().values()
            if binding.policy_identifier == policy_identifier
        ]

    def history(self, dataset: str | None = None) -> list[BindingHistoryEntry]:
        entries = _history_from_document(self._store.get_json(HISTORY_KEY))
        if dataset is not None:
            entries = [entry for entry in entries if entry.dataset == dataset]
        return entries

    def version_at(self, dataset: str, at: str) -> Binding | None:
        """Binding that governed ``dataset`` at ISO timestamp ``at``.

        History is append-only and append-ordered; the *last* entry whose ``at``
        is not later than the queried instant wins. Operations sharing a wall
        second are disambiguated by their append position.
        """
        winner: BindingHistoryEntry | None = None
        for entry in self.history(dataset):
            if entry.at <= at:
                winner = entry
        if winner is None:
            return None
        return Binding(
            dataset=winner.dataset,
            policy_identifier=winner.policy_identifier,
            version=winner.version,
            release_id=winner.release_id,
            updated_at=winner.at,
        )

    def apply(
        self,
        changes: "tuple[BindingChange, ...]",
        kind: str,
        release_id: str,
        moment: str | None = None,
        extra_documents: dict[str, object] | None = None,
    ) -> None:
        """Move every affected pointer and append its history in one write.

        Reads, validation by the caller, and the resulting writes happen under a
        single reentrant store lock, so no two publishes can interleave on the
        same dataset. ``extra_documents`` lets the caller persist the new
        version and release record in the same atomic batch.
        """
        when = moment or utc_now()
        with self._store.transaction():
            bindings = _bindings_from_document(self._store.get_json(CURRENT_KEY))
            history = _history_from_document(self._store.get_json(HISTORY_KEY))
            # Extra documents are immutable facts (new version, new release):
            # refuse to clobber one rather than silently overwriting history.
            if extra_documents:
                existing = [key for key in extra_documents if self._store.has(key)]
                if existing:
                    from maskhub.errors import Conflict

                    raise Conflict(f"refusing to overwrite immutable records: {existing}")
            for change in changes:
                bindings[change.dataset] = Binding(
                    dataset=change.dataset,
                    policy_identifier=change.policy_identifier,
                    version=change.to_version,
                    release_id=release_id,
                    updated_at=when,
                )
                history.append(
                    BindingHistoryEntry(
                        dataset=change.dataset,
                        policy_identifier=change.policy_identifier,
                        version=change.to_version,
                        release_id=release_id,
                        kind=kind,
                        at=when,
                    )
                )
            documents: dict[str, object] = {
                CURRENT_KEY: {
                    dataset: binding.as_dict() for dataset, binding in bindings.items()
                },
                HISTORY_KEY: [entry.as_dict() for entry in history],
            }
            if extra_documents:
                documents.update(extra_documents)
            self._store.put_many_json(documents)
