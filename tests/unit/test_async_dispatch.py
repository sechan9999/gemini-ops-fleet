"""The event stream and the outbox drain that makes the fleet asynchronous."""

from __future__ import annotations

from app import tools, worker
from app.domain import Activity
from app.store import emit, session_scope


def _record(collected: list[tuple[str, str]]):
    def handler(agent_name: str, kind: str, payload: dict) -> str:
        collected.append((kind, agent_name))
        return "ok"

    return handler


def test_events_route_to_the_owning_agent():
    with session_scope() as session:
        emit(session, "as.opened", {"ticket_id": 1}, actor="system")
        emit(session, "delivery.done", {"order_id": 1}, actor="system")
        emit(session, "transaction.posted", {"order_id": 1}, actor="system")

    seen: list[tuple[str, str]] = []
    result = worker.drain_once(handler=_record(seen))

    assert result.dispatched == 3
    assert dict(seen) == {
        "as.opened": "triage_agent",
        "delivery.done": "followup_agent",
        "transaction.posted": "reconcile_agent",
    }


def test_draining_twice_does_not_reprocess():
    with session_scope() as session:
        emit(session, "as.opened", {"ticket_id": 1}, actor="system")

    seen: list[tuple[str, str]] = []
    worker.drain_once(handler=_record(seen))
    second = worker.drain_once(handler=_record(seen))

    assert len(seen) == 1
    assert second.dispatched == 0


def test_a_failing_handler_leaves_the_event_for_retry():
    with session_scope() as session:
        emit(session, "as.opened", {"ticket_id": 1}, actor="system")

    def explode(agent_name: str, kind: str, payload: dict) -> str:
        raise RuntimeError("model unavailable")

    result = worker.drain_once(handler=explode)
    assert result.dispatched == 0
    assert worker.pending_count() == 1

    seen: list[tuple[str, str]] = []
    worker.drain_once(handler=_record(seen))
    assert seen == [("as.opened", "triage_agent")]


def test_unrouted_events_do_not_block_the_queue():
    with session_scope() as session:
        emit(session, "something.unknown", {}, actor="system")
        emit(session, "as.opened", {"ticket_id": 1}, actor="system")

    seen: list[tuple[str, str]] = []
    result = worker.drain_once(handler=_record(seen))

    assert result.skipped == 1
    assert result.dispatched == 1
    assert worker.pending_count() == 0


def test_a_tool_call_writes_to_the_stream(context_for):
    tools.triage_ticket(1, "quality", "high", context_for("tok-support"))

    with session_scope() as session:
        kinds = [a.kind for a in session.query(Activity).all()]
    assert "as.triaged" in kinds


def test_triage_assigns_by_load_not_by_the_model(context_for):
    result = tools.triage_ticket(1, "quality", "high", context_for("tok-support"))

    assert result["status"] == "success"
    # The only support employee in the seed data, chosen deterministically.
    assert result["assignee"] == "Min (support)"


def test_triage_rejects_an_unknown_severity(context_for):
    result = tools.triage_ticket(1, "quality", "catastrophic", context_for("tok-support"))
    assert result["status"] == "error"
