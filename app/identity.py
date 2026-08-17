"""Caller identity and what each role is allowed to reach.

The rule this module exists to enforce: a caller's role is derived from an
authenticated principal on the server, never read from a tool argument and never
inferred from anything the model wrote. An agent that is asked nicely to "act as
the accounting team" still calls tools as whoever the session actually belongs
to.
"""

from __future__ import annotations

from dataclasses import dataclass

from google.adk.tools import ToolContext
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import AuditEntry, Customer, Role

# Session-state key holding the verified caller. Written once by the server when
# the session is created; tools only ever read it.
IDENTITY_KEY = "fleet:identity"


@dataclass(frozen=True)
class Identity:
    employee_id: int
    name: str
    role: Role

    @property
    def is_manager(self) -> bool:
        return self.role is Role.MANAGER


class AccessDenied(Exception):
    """Raised when a caller reaches for something outside their scope."""


def identity_from_state(state: dict) -> Identity:
    raw = state.get(IDENTITY_KEY)
    if not raw:
        raise AccessDenied("no verified identity on this session")
    return Identity(
        employee_id=int(raw["employee_id"]),
        name=str(raw["name"]),
        role=Role(raw["role"]),
    )


def caller(tool_context: ToolContext) -> Identity:
    return identity_from_state(tool_context.state)


def to_state(identity: Identity) -> dict:
    return {
        "employee_id": identity.employee_id,
        "name": identity.name,
        "role": identity.role.value,
    }


# --- Authorisation rules ---------------------------------------------------

#: Tools only these roles may call at all.
TOOL_ROLES: dict[str, set[Role]] = {
    "reconcile_accounting": {Role.ACCOUNTING, Role.MANAGER},
}


def check_tool_access(identity: Identity, tool_name: str) -> None:
    allowed = TOOL_ROLES.get(tool_name)
    if allowed is not None and identity.role not in allowed:
        raise AccessDenied(
            f"{identity.role.value} may not call {tool_name}"
        )


def visible_customers(session: Session, identity: Identity):
    """Rows of `Customer` this caller may see.

    Managers and accounting see the whole book; support sees everyone because
    tickets can arrive from any customer; sales sees only their own accounts.
    """
    stmt = select(Customer)
    if identity.role is Role.SALES:
        stmt = stmt.where(Customer.owner_employee_id == identity.employee_id)
    return stmt


def assert_can_view_customer(
    session: Session, identity: Identity, customer_id: int
) -> Customer:
    customer = session.scalar(
        visible_customers(session, identity).where(Customer.id == customer_id)
    )
    if customer is None:
        raise AccessDenied(f"customer {customer_id} is not in your scope")
    return customer


def audit(
    session: Session, identity: Identity, tool: str, outcome: str, detail: str
) -> None:
    session.add(
        AuditEntry(
            actor=identity.name,
            role=identity.role.value,
            tool=tool,
            outcome=outcome,
            detail=detail,
        )
    )
