"""Masking policy endpoints."""

from __future__ import annotations

from maskhub.console.http import Request, Response
from maskhub.console.validation import (
    optional_int,
    optional_text,
    optional_text_list,
    require_text,
    require_text_list,
)
from maskhub.errors import InvalidRequest
from maskhub.policy import release
from maskhub.policy.coverage import check_coverage
from maskhub.policy.policy import Policy


def list_policies(runtime, request: Request) -> Response:
    serving: dict[str, dict[str, int]] = {}
    for assignment in runtime.versions.assignments():
        serving.setdefault(assignment.policy, {})[assignment.dataset] = assignment.version
    policies = []
    for policy in runtime.policies.all():
        payload = policy.as_dict()
        payload["serving"] = serving.get(policy.identifier, {})
        policies.append(payload)
    return Response.json(200, {"count": len(policies), "policies": policies})


def create_policy(runtime, request: Request) -> Response:
    payload = request.json_object()
    policy = Policy(
        identifier=require_text(payload, "identifier"),
        datasets=tuple(require_text_list(payload, "datasets")),
        rules=tuple(require_text_list(payload, "rules")),
    )
    for rule_id in policy.rules:
        runtime.rules.get(rule_id)
    for dataset_id in policy.datasets:
        runtime.datasets.get(dataset_id)
    runtime.policies.save(policy)
    runtime.journal.append("policy.create", policy.identifier, ",".join(policy.datasets))
    return Response.json(200, policy.as_dict())


def preview_publish(runtime, request: Request) -> Response:
    payload = request.json_object()
    preview = release.preview_publish(
        require_text(payload, "identifier"),
        optional_text_list(payload, "datasets"),
        runtime.policies,
        runtime.versions,
        runtime.datasets,
        runtime.rules,
    )
    return Response.json(200, preview.as_dict())


def publish_policy(runtime, request: Request) -> Response:
    payload = request.json_object()
    version, events = release.publish(
        require_text(payload, "identifier"),
        optional_text_list(payload, "datasets"),
        optional_text(payload, "note"),
        runtime.policies,
        runtime.versions,
        runtime.datasets,
        runtime.store,
    )
    runtime.journal.append(
        "policy.publish", version.ref, ",".join(event.dataset for event in events)
    )
    return Response.json(
        200,
        {
            "version": version.as_dict(),
            "assigned": [event.as_dict() for event in events],
        },
    )


def rollback_policy(runtime, request: Request) -> Response:
    payload = request.json_object()
    identifier = require_text(payload, "identifier")
    events = release.rollback(
        identifier,
        optional_text_list(payload, "datasets"),
        runtime.policies,
        runtime.versions,
        runtime.store,
    )
    runtime.journal.append(
        "policy.rollback", identifier, ",".join(event.dataset for event in events)
    )
    return Response.json(
        200,
        {"policy": identifier, "rolled_back": [event.as_dict() for event in events]},
    )


def retire_policy(runtime, request: Request) -> Response:
    payload = request.json_object()
    identifier = require_text(payload, "identifier")
    retired, events = release.retire(
        identifier, runtime.policies, runtime.versions, runtime.store
    )
    runtime.journal.append(
        "policy.retire", identifier, ",".join(event.dataset for event in events)
    )
    return Response.json(
        200,
        {
            "policy": retired.as_dict(),
            "released": [event.as_dict() for event in events],
        },
    )


def list_versions(runtime, request: Request) -> Response:
    policy_id = _require_query(request, "policy")
    runtime.policies.get(policy_id)
    versions = runtime.versions.versions(policy_id)
    return Response.json(
        200,
        {
            "policy": policy_id,
            "count": len(versions),
            "versions": [version.as_dict() for version in versions],
        },
    )


def get_version(runtime, request: Request) -> Response:
    policy_id = _require_query(request, "policy")
    raw = _require_query(request, "version")
    try:
        number = int(raw)
    except ValueError as exc:
        raise InvalidRequest(f"version must be an integer: {raw!r}") from exc
    return Response.json(200, runtime.versions.get_version(policy_id, number).as_dict())


def policy_timeline(runtime, request: Request) -> Response:
    dataset_id = _require_query(request, "dataset")
    runtime.datasets.get(dataset_id)
    current = runtime.versions.assignment(dataset_id)
    events = runtime.versions.timeline(dataset_id)
    return Response.json(
        200,
        {
            "dataset": dataset_id,
            "current": current.as_dict() if current else None,
            "events": [event.as_dict() for event in events],
        },
    )


def serving_version(runtime, request: Request) -> Response:
    dataset_id = _require_query(request, "dataset")
    runtime.datasets.get(dataset_id)
    at = request.query_one("at")
    assignment = runtime.versions.serving_at(dataset_id, at)
    return Response.json(
        200,
        {
            "dataset": dataset_id,
            "at": at,
            "policy": assignment.policy if assignment else None,
            "version": assignment.version if assignment else None,
            "ref": assignment.ref if assignment else None,
        },
    )


def record_batch(runtime, request: Request) -> Response:
    payload = request.json_object()
    batch = require_text(payload, "batch")
    dataset_id = require_text(payload, "dataset")
    runtime.datasets.get(dataset_id)
    record = runtime.versions.record_batch(batch, dataset_id)
    runtime.journal.append("batch.record", record.batch, f"{record.dataset}:{record.ref}")
    return Response.json(200, record.as_dict())


def get_batch(runtime, request: Request) -> Response:
    batch = _require_query(request, "batch")
    return Response.json(200, runtime.versions.get_batch(batch).as_dict())


def list_batches(runtime, request: Request) -> Response:
    dataset_id = request.query_one("dataset")
    records = runtime.versions.batches(dataset_id)
    return Response.json(
        200, {"count": len(records), "batches": [record.as_dict() for record in records]}
    )


def policy_coverage(runtime, request: Request) -> Response:
    payload = request.json_object()
    policy_id = require_text(payload, "policy")
    dataset_id = require_text(payload, "dataset")
    report = check_coverage(
        policy_id,
        dataset_id,
        runtime.policies,
        runtime.datasets,
        runtime.rules,
        versions=runtime.versions,
        version=optional_int(payload, "version"),
    )
    return Response.json(200, report.as_dict())


def coverage_gap(runtime, request: Request) -> Response:
    """Report every dataset that no serving policy version fully covers."""

    gaps: list[dict[str, object]] = []
    for dataset in runtime.datasets.all():
        covered: set[str] = set()
        for assignment in runtime.versions.assignments():
            if assignment.dataset != dataset.identifier:
                continue
            report = check_coverage(
                assignment.policy,
                dataset.identifier,
                runtime.policies,
                runtime.datasets,
                runtime.rules,
                versions=runtime.versions,
                version=assignment.version,
            )
            covered.update(report.covered)
        missing = [
            item.name for item in dataset.masked_fields() if item.name not in covered
        ]
        if missing:
            gaps.append({"dataset": dataset.identifier, "uncovered": missing})
    return Response.json(200, {"gaps": gaps, "count": len(gaps)})


def _require_query(request: Request, name: str) -> str:
    value = request.query_one(name)
    if value is None or not value.strip():
        raise InvalidRequest(f"query parameter {name} is required")
    return value.strip()
