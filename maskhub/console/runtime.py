"""Composition root for the gateway."""

from __future__ import annotations

from dataclasses import dataclass

from maskhub.audit.journal import Journal
from maskhub.catalog.dataset import DatasetRegistry
from maskhub.policy.registry import PolicyRegistry
from maskhub.rule.registry import RuleRegistry
from maskhub.source.registry import SourceRegistry
from maskhub.store.kvstore import RecordStore


@dataclass
class Runtime:
    store: RecordStore
    journal: Journal
    sources: SourceRegistry
    datasets: DatasetRegistry
    rules: RuleRegistry
    policies: PolicyRegistry


def build_runtime(store: RecordStore) -> Runtime:
    rules = RuleRegistry(store)
    datasets = DatasetRegistry(store)
    return Runtime(
        store=store,
        journal=Journal(store),
        sources=SourceRegistry(store),
        datasets=datasets,
        rules=rules,
        policies=PolicyRegistry(store, rules=rules, datasets=datasets),
    )
