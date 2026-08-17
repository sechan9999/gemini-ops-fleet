"""The human gate.

Deliberately not a tool. Nothing in this module is reachable by an agent -- it
is called from the HTTP surface by an authenticated person. That separation is
the guarantee: an agent can fill the queue, and only a human can empty it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import Approval, Customer
from app.identity import Identity
from app.store import emit


class NotApproved(Exception):
    """Raised when something tries to send a draft that no human approved."""


def pending(session: Session) -> list[Approval]:
    return list(
        session.scalars(
            select(Approval).where(Approval.approved.is_(False)).order_by(Approval.id)
        ).all()
    )


def approve(session: Session, approval_id: int, approver: Identity) -> Approval:
    """Record a person's approval of a draft."""
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise NotApproved(f"no approval {approval_id}")
    approval.approved = True
    approval.approved_by = approver.name
    emit(
        session,
        "followup.approved",
        {"approval_id": approval.id},
        actor=approver.name,
    )
    return approval


def send(session: Session, approval_id: int) -> dict:
    """Deliver an approved draft.

    Refuses anything that is not approved. This is the last checkpoint, and it
    re-checks rather than trusting that the caller already did.
    """
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise NotApproved(f"no approval {approval_id}")
    if not approval.approved:
        raise NotApproved(
            f"approval {approval_id} has no human sign-off; refusing to send"
        )

    customer = session.get(Customer, approval.customer_id)
    approval.sent = True
    emit(
        session,
        "followup.sent",
        {
            "approval_id": approval.id,
            "customer_id": approval.customer_id,
            "approved_by": approval.approved_by,
        },
        actor=approval.approved_by or "unknown",
    )
    return {
        "status": "sent",
        "approval_id": approval.id,
        "to": customer.email if customer else None,
        "approved_by": approval.approved_by,
    }
