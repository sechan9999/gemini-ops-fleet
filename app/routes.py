"""HTTP surface for the fleet.

Three things live here that deliberately do not live in the agent's tool set:

- the **approval queue**, where a human clears or refuses a drafted message;
- the **registry**, which other departments read to discover agents;
- the **audit trail**, including the calls that were refused.

Callers authenticate with an employee token and their role is looked up from
that token. Nothing accepts a role as a parameter, here or anywhere else.
"""

from __future__ import annotations

import base64
import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select

from app import approvals, registry, worker
from app.domain import Activity, AuditEntry
from app.identity import Identity
from app.store import emit, employee_by_token, session_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fleet", tags=["fleet"])


def current_employee(authorization: str = Header(default="")) -> Identity:
    """Resolve the caller from a bearer token.

    The server's half of the identity contract: the role attached to a request
    is looked up here and passed down, so no downstream code has to trust a
    value that arrived in the payload.
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing employee token")

    with session_scope() as session:
        employee = employee_by_token(session, token)
        if employee is None:
            raise HTTPException(status_code=401, detail="unknown employee token")
        return Identity(
            employee_id=employee.id, name=employee.name, role=employee.role
        )


# --- Discovery -------------------------------------------------------------


@router.get("/registry")
def get_registry(department: str | None = None, tag: str | None = None) -> dict:
    """The agent catalogue, optionally filtered by department or capability."""
    entries = (
        registry.discover(department, tag)
        if (department or tag)
        else registry.catalog()
    )
    return {"count": len(entries), "agents": entries}


# --- Human gate ------------------------------------------------------------


@router.get("/approvals")
def list_approvals(who: Identity = Depends(current_employee)) -> dict:
    with session_scope() as session:
        queue = approvals.pending(session)
        return {
            "count": len(queue),
            "approvals": [
                {
                    "id": a.id,
                    "customer_id": a.customer_id,
                    "draft": a.draft,
                    "approved": a.approved,
                    "sent": a.sent,
                }
                for a in queue
            ],
        }


@router.post("/approvals/{approval_id}/approve")
def approve(approval_id: int, who: Identity = Depends(current_employee)) -> dict:
    with session_scope() as session:
        try:
            approval = approvals.approve(session, approval_id, who)
        except approvals.NotApproved as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "status": "approved",
            "approval_id": approval.id,
            "approved_by": who.name,
        }


@router.post("/approvals/{approval_id}/send")
def send(approval_id: int, who: Identity = Depends(current_employee)) -> dict:
    """Deliver an approved draft.

    Refuses anything without a recorded human sign-off, and says so with a 409
    rather than a generic error, because that refusal is the feature.
    """
    with session_scope() as session:
        try:
            return approvals.send(session, approval_id)
        except approvals.NotApproved as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


# --- Audit -----------------------------------------------------------------


@router.get("/audit")
def audit_trail(limit: int = 50, who: Identity = Depends(current_employee)) -> dict:
    with session_scope() as session:
        entries = session.scalars(
            select(AuditEntry).order_by(AuditEntry.id.desc()).limit(limit)
        ).all()
        return {
            "count": len(entries),
            "entries": [
                {
                    "id": e.id,
                    "actor": e.actor,
                    "role": e.role,
                    "tool": e.tool,
                    "outcome": e.outcome,
                    "detail": e.detail,
                    "at": e.created_at.isoformat(),
                }
                for e in entries
            ],
        }


# --- Event stream ----------------------------------------------------------


@router.get("/events")
def list_events(limit: int = 50, who: Identity = Depends(current_employee)) -> dict:
    with session_scope() as session:
        rows = session.scalars(
            select(Activity).order_by(Activity.id.desc()).limit(limit)
        ).all()
        return {
            "pending": sum(1 for r in rows if not r.dispatched),
            "events": [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "actor": r.actor,
                    "dispatched": r.dispatched,
                    "payload": r.payload,
                }
                for r in rows
            ],
        }


@router.post("/events/drain")
def drain(who: Identity = Depends(current_employee)) -> dict:
    """Run the outbox drain once, on demand.

    The same routine Pub/Sub triggers. Exposed so the asynchronous path can be
    demonstrated without waiting for a push delivery.
    """
    result = worker.drain_once()
    return {
        "dispatched": result.dispatched,
        "skipped": result.skipped,
        "details": result.details,
    }


@router.post("/trigger/pubsub")
async def pubsub_push(envelope: dict) -> dict:
    """Pub/Sub push endpoint.

    Accepts the standard push envelope, appends the event to the stream, and
    drains. Unparseable messages are acknowledged rather than retried -- a
    malformed payload will not become valid on the fourth delivery, and letting
    Pub/Sub redeliver it forever buries the events behind it.
    """
    message = envelope.get("message") or {}
    raw = message.get("data")
    if not raw:
        logger.warning("pubsub push with no data; acknowledging")
        return {"status": "ignored", "reason": "empty message"}

    try:
        decoded = json.loads(base64.b64decode(raw).decode("utf-8"))
        kind = decoded["kind"]
        payload = decoded.get("payload", {})
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("undecodable pubsub message: %s", exc)
        return {"status": "ignored", "reason": "undecodable message"}

    with session_scope() as session:
        emit(session, kind, payload, actor="pubsub")

    result = worker.drain_once()
    return {
        "status": "accepted",
        "kind": kind,
        "dispatched": result.dispatched,
        "details": result.details,
    }
