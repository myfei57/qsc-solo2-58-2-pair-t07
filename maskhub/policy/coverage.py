"""Coverage of sensitive fields by a specific immutable policy version.

Coverage can be checked against any released version; the default is the
version currently serving the dataset, so the report describes what actually
applies to data today rather than what some mutable draft might become.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from maskhub.catalog.dataset import DatasetRegistry
from maskhub.errors import NotFound
from maskhub.policy.registry import PolicyRegistry
from maskhub.rule.registry import RuleRegistry


@dataclass(frozen=True)
class CoverageReport:
    policy: str
    version: int
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


def resolve_version(policies: PolicyRegistry, policy_id: str, dataset_id: str,
                    version: int | None) -> int:
    if version is not None:
        policies.get_version(policy_id, version)
        return version
    binding = policies.serving_for_dataset(dataset_id)
    if binding is None or binding.policy_identifier != policy_id:
        raise NotFound(
            f"policy {policy_id} does not serve dataset {dataset_id}; "
            "pass an explicit version"
        )
    return binding.version


def check_coverage(
    policy_id: str,
    dataset_id: str,
    policies: PolicyRegistry,
    datasets: DatasetRegistry,
    rules: RuleRegistry,
    version: int | None = None,
) -> CoverageReport:
    version_number = resolve_version(policies, policy_id, dataset_id, version)
    policy_version = policies.get_version(policy_id, version_number)
    dataset = datasets.get(dataset_id)

    covered: list[str] = []
    uncovered: list[str] = []
    used: set[str] = set()
    for field in dataset.masked_fields():
        hit = False
        for rule_id in policy_version.rules:
            rule = rules.get(rule_id)
            if rule.matches(field.name):
                hit = True
                used.add(rule_id)
        (covered if hit else uncovered).append(field.name)
    unmatched = tuple(sorted(set(policy_version.rules) - used))
    return CoverageReport(
        policy=policy_id,
        version=version_number,
        dataset=dataset.identifier,
        covered=tuple(covered),
        uncovered=tuple(uncovered),
        unmatched_rules=unmatched,
    )
