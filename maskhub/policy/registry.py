"""Policy orchestration: drafts, immutable versions, scope, releases.

Invariants enforced here:

* a publish mints one immutable version and atomically points the requested
  datasets at it;
* a dataset is served by exactly one policy/version at any time, so publishing
  onto a dataset owned by another policy is a conflict unless ``force`` is set;
* rollback points scope back at an already-existing version and never mints or
  deletes a version;
* nothing here rewrites history or already-processed batch stamps.
"""

from __future__ import annotations

from maskhub.catalog.dataset import DatasetRegistry
from maskhub.errors import Conflict, InvalidRequest, NotFound
from maskhub.policy.batch import BatchRecord, BatchRegistry, stamp
from maskhub.policy.bindings import Binding, BindingHistoryEntry, BindingLedger
from maskhub.policy.draft import DraftRegistry, PolicyDraft
from maskhub.policy.impact import ImpactReport, ScopeImpact
from maskhub.policy.release import (
    BindingChange,
    Release,
    ReleaseKind,
    ReleaseRegistry,
    new_release_id,
)
from maskhub.policy.version import PolicyVersion, VersionRegistry, snapshot
from maskhub.rule.registry import RuleRegistry
from maskhub.store.kvstore import RecordStore
from maskhub.timeutil import utc_now


