"""Pre-publish impact preview.

Computing a preview performs no writes. It answers, for a draft and a proposed
scope, exactly which datasets would move, between which versions, and whether
anything blocks the publish.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScopeImpact:
    dataset: str
    current_policy: str | None
    current_version: int | None
    target_policy: str
    target_version: int
    change: str  # "adopt" | "switch" | "noop"
    covered: tuple[str, ...] = ()
    uncovered: tuple[str, ...] = ()
    blocking: tuple[str, ...] = ()

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocking)

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "current_policy": self.current_policy,
            "current_version": self.current_version,
            "target_policy": self.target_policy,
            "target_version": self.target_version,
            "change": self.change,
            "covered": list(self.covered),
            "uncovered": list(self.uncovered),
            "blocking": list(self.blocking),
            "blocked": self.is_blocked,
        }


@dataclass(frozen=True)
class ImpactReport:
    policy_identifier: str
    target_version: int
    scope: tuple[str, ...]
    datasets: tuple[ScopeImpact, ...]

    @property
    def blocked(self) -> tuple[ScopeImpact, ...]:
        return tuple(item for item in self.datasets if item.is_blocked)

    @property
    def publishable(self) -> bool:
        return not self.blocked

    @property
    def changes(self) -> tuple[ScopeImpact, ...]:
        return tuple(item for item in self.datasets if item.change != "noop")

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_identifier": self.policy_identifier,
            "target_version": self.target_version,
            "scope": list(self.scope),
            "datasets": [item.as_dict() for item in self.datasets],
            "changed": len(self.changes),
            "blocked": [item.dataset for item in self.blocked],
            "publishable": self.publishable,
        }
