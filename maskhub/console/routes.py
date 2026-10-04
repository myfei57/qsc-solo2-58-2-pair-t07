"""Route table wiring the handlers to the runtime."""

from __future__ import annotations

from functools import partial

from maskhub.console.handlers import audit, batch, catalog, policy, rule, source, system
from maskhub.console.http import Request, Response
from maskhub.console.router import Router


def build_router(runtime) -> Router:
    router = Router()
    bind = partial

    router.get("/health", bind(system.health, runtime))
    router.get("/status", bind(system.status, runtime))

    router.get("/sources", bind(source.list_sources, runtime))
    router.post("/sources", bind(source.register_source, runtime))
    router.post("/source/status", bind(source.set_source_status, runtime))

    router.get("/datasets", bind(catalog.list_datasets, runtime))
    router.post("/datasets", bind(catalog.register_dataset, runtime))
    router.get("/dataset", bind(catalog.get_dataset, runtime))

    router.get("/rules", bind(rule.list_rules, runtime))
    router.post("/rules", bind(rule.create_rule, runtime))
    router.post("/rule/remove", bind(rule.remove_rule, runtime))
    router.post("/rule/match", bind(rule.match_rule, runtime))

    router.get("/policies", bind(policy.list_policies, runtime))
    router.post("/policies", bind(policy.create_policy, runtime))
    router.post("/policy/draft", bind(policy.update_policy, runtime))
    router.get("/policy/versions", bind(policy.list_versions, runtime))
    router.post("/policy/version", bind(policy.get_version, runtime))
    router.post("/policy/preview", bind(policy.preview_publish, runtime))
    router.post("/policy/publish", bind(policy.publish_policy, runtime))
    router.post("/policy/rollback", bind(policy.rollback_policy, runtime))
    router.get("/policy/releases", bind(policy.list_releases, runtime))
    router.get("/policy/release", bind(policy.get_release, runtime))
    router.get("/policy/serving", bind(policy.serving, runtime))
    router.get("/policy/history", bind(policy.scope_history, runtime))
    router.post("/policy/governing-at", bind(policy.governing_at, runtime))
    router.post("/policy/coverage", bind(policy.policy_coverage, runtime))
    router.get("/policy/gaps", bind(policy.coverage_gap, runtime))

    router.post("/batches", bind(batch.record_batch, runtime))
    router.get("/batch", bind(batch.get_batch, runtime))
    router.get("/batches", bind(batch.list_batches, runtime))

    router.get("/audit", bind(audit.list_audit, runtime))
    router.get("/audit/summary", bind(audit.audit_summary, runtime))
    router.get("/audit/latest", bind(audit.audit_latest, runtime))
    router.get("/audit/action", bind(audit.audit_by_action, runtime))

    def route_table(request: Request) -> Response:
        return Response.json(200, {"count": len(router), "routes": router.describe()})

    router.get("/routes", route_table)
    return router
