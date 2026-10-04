"""Batch provenance endpoints.

Batches are recorded by the data plane after processing; the control plane
stamps each one with the exact serving version and answers provenance queries
later, regardless of subsequent rollbacks.
"""

from __future__ import annotations

from maskhub.console.http import Request, Response
from maskhub.console.validation import require_text
from maskhub.errors import InvalidRequest


def record_batch(runtime, request: Request) -> Response:
    payload = request.json_object()
    identifier = require_text(payload, "identifier")
    dataset = require_text(payload, "dataset")
    record_count = payload.get("record_count", 0)
    if not isinstance(record_count, int) or isinstance(record_count, bool) or record_count < 0:
        raise InvalidRequest("record_count must be a non-negative integer")
    batch = runtime.policies.process_batch(
        batch_identifier=identifier,
        dataset=dataset,
        record_count=record_count,
    )
    runtime.journal.append(
        "policy.batch.process",
        identifier,
        f"{batch.dataset} governed_by={batch.version_ref} release={batch.release_id}",
    )
    return Response.json(200, batch.as_dict())


def get_batch(runtime, request: Request) -> Response:
    identifier = request.query_one("id", "") or ""
    if not identifier:
        raise InvalidRequest("id query parameter is required")
    return Response.json(200, runtime.policies.batch(identifier).as_dict())


def list_batches(runtime, request: Request) -> Response:
    dataset = request.query_one("dataset")
    batches = (
        runtime.policies.batches_for_dataset(dataset)
        if dataset
        else runtime.policies.batches.all()
    )
    return Response.json(
        200,
        {"count": len(batches), "batches": [batch.as_dict() for batch in batches]},
    )
