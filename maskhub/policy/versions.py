"""Storage for policy versions, serving assignments, and batch records."""

from __future__ import annotations

from datetime import datetime, timezone

from maskhub.errors import Conflict, InvalidRequest, NotFound
from maskhub.policy.version import (
    Assignment,
    AssignmentEvent,
    BatchRecord,
    PolicyVersion,
    utcnow,
)
from maskhub.store.kvstore import RecordStore

PREFIX_VERSION = "pver."
PREFIX_ASSIGNMENT = "pserve."
PREFIX_BATCH = "batch."
EVENT_LOG_KEY = "pservelog"


def version_key(policy: str, version: int) -> str:
    return f"{PREFIX_VERSION}{policy}.{version}"


def assignment_key(dataset: str) -> str:
    return PREFIX_ASSIGNMENT + dataset


def batch_key(batch: str) -> str:
    return PREFIX_BATCH + batch


class VersionRegistry:
    def __init__(self, store: RecordStore) -> None:
        self._store = store

    # --- immutable versions -------------------------------------------------

    def save_version(self, version: PolicyVersion) -> PolicyVersion:
        key = version_key(version.policy, version.version)
        if self._store.has(key):
            raise Conflict(f"{version.ref} already exists; versions are immutable")
        self._store.put_json(key, version.as_dict())
        return version

    def find_version(self, policy: str, version: int) -> PolicyVersion | None:
        return PolicyVersion.from_dict(self._store.get_json(version_key(policy, version)))

    def get_version(self, policy: str, version: int) -> PolicyVersion:
        found = self.find_version(policy, version)
        if found is None:
            raise NotFound(f"unknown version {policy}@{version}")
        return found

    def versions(self, policy: str) -> list[PolicyVersion]:
        from maskhub.store.query import keys_with_prefix

        found = []
        for key in keys_with_prefix(self._store, PREFIX_VERSION + policy + "."):
            version = PolicyVersion.from_dict(self._store.get_json(key))
            if version is not None and version.policy == policy:
                found.append(version)
        return sorted(found, key=lambda item: item.version)

    def latest_version(self, policy: str) -> PolicyVersion | None:
        existing = self.versions(policy)
        return existing[-1] if existing else None

    def next_version_number(self, policy: str) -> int:
        latest = self.latest_version(policy)
        return latest.version + 1 if latest is not None else 1

    def previous_version(
        self, policy: str, version: int, dataset: str | None = None
    ) -> PolicyVersion | None:
        """Highest version below ``version`` (still targeting ``dataset`` if given)."""

        candidates = [
            item
            for item in self.versions(policy)
            if item.version < version and (dataset is None or dataset in item.datasets)
        ]
        return candidates[-1] if candidates else None

    # --- serving assignments (one version per dataset) ----------------------

    def assignment(self, dataset: str) -> Assignment | None:
        return Assignment.from_dict(self._store.get_json(assignment_key(dataset)))

    def assignments(self) -> list[Assignment]:
        from maskhub.store.query import keys_with_prefix

        found = []
        for key in keys_with_prefix(self._store, PREFIX_ASSIGNMENT):
            assignment = Assignment.from_dict(self._store.get_json(key))
            if assignment is not None:
                found.append(assignment)
        return sorted(found, key=lambda item: item.dataset)

    def assignments_for(self, policy: str) -> list[Assignment]:
        return [item for item in self.assignments() if item.policy == policy]

    def reassign(
        self, moves: list[tuple[str, str, int | None]], cause: str
    ) -> list[AssignmentEvent]:
        """Move dataset pointers and append the moves to the history log.

        ``moves`` holds ``(dataset, policy, to_version)`` triples; a ``None``
        target releases the dataset from service. All moves apply under one
        store lock so concurrent requests cannot interleave.
        """

        now = utcnow()
        events: list[AssignmentEvent] = []
        with self._store.transaction():
            for dataset, policy, to_version in moves:
                current = self.assignment(dataset)
                events.append(
                    AssignmentEvent(
                        at=now,
                        dataset=dataset,
                        policy=policy,
                        cause=cause,
                        from_version=current.version if current else None,
                        to_version=to_version,
                    )
                )
                if to_version is None:
                    self._store.delete(assignment_key(dataset))
                else:
                    self._store.put_json(
                        assignment_key(dataset),
                        Assignment(
                            dataset=dataset, policy=policy, version=to_version, since=now
                        ).as_dict(),
                    )
            self._append_events(events)
        return events

    # --- assignment history --------------------------------------------------

    def events(self, dataset: str | None = None) -> list[AssignmentEvent]:
        entries = _normalize_events(self._store.get_json(EVENT_LOG_KEY))
        if dataset is None:
            return entries
        return [entry for entry in entries if entry.dataset == dataset]

    def timeline(self, dataset: str) -> list[AssignmentEvent]:
        return self.events(dataset)

    def serving_at(self, dataset: str, at: str | None = None) -> Assignment | None:
        """The version governing ``dataset`` now or at a past moment."""

        if at is None:
            return self.assignment(dataset)
        moment = _parse_moment(at)
        result: AssignmentEvent | None = None
        for event in self.timeline(dataset):
            if datetime.fromisoformat(event.at) <= moment:
                result = event
            else:
                break
        if result is None or result.to_version is None:
            return None
        return Assignment(
            dataset=dataset, policy=result.policy, version=result.to_version, since=result.at
        )

    # --- batch attribution (write once, never rewritten) ---------------------

    def record_batch(self, batch: str, dataset: str) -> BatchRecord:
        with self._store.transaction():
            if self._store.has(batch_key(batch)):
                raise Conflict(f"batch {batch} is already recorded")
            assignment = self.assignment(dataset)
            if assignment is None:
                raise Conflict(f"dataset {dataset} is not served by any policy version")
            record = BatchRecord(
                batch=batch,
                dataset=dataset,
                policy=assignment.policy,
                version=assignment.version,
                recorded_at=utcnow(),
            )
            self._store.put_json(batch_key(batch), record.as_dict())
            return record

    def find_batch(self, batch: str) -> BatchRecord | None:
        return BatchRecord.from_dict(self._store.get_json(batch_key(batch)))

    def get_batch(self, batch: str) -> BatchRecord:
        record = self.find_batch(batch)
        if record is None:
            raise NotFound(f"unknown batch {batch}")
        return record

    def batches(self, dataset: str | None = None) -> list[BatchRecord]:
        from maskhub.store.query import keys_with_prefix

        found = []
        for key in keys_with_prefix(self._store, PREFIX_BATCH):
            record = BatchRecord.from_dict(self._store.get_json(key))
            if record is not None and (dataset is None or record.dataset == dataset):
                found.append(record)
        return sorted(found, key=lambda item: item.recorded_at)

    def _append_events(self, events: list[AssignmentEvent]) -> None:
        def mutate(document: object) -> tuple[object, None]:
            entries = _normalize_events(document)
            entries.extend(events)
            return [entry.as_dict() for entry in entries], None

        self._store.update_json(EVENT_LOG_KEY, mutate)


def _normalize_events(document: object) -> list[AssignmentEvent]:
    if not isinstance(document, list):
        return []
    entries = [AssignmentEvent.from_dict(item) for item in document]
    return [entry for entry in entries if entry is not None]


def _parse_moment(raw: str) -> datetime:
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidRequest(f"invalid timestamp {raw!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment
