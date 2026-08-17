"""Access control: the claims the demo makes, asserted in code.

These are the tests that matter most. If any of them fails, the pitch is false.
"""

from __future__ import annotations

import pytest

from app import tools
from app.domain import Role
from app.identity import AccessDenied, Identity, identity_from_state


def test_sales_cannot_retrieve_an_accounting_document(context_for):
    """The headline claim: scope is enforced in the query, not the prompt."""
    sales = context_for("tok-sales")
    result = tools.search_knowledge("margin reconciliation memo", 5, sales)

    assert result["status"] == "success"
    titles = [hit["title"] for hit in result["results"]]
    assert "Q3 margin reconciliation memo" not in titles


def test_accounting_can_retrieve_the_same_document(context_for):
    """The counterpart: the document exists and is findable by the right role."""
    accounting = context_for("tok-accounting")
    result = tools.search_knowledge("margin reconciliation memo", 5, accounting)

    titles = [hit["title"] for hit in result["results"]]
    assert "Q3 margin reconciliation memo" in titles


def test_shared_policy_is_visible_to_everyone(context_for):
    for token in ("tok-sales", "tok-support", "tok-accounting", "tok-manager"):
        result = tools.search_knowledge("refund within 14 days", 5, context_for(token))
        titles = [hit["title"] for hit in result["results"]]
        assert "Refund policy" in titles, f"{token} could not see the refund policy"


def test_sales_cannot_open_another_reps_customer(context_for):
    """Bolt Fabrication is owned by the manager, not by Jin."""
    sales = context_for("tok-sales")
    result = tools.get_customer_360(2, sales)

    assert result["status"] == "denied"
    assert "scope" in result["reason"]


def test_sales_can_open_their_own_customer(context_for):
    sales = context_for("tok-sales")
    result = tools.get_customer_360(1, sales)

    assert result["status"] == "success"
    assert result["customer"]["name"] == "Acme Machining"


def test_reconciliation_is_refused_for_sales(context_for):
    result = tools.reconcile_accounting(context_for("tok-sales"))

    assert result["status"] == "denied"
    assert "reconcile_accounting" in result["reason"]


def test_reconciliation_runs_for_accounting(context_for):
    result = tools.reconcile_accounting(context_for("tok-accounting"))

    assert result["status"] == "success"
    assert result["integrity_rate"] == 1.0
    assert result["missing"] == []
    assert result["mismatched"] == []


def test_a_session_without_an_identity_fails_closed():
    """No identity is not "everyone" -- it is nobody."""
    with pytest.raises(AccessDenied):
        identity_from_state({})


def test_identity_cannot_be_supplied_as_a_tool_argument():
    """Tools take no role parameter, so a model cannot assert one.

    Guards against a future signature change quietly reopening the door.
    """
    import inspect

    for tool in tools.FLEET_TOOLS:
        params = set(inspect.signature(tool).parameters)
        assert not params & {"role", "identity", "employee_id", "as_role"}, (
            f"{tool.__name__} accepts a caller-supplied identity"
        )


def test_manager_sees_the_whole_customer_book(context_for):
    result = tools.get_pipeline(context_for("tok-manager"))
    assert result["status"] == "success"

    sales_view = tools.get_pipeline(context_for("tok-sales"))
    manager_total = sum(row["count"] for row in result["results"])
    sales_total = sum(row["count"] for row in sales_view["results"])
    assert manager_total >= sales_total


def test_identity_round_trips_through_state(context_for):
    ctx = context_for("tok-support")
    identity = identity_from_state(ctx.state)
    assert isinstance(identity, Identity)
    assert identity.role is Role.SUPPORT
