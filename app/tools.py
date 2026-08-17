"""Tools the fleet can call.

Every tool follows the same shape: resolve the verified caller, check whether
that caller may run this tool at all, do the work inside the caller's scope, and
record the outcome in the audit trail. Denials return a normal result dict
rather than raising, so the model can explain the refusal instead of crashing --
but the refusal is decided in Python, not by the model.

No tool here sends a message to a customer or moves money. The furthest an agent
can go is to draft something and park it for a human.
"""

from __future__ import annotations

from google.adk.tools import ToolContext
from sqlalchemy import func, select

from app import retrieval
from app.domain import (
    Approval,
    Document,
    Employee,
    Order,
    Role,
    Ticket,
    Transaction,
)
from app.identity import (
    AccessDenied,
    assert_can_view_customer,
    audit,
    caller,
    check_tool_access,
    visible_customers,
)
from app.store import emit, session_scope


def _denied(reason: str) -> dict:
    return {"status": "denied", "reason": reason, "results": []}


# --- Retrieval -------------------------------------------------------------


def search_knowledge(query: str, limit: int, tool_context: ToolContext) -> dict:
    """Search company knowledge and policy documents.

    Only documents the calling employee's department is cleared for are
    searched. Documents outside that scope are not returned and are not
    summarised.

    Args:
        query: What to look for, in natural language.
        limit: Maximum number of documents to return.

    Returns:
        dict with 'status' and 'results', each result carrying a title, a
        snippet, and the document id to cite.
    """
    try:
        who = caller(tool_context)
        check_tool_access(who, "search_knowledge")
    except AccessDenied as exc:
        return _denied(str(exc))

    with session_scope() as s:
        hits = retrieval.search(s, who.role, query, limit)
        audit(
            s,
            who,
            "search_knowledge",
            "allowed",
            f"query={query!r} hits={len(hits)}",
        )
        return {
            "status": "success",
            "scoped_to_role": who.role.value,
            "results": [
                {
                    "document_id": h.document_id,
                    "title": h.title,
                    "snippet": h.snippet,
                    "score": h.score,
                }
                for h in hits
            ],
        }


# --- Customer & pipeline ---------------------------------------------------


def get_customer_360(customer_id: int, tool_context: ToolContext) -> dict:
    """Return a consolidated view of one customer.

    Covers the customer record, their orders, and their tickets. Sales staff can
    only reach customers they own.

    Args:
        customer_id: The customer to look up.

    Returns:
        dict with 'status' and, when permitted, 'customer', 'orders', 'tickets'.
    """
    try:
        who = caller(tool_context)
        check_tool_access(who, "get_customer_360")
    except AccessDenied as exc:
        return _denied(str(exc))

    with session_scope() as s:
        try:
            customer = assert_can_view_customer(s, who, customer_id)
        except AccessDenied as exc:
            audit(s, who, "get_customer_360", "denied", str(exc))
            return _denied(str(exc))

        orders = s.scalars(
            select(Order).where(Order.customer_id == customer_id)
        ).all()
        tickets = s.scalars(
            select(Ticket).where(Ticket.customer_id == customer_id)
        ).all()
        audit(s, who, "get_customer_360", "allowed", f"customer={customer_id}")

        return {
            "status": "success",
            "customer": {"id": customer.id, "name": customer.name},
            "orders": [
                {
                    "id": o.id,
                    "product": o.product,
                    "amount": o.amount,
                    "status": o.status,
                }
                for o in orders
            ],
            "tickets": [
                {"id": t.id, "subject": t.subject, "status": t.status}
                for t in tickets
            ],
        }


def list_open_tickets(tool_context: ToolContext) -> dict:
    """List tickets that are still open, within the caller's scope.

    Returns:
        dict with 'status' and 'results'.
    """
    try:
        who = caller(tool_context)
        check_tool_access(who, "list_open_tickets")
    except AccessDenied as exc:
        return _denied(str(exc))

    with session_scope() as s:
        scoped_ids = [c.id for c in s.scalars(visible_customers(s, who)).all()]
        tickets = s.scalars(
            select(Ticket)
            .where(Ticket.status == "open")
            .where(Ticket.customer_id.in_(scoped_ids))
        ).all()
        audit(s, who, "list_open_tickets", "allowed", f"count={len(tickets)}")
        return {
            "status": "success",
            "results": [
                {
                    "id": t.id,
                    "subject": t.subject,
                    "category": t.category,
                    "severity": t.severity,
                    "assignee_id": t.assignee_id,
                }
                for t in tickets
            ],
        }


def get_pipeline(tool_context: ToolContext) -> dict:
    """Summarise orders by status and value, within the caller's scope.

    Returns:
        dict with 'status' and 'results', one entry per order status.
    """
    try:
        who = caller(tool_context)
        check_tool_access(who, "get_pipeline")
    except AccessDenied as exc:
        return _denied(str(exc))

    with session_scope() as s:
        scoped_ids = [c.id for c in s.scalars(visible_customers(s, who)).all()]
        rows = s.execute(
            select(Order.status, func.count(Order.id), func.sum(Order.amount))
            .where(Order.customer_id.in_(scoped_ids))
            .group_by(Order.status)
        ).all()
        audit(s, who, "get_pipeline", "allowed", f"buckets={len(rows)}")
        return {
            "status": "success",
            "results": [
                {"status": status, "count": count, "value": float(total or 0)}
                for status, count, total in rows
            ],
        }


# --- Accounting ------------------------------------------------------------


def reconcile_accounting(tool_context: ToolContext) -> dict:
    """Check that every order has matching ledger entries.

    Restricted to accounting and management. Reports discrepancies; it does not
    create, alter, or reverse any financial record.

    Returns:
        dict with 'status', 'integrity_rate', 'missing', and 'mismatched'.
    """
    try:
        who = caller(tool_context)
        check_tool_access(who, "reconcile_accounting")
    except AccessDenied as exc:
        with session_scope() as s:
            # Record the refusal. A denial is evidence that the boundary held,
            # so it belongs in the trail as much as a success does.
            try:
                audit(s, caller(tool_context), "reconcile_accounting", "denied", str(exc))
            except AccessDenied:
                pass
        return _denied(str(exc))

    with session_scope() as s:
        orders = s.scalars(select(Order)).all()
        missing: list[int] = []
        mismatched: list[dict] = []

        for order in orders:
            entries = s.scalars(
                select(Transaction).where(Transaction.order_id == order.id)
            ).all()
            if not entries:
                if order.status != "cancelled":
                    missing.append(order.id)
                continue
            net = sum(e.amount if e.kind == "sale" else -e.amount for e in entries)
            expected = 0.0 if order.status == "cancelled" else order.amount
            if abs(net - expected) > 0.01:
                mismatched.append(
                    {"order_id": order.id, "expected": expected, "ledger": net}
                )

        total = len(orders) or 1
        clean = total - len(missing) - len(mismatched)
        rate = round(clean / total, 4)
        audit(s, who, "reconcile_accounting", "allowed", f"integrity={rate}")

        return {
            "status": "success",
            "integrity_rate": rate,
            "missing": missing,
            "mismatched": mismatched,
        }


# --- Ticket handling -------------------------------------------------------


