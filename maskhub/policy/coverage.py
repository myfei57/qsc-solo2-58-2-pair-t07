"""Static check that a rule set covers every declared sensitive field."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence

from maskhub.catalog.dataset import Dataset, DatasetRegistry
from maskhub.errors import NotFound
from maskhub.policy.registry import PolicyRegistry
from maskhub.policy.versions import VersionRegistry
from maskhub.rule.registry import RuleRegistry


@dataclass(frozen=True)
class CoverageReport:
    policy: str
    dataset: str
    covered: tuple[str, ...]
    uncovered: tuple[str, ...]
    unmatched_rules: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.uncovered

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["complete"] = self.complete
        return payload


def evaluate_coverage(
    policy_ref: str,
    rule_ids: Sequence[str],
    dataset: Dataset,
    rules: RuleRegistry,
) -> CoverageReport:
    sensitive = [item.name for item in dataset.masked_fields()]
    covered: list[str] = []
    uncovered: list[str] = []
    used: set[str] = set()
    for name in sensitive:
        hit = False
        for rule_id in rule_ids:
            rule = rules.get(rule_id)
            if rule.matches(name):
                hit = True
                used.add(rule_id)
        (covered if hit else uncovered).append(name)
    unmatched = tuple(sorted(set(rule_ids) - used))
    return CoverageReport(
        policy=policy_ref,
        dataset=dataset.identifier,
        covered=tuple(covered),
        uncovered=tuple(uncovered),
        unmatched_rules=unmatched,
    )


def check_coverage(
    policy_id: str,
    dataset_id: str,
    policies: PolicyRegistry,
    datasets: DatasetRegistry,
    rules: RuleRegistry,
    versions: VersionRegistry | None = None,
    version: int | None = None,
) -> CoverageReport:
    if version is None:
        policy = policies.get(policy_id)
        rule_ids, scope, ref = policy.rules, policy.datasets, policy.identifier
    else:
        if versions is None:
            raise NotFound(f"unknown version {policy_id}@{version}")
        snapshot = versions.get_version(policy_id, version)
        rule_ids, scope, ref = snapshot.rules, snapshot.datasets, snapshot.ref
    if dataset_id not in scope:
        raise NotFound(f"policy {ref} does not target dataset {dataset_id}")
    dataset = datasets.get(dataset_id)
    return evaluate_coverage(ref, rule_ids, dataset, rules)