class PolicyRegistry:
    def __init__(
        self,
        store: RecordStore,
        rules: RuleRegistry | None = None,
        datasets: DatasetRegistry | None = None,
    ) -> None:
        self._store = store
        self.rules = rules
        self.datasets = datasets
        self.drafts = DraftRegistry(store)
        self.versions = VersionRegistry(store)
        self.releases = ReleaseRegistry(store)
        self.bindings = BindingLedger(store)
        self.batches = BatchRegistry(store)

    # ------------------------------------------------------------------ drafts

    def save_draft(
        self,
        identifier: str,
        rules: tuple[str, ...],
        proposed_scope: tuple[str, ...] = (),
    ) -> PolicyDraft:
        self._validate_rules(rules)
        if proposed_scope:
            self._validate_datasets(proposed_scope)
        draft = PolicyDraft(
            identifier=identifier, rules=tuple(rules), proposed_scope=tuple(proposed_scope)
        )
        return self.drafts.save(draft)

    def get_draft(self, identifier: str) -> PolicyDraft:
        return self.drafts.get(identifier)

    def drafts_all(self) -> list[PolicyDraft]:
        return self.drafts.all()

    # ---------------------------------------------------------------- versions

    def get_version(self, policy_identifier: str, version: int) -> PolicyVersion:
        return self.versions.get(policy_identifier, version)

    def versions_for(self, policy_identifier: str) -> list[PolicyVersion]:
        return self.versions.versions(policy_identifier)

    def latest_version(self, policy_identifier: str) -> PolicyVersion | None:
        return self.versions.latest(policy_identifier)

    def releases_for(self, policy_identifier: str) -> list[Release]:
        return self.releases.for_policy(policy_identifier)

    def release(self, identifier: str) -> Release:
        return self.releases.get(identifier)

    # ------------------------------------------------------------------ scope

    def serving(self) -> list[Binding]:
        return list(self.bindings.current().values())

    def serving_for_policy(self, policy_identifier: str) -> list[Binding]:
        return self.bindings.for_policy(policy_identifier)

    def serving_for_dataset(self, dataset: str) -> Binding | None:
        return self.bindings.for_dataset(dataset)

    def history(self, dataset: str | None = None) -> list[BindingHistoryEntry]:
        return self.bindings.history(dataset)

    def governing(self, dataset: str, at: str) -> Binding | None:
        return self.bindings.version_at(dataset, at)

    # ------------------------------------------------------------------ batch

    def process_batch(
        self,
        batch_identifier: str,
        dataset: str,
        record_count: int = 0,
    ) -> BatchRecord:
        """Stamp a processed batch with the version serving it right now."""
        self._validate_datasets((dataset,))
        binding = self.bindings.for_dataset(dataset)
        if binding is None:
            raise Conflict(f"dataset {dataset} has no serving policy version")
        record = stamp(
            identifier=batch_identifier,
            dataset=dataset,
            policy_identifier=binding.policy_identifier,
            version=binding.version,
            release_id=binding.release_id,
            record_count=record_count,
        )
        return self.batches.record(record)

    def batch(self, identifier: str) -> BatchRecord:
        return self.batches.get(identifier)

    def batches_for_dataset(self, dataset: str) -> list[BatchRecord]:
        return self.batches.for_dataset(dataset)

    # ----------------------------------------------------------------- preview

    def preview_publish(
        self,
        policy_identifier: str,
        scope: tuple[str, ...] | None = None,
    ) -> ImpactReport:
        """Dry-run a publish; performs no writes."""
        draft = self.drafts.get(policy_identifier)
        target_scope = self._resolve_scope(scope, draft)
        self._validate_rules(draft.rules)
        self._validate_datasets(target_scope)
        target_version = self.versions.next_number(policy_identifier)
        impacts = [
            self._impact_for(dataset, policy_identifier, target_version, draft)
            for dataset in target_scope
        ]
        return ImpactReport(
            policy_identifier=policy_identifier,
            target_version=target_version,
            scope=tuple(target_scope),
            datasets=tuple(impacts),
        )

    def _impact_for(
        self,
        dataset: str,
        policy_identifier: str,
        target_version: int,
        draft: PolicyDraft,
    ) -> ScopeImpact:
        current = self.bindings.for_dataset(dataset)
        blocking: list[str] = []
        if current is None:
            change = "adopt"
            current_policy = None
            current_version = None
        elif current.policy_identifier != policy_identifier:
            change = "switch"
            current_policy = current.policy_identifier
            current_version = current.version
            blocking.append(
                f"dataset {dataset} is served by another policy "
                f"{current.policy_identifier}@v{current.version}; re-publish with force"
            )
        elif current.version == target_version:
            change = "noop"
            current_policy = current.policy_identifier
            current_version = current.version
        else:
            change = "switch"
            current_policy = current.policy_identifier
            current_version = current.version

        covered, uncovered = self._coverage(dataset, draft.rules)
        return ScopeImpact(
            dataset=dataset,
            current_policy=current_policy,
            current_version=current_version,
            target_policy=policy_identifier,
            target_version=target_version,
            change=change,
            covered=tuple(covered),
            uncovered=tuple(uncovered),
            blocking=tuple(blocking),
        )

    # ----------------------------------------------------------------- publish

    def publish(
        self,
        policy_identifier: str,
        scope: tuple[str, ...] | None = None,
        *,
        note: str = "",
        force: bool = False,
    ) -> Release:
        """Mint an immutable version and atomically apply it to the scope."""
        with self._store.transaction():
            draft = self.drafts.get(policy_identifier)
            target_scope = self._resolve_scope(scope, draft)
            self._validate_rules(draft.rules)
            self._validate_datasets(target_scope)
            if not target_scope:
                raise InvalidRequest("publish requires a non-empty scope")

            number = self.versions.next_number(policy_identifier)
            release_id = new_release_id(self._store)
            moment = utc_now()

            changes: list[BindingChange] = []
            for dataset in target_scope:
                current = self.bindings.for_dataset(dataset)
                if (
                    current is not None
                    and current.policy_identifier != policy_identifier
                    and not force
                ):
                    raise Conflict(
                        f"dataset {dataset} is served by another policy "
                        f"{current.policy_identifier}@v{current.version}; "
                        "re-publish with force to transfer it"
                    )
                from_version = current.version if current is not None else None
                changes.append(
                    BindingChange(
                        dataset=dataset,
                        policy_identifier=policy_identifier,
                        from_version=from_version,
                        to_version=number,
                    )
                )

            version = snapshot(
                policy_identifier=policy_identifier,
                rules=draft.rules,
                release_id=release_id,
                version=number,
                note=note,
                created_at=moment,
            )
            release = Release(
                identifier=release_id,
                kind=ReleaseKind.PUBLISH.value,
                policy_identifier=policy_identifier,
                created_at=moment,
                changes=tuple(changes),
                new_version=number,
                note=note,
            )
            self.bindings.apply(
                tuple(changes),
                kind=ReleaseKind.PUBLISH.value,
                release_id=release_id,
                moment=moment,
                extra_documents={
                    _version_doc_key(version): version.as_dict(),
                    _release_doc_key(release): release.as_dict(),
                },
            )
            return release

    # ---------------------------------------------------------------- rollback

    def rollback(
        self,
        policy_identifier: str,
        scope: tuple[str, ...] | None = None,
        *,
        target_version: int | None = None,
        note: str = "",
    ) -> Release:
        """Point scope back at an earlier version; mints and deletes nothing."""
        with self._store.transaction():
            bound = [
                binding
                for binding in self.bindings.for_policy(policy_identifier)
                if scope is None or binding.dataset in scope
            ]
            if scope is not None:
                owned = {binding.dataset for binding in bound}
                missing = sorted(set(scope) - owned)
                if missing:
                    raise Conflict(
                        f"datasets not served by {policy_identifier}: {missing}"
                    )
            if not bound:
                raise Conflict(f"policy {policy_identifier} serves no such scope")

            chosen_version = self._resolve_rollback_version(
                policy_identifier, bound, target_version
            )

            changes: list[BindingChange] = []
            for binding in bound:
                if binding.version == chosen_version:
                    continue
                changes.append(
                    BindingChange(
                        dataset=binding.dataset,
                        policy_identifier=policy_identifier,
                        from_version=binding.version,
                        to_version=chosen_version,
                    )
                )
            if not changes:
                raise Conflict(
                    f"scope already on {policy_identifier}@v{chosen_version}; nothing to roll back"
                )

            release_id = new_release_id(self._store)
            moment = utc_now()
            release = Release(
                identifier=release_id,
                kind=ReleaseKind.ROLLBACK.value,
                policy_identifier=policy_identifier,
                created_at=moment,
                changes=tuple(changes),
                target_version=chosen_version,
                note=note,
            )
            self.bindings.apply(
                tuple(changes),
                kind=ReleaseKind.ROLLBACK.value,
                release_id=release_id,
                moment=moment,
                extra_documents={_release_doc_key(release): release.as_dict()},
            )
            # ``version`` documents that an older immutable version is now
            # serving again; it itself stays untouched.
            return release

    def _resolve_rollback_version(
        self,
        policy_identifier: str,
        bound: list[Binding],
        target_version: int | None,
    ) -> int:
        if target_version is not None:
            known = {item.version for item in self.versions.versions(policy_identifier)}
            if target_version not in known:
                raise NotFound(
                    f"unknown policy version {policy_identifier}@v{target_version}"
                )
            return target_version
        # Default "previous version": the immediate predecessor of the newest
        # released version. Explicit target_version can repoint at any older
        # release, including a non-adjacent one.
        existing = self.versions.versions(policy_identifier)
        if len(existing) < 2:
            raise Conflict(f"policy {policy_identifier} has no previous version")
        return existing[-2].version

    # ----------------------------------------------------------------- helpers

    def _resolve_scope(
        self, scope: tuple[str, ...] | None, draft: PolicyDraft
    ) -> list[str]:
        if scope:
            return list(dict.fromkeys(scope))
        if draft.proposed_scope:
            return list(draft.proposed_scope)
        raise InvalidRequest("scope is required (pass scope or set draft proposed_scope)")

    def _validate_rules(self, rules: tuple[str, ...]) -> None:
        if self.rules is None:
            return
        missing = [rule_id for rule_id in rules if self.rules.find(rule_id) is None]
        if missing:
            raise NotFound(f"unknown rules: {missing}")

    def _validate_datasets(self, datasets: tuple[str, ...]) -> None:
        if self.datasets is None:
            return
        missing = [
            dataset_id
            for dataset_id in datasets
            if self.datasets.find(dataset_id) is None
        ]
        if missing:
            raise NotFound(f"unknown datasets: {missing}")

    def _coverage(
        self, dataset_id: str, rule_ids: tuple[str, ...]
    ) -> tuple[list[str], list[str]]:
        if self.datasets is None or self.rules is None:
            return [], []
        dataset = self.datasets.get(dataset_id)
        covered: list[str] = []
        uncovered: list[str] = []
        for field in dataset.masked_fields():
            hit = any(self.rules.get(rule_id).matches(field.name) for rule_id in rule_ids)
            (covered if hit else uncovered).append(field.name)
        return covered, uncovered


def _version_doc_key(version: PolicyVersion) -> str:
    from maskhub.policy.version import version_key

    return version_key(version.policy_identifier, version.version)


def _release_doc_key(release: Release) -> str:
    from maskhub.policy.release import release_key

    return release_key(release.identifier)
