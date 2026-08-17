"""Outbox drain — the part that makes the fleet asynchronous.

Business code writes an event in the same transaction as the change it
describes. This worker picks those events up afterwards and hands them to
whichever agent owns that event type. Nobody is waiting at a prompt while it
happens.

The drain is idempotent: a row is claimed by flipping `dispatched` in its own
transaction, so a crash mid-handler costs at most one redelivery rather than a
duplicate side effect on every restart.

In deployment this is driven by Pub/Sub push into the ADK trigger endpoint. The
same routing table serves both paths, so local behaviour and cloud behaviour do
not drift.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select

from app import tracing
from app.domain import Activity
from app.fleet import EVENT_ROUTES
from app.store import session_scope

logger = logging.getLogger(__name__)

#: Called with (agent_name, event_kind, payload). Returns a short result string.
Handler = Callable[[str, str, dict], str]


@dataclass
class DispatchResult:
    dispatched: int
    skipped: int
    details: list[dict]


def log_only_handler(agent_name: str, kind: str, payload: dict) -> str:
    """Default handler: record the routing decision without calling a model.

    Useful on a laptop and in tests, where the interesting property is that the
    right agent was selected for the right event, not what the model said.
    """
    logger.info("route %s -> %s payload=%s", kind, agent_name, payload)
    return f"routed to {agent_name}"


def drain_once(handler: Handler | None = None, limit: int = 50) -> DispatchResult:
    """Process every pending event, oldest first."""
    handler = handler or log_only_handler
    details: list[dict] = []
    dispatched = 0
    skipped = 0

    with session_scope() as session:
        pending = session.scalars(
            select(Activity)
            .where(Activity.dispatched.is_(False))
            .order_by(Activity.id)
            .limit(limit)
        ).all()
        pending_ids = [(a.id, a.kind, dict(a.payload)) for a in pending]

    for activity_id, kind, payload in pending_ids:
        agent_name = EVENT_ROUTES.get(kind)

        if agent_name is None:
            # Nothing owns this event type. Mark it done so an unrouted event
            # does not block the queue forever, but say so out loud.
            with session_scope() as session:
                row = session.get(Activity, activity_id)
                if row is not None:
                    row.dispatched = True
            skipped += 1
            details.append({"id": activity_id, "kind": kind, "routed_to": None})
            continue

        try:
            # Each dispatch gets its own root span. Background work has no
            # incoming request to hang off, so without this the asynchronous
            # half of the system would be invisible in Cloud Trace.
            with tracing.route_span(kind, agent_name, activity_id):
                outcome = handler(agent_name, kind, payload)
        except Exception:
            # Leave the row unclaimed so the next drain retries it.
            logger.exception("handler failed for activity %s", activity_id)
            details.append(
                {"id": activity_id, "kind": kind, "routed_to": agent_name,
                 "error": True}
            )
            continue

        with session_scope() as session:
            row = session.get(Activity, activity_id)
            if row is not None:
                row.dispatched = True
        dispatched += 1
        details.append(
            {
                "id": activity_id,
                "kind": kind,
                "routed_to": agent_name,
                "outcome": outcome,
            }
        )

    return DispatchResult(dispatched=dispatched, skipped=skipped, details=details)


def pending_count() -> int:
    with session_scope() as session:
        return len(
            session.scalars(
                select(Activity).where(Activity.dispatched.is_(False))
            ).all()
        )


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    result = drain_once()
    print(f"dispatched={result.dispatched} skipped={result.skipped}")
    for item in result.details:
        print(" ", item)
