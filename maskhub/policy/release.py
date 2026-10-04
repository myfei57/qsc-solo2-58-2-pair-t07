"""Publish, preview, and rollback flows over drafts, versions, and assignments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from maskhub.catalog.dataset import DatasetRegistry
from maskhub.errors import Conflict, InvalidRequest
from maskhub.policy.coverage import CoverageReport, evaluate_coverage
from maskhub.policy.policy import Policy, PolicyState
from maskhub.policy.registry import PolicyRegistry
from maskhub.policy.version import (
    CAUSE_PUBLISH,
    CAUSE_RETIRE,
    CAUSE_ROLLBACK,
    AssignmentEvent,
    PolicyVersion,
    utcnow,
)
from maskhub.policy.versions import VersionRegistry
from maskhub.rule.registry import RuleRegistry
from maskhub.store.kvstore import RecordStore


@dataclass(frozen=True)
class DatasetImpact:
    """What publishing the current draft would change for one dataset."""

    dataset: str
    current_policy: str | None
    current_version: int | None
    conflict: bool
    coverage: CoverageReport
    added_rules: tuple[str, ...]
    removed_rules: tuple[str, ...]
    newly_covered: tuple[str, ...]
    newly_uncovered: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        current = None
        if self.current_policy is not None and self.current_version is not None:
            current = {"policy": self.current_policy, "version": self.current_version}
        return {
            "dataset": self.dataset,
            "current": current,
            "conflict": self.conflict,
            "coverage": self.coverage.as_dict(),
            "added_rules": list(self.added_rules),
            "removed_rules": list(self.removed_rules),
            "newly_covered": list(self.newly_covered),
            "newly_uncovered": list(self.newly_uncovered),
        }


@dataclass(frozen=True)
class PublishPreview:
    policy: str
    next_version: int
    scope: tuple[str, ...]
    impacts: tuple[DatasetImpact, ...]

    @property
    def conflicts(self) -> tuple[DatasetImpact, ...]:
        return tuple(item for item in self.impacts if item.conflict)

    @property
    def ok(self) -> bool:
        return not self.conflicts

    def as_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy,
            "next_version": self.next_version,
            "scope": list(self.scope),
            "ok": self.ok,
            "conflicts": [
                {
                    "dataset": item.dataset,
                    "served_by": item.current_policy,
                    "version": item.current_version,
                }
                for item in self.conflicts
            ],
            "impact": [item.as_dict() for item in self.impacts],
        }


def preview_publish(
    policy_id: str,
    scope: Sequence[str] | None,
    policies: PolicyRegistry,
    versions: VersionRegistry,
    datasets: DatasetRegistry,
    rules: RuleRegistry,
) -> PublishPreview:
    """Describe what publishing the draft would do, without changing anything."""

    draft = _publishable_draft(policies, policy_id)
    target = _resolve_scope(draft, scope)
    impacts = []
    for dataset_id in target:
        dataset = datasets.get(dataset_id)
        coverage = evaluate_coverage(policy_id, draft.rules, dataset, rules)
        current = versions.assignment(dataset_id)
        conflict = current is not None and current.policy != policy_id
        added: tuple[str, ...] = ()
        removed: tuple[str, ...] = ()
        newly_covered: tuple[str, ...] = ()
        newly_uncovered: tuple[str, ...] = ()
        if current is not None and current.policy == policy_id:
            serving = versions.get_version(policy_id, current.version)
            added = tuple(sorted(set(draft.rules) - set(serving.rules)))
            removed = tuple(sorted(set(serving.rules) - set(draft.rules)))
            before = evaluate_coverage(serving.ref, serving.rules, dataset, rules)
            newly_covered = tuple(sorted(set(coverage.covered) - set(before.covered)))
            newly_uncovered = tuple(sorted(set(before.covered) - set(coverage.covered)))
        impacts.append(
            DatasetImpact(
                dataset=dataset_id,
                current_policy=current.policy if current else None,
                current_version=current.version if current else None,
                conflict=conflict,
                coverage=coverage,
                added_rules=added,
                removed_rules=removed,
                newly_covered=newly_covered,
                newly_uncovered=newly_uncovered,
            )
        )
    return PublishPreview(
        policy=policy_id,
        next_version=versions.next_version_number(policy_id),
        scope=tuple(target),
        impacts=tuple(impacts),
    )


def publish(
    policy_id: str,
    scope: Sequence[str] | None,
    note: str,
    policies: PolicyRegistry,
    versions: VersionRegistry,
    datasets: DatasetRegistry,
    store: RecordStore,
) -> tuple[PolicyVersion, list[AssignmentEvent]]:
    """Snapshot the draft as a new immutable version and point the scope at it."""

    draft = _publishable_draft(policies, policy_id)
    target = _resolve_scope(draft, scope)
    for dataset_id in target:
        datasets.get(dataset_id)
    conflicts = []
    for dataset_id in target:
        current = versions.assignment(dataset_id)
        if current is not None and current.policy != policy_id:
            conflicts.append(f"{dataset_id} is served by {current.ref}")
    if conflicts:
        raise Conflict("cannot publish over another policy: " + "; ".join(conflicts))
    with store.transaction():
        version = PolicyVersion(
            policy=policy_id,
            version=versions.next_version_number(policy_id),
            datasets=tuple(draft.datasets),
            rules=tuple(draft.rules),
            published_at=utcnow(),
            note=note,
        )
        versions.save_version(version)
        moves = [(dataset_id, policy_id, version.version) for dataset_id in target]
        events = versions.reassign(moves, CAUSE_PUBLISH)
    return version, events


def rollback(
    policy_id: str,
    scope: Sequence[str] | None,
    policies: PolicyRegistry,
    versions: VersionRegistry,
    store: RecordStore,
) -> list[AssignmentEvent]:
    """Point datasets back to the previous version; versions are never deleted."""

    policies.get(policy_id)
    current = versions.assignments_for(policy_id)
    if not current:
        raise Conflict(f"policy {policy_id} has no serving assignments to roll back")
    if scope is not None:
        served = {item.dataset for item in current}
        unknown = sorted(set(scope) - served)
        if unknown:
            raise InvalidRequest(f"datasets not served by {policy_id}: {unknown}")
        targets = [item for item in current if item.dataset in set(scope)]
    else:
        latest = versions.latest_version(policy_id)
        targets = [item for item in current if latest is not None and item.version == latest.version]
        if not targets:
            targets = list(current)
    moves = []
    blocked = []
    for assignment in targets:
        previous = versions.previous_version(
            policy_id, assignment.version, dataset=assignment.dataset
        )
        if previous is None:
            blocked.append(assignment.dataset)
        else:
            moves.append((assignment.dataset, policy_id, previous.version))
    if blocked:
        raise Conflict(f"no earlier version to roll back to for {sorted(blocked)}")
    with store.transaction():
        return versions.reassign(moves, CAUSE_ROLLBACK)


def retire(
    policy_id: str,
    policies: PolicyRegistry,
    versions: VersionRegistry,
    store: RecordStore,
) -> tuple[Policy, list[AssignmentEvent]]:
    """Release every dataset the policy serves and close the draft."""

    with store.transaction():
        current = versions.assignments_for(policy_id)
        moves = [(item.dataset, policy_id, None) for item in current]
        events = versions.reassign(moves, CAUSE_RETIRE) if moves else []
        retired = policies.retire(policy_id)
    return retired, events


def _publishable_draft(policies: PolicyRegistry, policy_id: str) -> Policy:
    draft = policies.get(policy_id)
    if draft.state_enum is PolicyState.RETIRED:
        raise Conflict(f"policy {policy_id} is retired and cannot be published")
    return draft


def _resolve_scope(draft: Policy, scope: Sequence[str] | None) -> list[str]:
    if scope is None:
        return list(draft.datasets)
    unknown = sorted(set(scope) - set(draft.datasets))
    if unknown:
        raise InvalidRequest(f"datasets not targeted by {draft.identifier}: {unknown}")
    return list(dict.fromkeys(scope))
