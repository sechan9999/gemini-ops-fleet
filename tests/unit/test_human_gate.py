"""The human gate: an agent can draft, only a person can send."""

from __future__ import annotations

import pytest

from app import approvals, tools
from app.domain import Approval
from app.identity import Identity, identity_from_state
from app.store import session_scope


def test_drafting_queues_without_sending(context_for):
    sales = context_for("tok-sales")
    result = tools.draft_followup(1, "Your brackets shipped Tuesday.", sales)

    assert result["status"] == "queued_for_approval"
    assert result["sent"] is False

    with session_scope() as session:
        approval = session.get(Approval, result["approval_id"])
        assert approval is not None
        assert approval.approved is False
        assert approval.sent is False


def test_sending_without_approval_is_refused(context_for):
    sales = context_for("tok-sales")
    queued = tools.draft_followup(1, "Anything at all.", sales)

    with session_scope() as session:
        with pytest.raises(approvals.NotApproved, match="human sign-off"):
            approvals.send(session, queued["approval_id"])


def test_sending_after_approval_succeeds(context_for):
    ctx = context_for("tok-sales")
    identity: Identity = identity_from_state(ctx.state)
    queued = tools.draft_followup(1, "Your brackets shipped Tuesday.", ctx)

    with session_scope() as session:
        approvals.approve(session, queued["approval_id"], identity)

    with session_scope() as session:
        outcome = approvals.send(session, queued["approval_id"])

    assert outcome["status"] == "sent"
    assert outcome["approved_by"] == identity.name


def test_an_agent_cannot_reach_the_send_path():
    """No tool exposes approval or sending.

    The gate is only a gate if the agent has no key to it.
    """
    tool_names = {tool.__name__ for tool in tools.FLEET_TOOLS}
    assert not tool_names & {"approve", "send", "approve_and_send"}
    assert not any("send" in name for name in tool_names)


def test_drafting_for_someone_elses_customer_is_refused(context_for):
    result = tools.draft_followup(2, "Hello there.", context_for("tok-sales"))
    assert result["status"] == "denied"

    with session_scope() as session:
        assert approvals.pending(session) == []
