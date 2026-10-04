"""Batch provenance.

Every processed batch is stamped with the exact policy version that served it
at processing time. The stamp is immutable: rolling a scope back to an older
version never rewrites what already happened, so "which version governed this
batch?" stays answerable forever.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from maskhub.errors import Conflict, NotFound
from maskhub.store.kvstore import RecordStore
from maskhub.timeutil import utc_now

PREFIX = "policy.batch."


def batch_key(identifier: str) -> str:
    return PREFIX + identifier


@dataclass(frozen=True)
class BatchRecord:
    identifier: str
    dataset: str
    policy_identifier: str
    version: int
    processed_at: str
    release_id: str
    record_count: int = 0

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValueError("batch identifier is required")
        if not self.dataset.strip():
            raise ValueError("batch dataset is required")
        if not self.policy_identifier.strip():
            raise ValueError("batch policy identifier is required")
        if self.version < 1:
            raise ValueError("batch version must be >= 1")
        if self.record_count < 0:
            raise ValueError("batch record count must be >= 0")

    @property
    def version_ref(self) -> str:
        return f"{self.policy_identifier}@v{self.version}"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: object) -> "BatchRecord | None":
        if not isinstance(payload, dict):
            return None
        identifier = payload.get("identifier")
        dataset = payload.get("dataset")
        policy_identifier = payload.get("policy_identifier")
        version = payload.get("version")
        processed_at = payload.get("processed_at")
        release_id = payload.get("release_id")
        record_count = payload.get("record_count", 0)
        if not all(
            isinstance(item, str)
            for item in (identifier, dataset, policy_identifier, processed_at, release_id)
        ):
            return None
        if not isinstance(version, int) or not isinstance(record_count, int):
            return None
        try:
            return cls(
                identifier=identifier,
                dataset=dataset,
                policy_identifier=policy_identifier,
                version=version,
                processed_at=processed_at,
                release_id=release_id,
                record_count=record_count,
            )
        except ValueError:
            return None


class BatchRegistry:
    def __init__(self, store: RecordStore) -> None:
        self._store = store

    def record(self, batch: BatchRecord) -> BatchRecord:
        if self._store.has(batch_key(batch.identifier)):
            raise Conflict(f"batch {batch.identifier} already recorded")
        self._store.put_json(batch_key(batch.identifier), batch.as_dict())
        return batch

    def get(self, identifier: str) -> BatchRecord:
        batch = BatchRecord.from_dict(self._store.get_json(batch_key(identifier)))
        if batch is None:
            raise NotFound(f"unknown batch {identifier}")
        return batch

    def find(self, identifier: str) -> BatchRecord | None:
        return BatchRecord.from_dict(self._store.get_json(batch_key(identifier)))

    def identifiers(self) -> list[str]:
        from maskhub.store.query import keys_with_prefix

        return [key[len(PREFIX):] for key in keys_with_prefix(self._store, PREFIX)]

    def all(self) -> list[BatchRecord]:
        batches = [self.get(identifier) for identifier in self.identifiers()]
        batches.sort(key=lambda item: item.processed_at)
        return batches

    def for_dataset(self, dataset: str) -> list[BatchRecord]:
        return [batch for batch in self.all() if batch.dataset == dataset]


def stamp(
    identifier: str,
    dataset: str,
    policy_identifier: str,
    version: int,
    release_id: str,
    record_count: int = 0,
    processed_at: str | None = None,
) -> BatchRecord:
    return BatchRecord(
        identifier=identifier,
        dataset=dataset,
        policy_identifier=policy_identifier,
        version=version,
        processed_at=processed_at or utc_now(),
        release_id=release_id,
        record_count=record_count,
    )
