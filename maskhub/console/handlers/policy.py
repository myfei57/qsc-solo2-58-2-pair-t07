"""Versioned policy endpoints.

Flow: edit a draft (``/policies``), preview its impact (``/policy/preview``),
publish to mint an immutable version (``/policy/publish``), and roll a scope
back to a prior version (``/policy/rollback``). Serving pointers, release
history and batch provenance are queryable separately.
"""

from __future__ import annotations

from maskhub.console.http import Request, Response
from maskhub.console.validation import optional_text, require_text, require_text_list
from maskhub.errors import InvalidRequest
from maskhub.policy.coverage import check_coverage


def _optional_scope(payload) -> tuple[str, ...] | None:
    scope = payload.get("scope")
    if scope is None:
        return None
    if not isinstance(scope, list) or not scope:
        raise InvalidRequest("scope must be a non-empty list")
    return tuple(require_text_list(payload, "scope"))


def _optional_int(payload, key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidRequest(f"{key} must be a positive integer")
    return value


def list_policies(runtime, request: Request) -> Response:
    drafts = [draft.as_dict() for draft in runtime.policies.drafts_all()]
    return Response.json(200, {"count": len(drafts), "policies": drafts})


def create_policy(runtime, request: Request) -> Response:
    payload = request.json_object()
    identifier = require_text(payload, "identifier")
    rules = tuple(require_text_list(payload, "rules"))
    scope = tuple(require_text_list(payload, "scope")) if "scope" in payload else ()
    draft = runtime.policies.save_draft(identifier, rules, scope)
    runtime.journal.append("policy.draft.save", identifier, ",".join(scope))
    return Response.json(200, draft.as_dict())


def update_policy(runtime, request: Request) -> Response:
    """Edit the draft. Never touches published versions or serving scope."""
    payload = request.json_object()
    identifier = require_text(payload, "identifier")
    existing = runtime.policies.get_draft(identifier)
    rules = payload.get("rules")
    if rules is not None:
        rules = tuple(require_text_list(payload, "rules"))
    else:
        rules = existing.rules
    if "scope" in payload:
        scope = tuple(require_text_list(payload, "scope"))
    else:
        scope = existing.proposed_scope
    draft = runtime.policies.save_draft(identifier, rules, scope)
    runtime.journal.append("policy.draft.save", identifier, ",".join(scope))
    return Response.json(200, draft.as_dict())


def list_versions(runtime, request: Request) -> Response:
    policy_id = request.query_one("policy", "") or ""
    if not policy_id:
        raise InvalidRequest("policy query parameter is required")
    versions = runtime.policies.versions_for(policy_id)
    return Response.json(
        200,
        {
            "policy": policy_id,
            "count": len(versions),
            "versions": [version.as_dict() for version in versions],
        },
    )


def get_version(runtime, request: Request) -> Response:
    payload = request.json_object()
    policy_id = require_text(payload, "policy")
    version = _optional_int(payload, "version")
    if version is None:
        raise InvalidRequest("version is required")
    return Response.json(
        200, runtime.policies.get_version(policy_id, version).as_dict()
    )


def preview_publish(runtime, request: Request) -> Response:
    payload = request.json_object()
    policy_id = require_text(payload, "policy")
    report = runtime.policies.preview_publish(policy_id, _optional_scope(payload))
    return Response.json(200, report.as_dict())


def publish_policy(runtime, request: Request) -> Response:
    payload = request.json_object()
    policy_id = require_text(payload, "policy")
    force = bool(payload.get("force", False))
    note = optional_text(payload, "note")
    release = runtime.policies.publish(
        policy_id, _optional_scope(payload), note=note, force=force
    )
    runtime.journal.append(
        "policy.publish",
        policy_id,
        f"release={release.identifier} version=v{release.new_version} "
        f"datasets={','.join(change.dataset for change in release.changes)}",
    )
    return Response.json(200, release.as_dict())


def rollback_policy(runtime, request: Request) -> Response:
    payload = request.json_object()
    policy_id = require_text(payload, "policy")
    target = _optional_int(payload, "target_version")
    note = optional_text(payload, "note")
    release = runtime.policies.rollback(
        policy_id, _optional_scope(payload), target_version=target, note=note
    )
    runtime.journal.append(
        "policy.rollback",
        policy_id,
        f"release={release.identifier} target=v{release.target_version} "
        f"datasets={','.join(change.dataset for change in release.changes)}",
    )
    return Response.json(200, release.as_dict())


def list_releases(runtime, request: Request) -> Response:
    policy_id = request.query_one("policy")
    releases = (
        runtime.policies.releases_for(policy_id)
        if policy_id
        else runtime.policies.releases.all()
    )
    return Response.json(
        200,
        {"count": len(releases), "releases": [release.as_dict() for release in releases]},
    )


def get_release(runtime, request: Request) -> Response:
    release_id = request.query_one("id", "") or ""
    if not release_id:
        raise InvalidRequest("id query parameter is required")
    return Response.json(200, runtime.policies.release(release_id).as_dict())


def serving(runtime, request: Request) -> Response:
    dataset = request.query_one("dataset")
    if dataset is not None:
        binding = runtime.policies.serving_for_dataset(dataset)
        if binding is None:
            return Response.json(404, {"error": f"dataset {dataset} has no serving policy"})
        return Response.json(200, binding.as_dict())
    policy_id = request.query_one("policy")
    bindings = (
        runtime.policies.serving_for_policy(policy_id)
        if policy_id
        else runtime.policies.serving()
    )
    return Response.json(
        200,
        {"count": len(bindings), "bindings": [binding.as_dict() for binding in bindings]},
    )


def scope_history(runtime, request: Request) -> Response:
    dataset = request.query_one("dataset")
    entries = runtime.policies.history(dataset)
    return Response.json(
        200,
        {"count": len(entries), "history": [entry.as_dict() for entry in entries]},
    )


def governing_at(runtime, request: Request) -> Response:
    payload = request.json_object()
    dataset = require_text(payload, "dataset")
    at = require_text(payload, "at")
    binding = runtime.policies.governing(dataset, at)
    if binding is None:
        return Response.json(
            404, {"error": f"no policy governed {dataset} at {at}"}
        )
    return Response.json(200, binding.as_dict())


def policy_coverage(runtime, request: Request) -> Response:
    payload = request.json_object()
    policy_id = require_text(payload, "policy")
    dataset_id = require_text(payload, "dataset")
    version = _optional_int(payload, "version")
    report = check_coverage(
        policy_id,
        dataset_id,
        runtime.policies,
        runtime.datasets,
        runtime.rules,
        version=version,
    )
    return Response.json(200, report.as_dict())


def coverage_gap(runtime, request: Request) -> Response:
    """Report every serving dataset whose current version leaves fields uncovered."""
    gaps: list[dict[str, object]] = []
    for binding in runtime.policies.serving():
        report = check_coverage(
            binding.policy_identifier,
            binding.dataset,
            runtime.policies,
            runtime.datasets,
            runtime.rules,
            version=binding.version,
        )
        if report.uncovered:
            gaps.append(
                {
                    "dataset": binding.dataset,
                    "policy": binding.policy_identifier,
                    "version": binding.version,
                    "uncovered": list(report.uncovered),
                }
            )
    return Response.json(200, {"gaps": gaps, "count": len(gaps)})