def triage_ticket(
    ticket_id: int, category: str, severity: str, tool_context: ToolContext
) -> dict:
    """Record a category and severity for a ticket and assign an owner.

    The owner is chosen deterministically as the least-loaded support employee,
    not by the model. Does not modify the customer's original message.

    Args:
        ticket_id: The ticket to triage.
        category: One of 'quality', 'delivery', 'billing', 'other'.
        severity: One of 'low', 'medium', 'high'.

    Returns:
        dict with 'status', 'assignee', and the recorded classification.
    """
    try:
        who = caller(tool_context)
        check_tool_access(who, "triage_ticket")
    except AccessDenied as exc:
        return _denied(str(exc))

    if severity not in {"low", "medium", "high"}:
        return {"status": "error", "reason": f"unknown severity {severity!r}"}

    with session_scope() as s:
        ticket = s.get(Ticket, ticket_id)
        if ticket is None:
            return {"status": "error", "reason": f"no ticket {ticket_id}"}

        assignee = s.scalars(
            select(Employee)
            .where(Employee.role == Role.SUPPORT)
            .order_by(Employee.open_load.asc())
        ).first()

        ticket.category = category
        ticket.severity = severity
        if assignee is not None:
            ticket.assignee_id = assignee.id
            assignee.open_load += 1

        emit(
            s,
            "as.triaged",
            {"ticket_id": ticket_id, "category": category, "severity": severity},
            actor=who.name,
        )
        audit(s, who, "triage_ticket", "allowed", f"ticket={ticket_id}")

        return {
            "status": "success",
            "ticket_id": ticket_id,
            "category": category,
            "severity": severity,
            "assignee": assignee.name if assignee else None,
        }


def capture_knowledge(
    ticket_id: int, title: str, body: str, tool_context: ToolContext
) -> dict:
    """Turn a resolved ticket into a searchable knowledge document.

    The new document inherits the department scope of the caller, so capturing
    knowledge cannot widen who can see it.

    Args:
        ticket_id: The resolved ticket this knowledge came from.
        title: Short title for the knowledge document.
        body: The reusable explanation, written for a colleague.

    Returns:
        dict with 'status' and the new 'document_id'.
    """
    try:
        who = caller(tool_context)
        check_tool_access(who, "capture_knowledge")
    except AccessDenied as exc:
        return _denied(str(exc))

    with session_scope() as s:
        ticket = s.get(Ticket, ticket_id)
        if ticket is None:
            return {"status": "error", "reason": f"no ticket {ticket_id}"}

        roles = sorted({who.role.value, Role.SUPPORT.value, Role.MANAGER.value})
        doc = Document(
            title=title,
            body=body,
            allowed_roles=Document.encode_roles(roles),
            source="captured",
        )
        s.add(doc)
        s.flush()

        emit(
            s,
            "knowledge.captured",
            {"document_id": doc.id, "ticket_id": ticket_id},
            actor=who.name,
        )
        audit(s, who, "capture_knowledge", "allowed", f"doc={doc.id}")
        return {"status": "success", "document_id": doc.id, "visible_to": roles}


# --- Customer follow-up (draft only) ---------------------------------------


def draft_followup(customer_id: int, message: str, tool_context: ToolContext) -> dict:
    """Queue a draft message to a customer for human approval.

    This does not send anything. The draft waits in the approval queue until a
    person approves it; only then can it leave the system.

    Args:
        customer_id: Who the message is addressed to.
        message: The proposed message body.

    Returns:
        dict with 'status', the 'approval_id', and 'sent' which is always false.
    """
    try:
        who = caller(tool_context)
        check_tool_access(who, "draft_followup")
    except AccessDenied as exc:
        return _denied(str(exc))

    with session_scope() as s:
        try:
            assert_can_view_customer(s, who, customer_id)
        except AccessDenied as exc:
            audit(s, who, "draft_followup", "denied", str(exc))
            return _denied(str(exc))

        approval = Approval(customer_id=customer_id, draft=message)
        s.add(approval)
        s.flush()
        emit(
            s,
            "followup.drafted",
            {"approval_id": approval.id, "customer_id": customer_id},
            actor=who.name,
        )
        audit(s, who, "draft_followup", "allowed", f"approval={approval.id}")
        return {
            "status": "queued_for_approval",
            "approval_id": approval.id,
            "sent": False,
            "note": "A human must approve this before it can be sent.",
        }


FLEET_TOOLS = [
    search_knowledge,
    get_customer_360,
    list_open_tickets,
    get_pipeline,
    reconcile_accounting,
    triage_ticket,
    capture_knowledge,
    draft_followup,
]
