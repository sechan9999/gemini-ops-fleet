"""Domain model.

One `Activity` stream is the spine of the system: every meaningful thing that
happens in the company lands there as an event, and the agents are consumers of
that stream rather than of a chat box. Everything else in this module is either
a source of those events or a projection of them.

`Activity.dispatched` makes the stream a transactional outbox — a row is written
in the same transaction as the business change, and a separate worker drains it.
That is what lets agents run in the background without a second commit path.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Base(DeclarativeBase):
    pass


class Role(enum.StrEnum):
    """Departments, used as the unit of access control.

    Roles are assigned to employees in the database and resolved server-side.
    Nothing the model produces can change a caller's role.
    """

    SALES = "sales"
    SUPPORT = "support"
    ACCOUNTING = "accounting"
    MANAGER = "manager"


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    role: Mapped[Role] = mapped_column(Enum(Role))
    api_token: Mapped[str] = mapped_column(String(64), unique=True)
    open_load: Mapped[int] = mapped_column(Integer, default=0)

    customers: Mapped[list[Customer]] = relationship(back_populates="owner")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(40))
    # Row-level ownership. A sales rep sees their own customers and no others.
    owner_employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))

    owner: Mapped[Employee | None] = relationship(back_populates="customers")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    product: Mapped[str] = mapped_column(String(120))
    amount: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default="placed")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class Transaction(Base):
    """Accounting ledger entry mirroring an order."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    kind: Mapped[str] = mapped_column(String(16))  # sale | refund
    amount: Mapped[float] = mapped_column(Float)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(40))
    severity: Mapped[str | None] = mapped_column(String(16))
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    status: Mapped[str] = mapped_column(String(16), default="open")
    resolution: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class Document(Base):
    """A retrievable document, scoped to the roles allowed to see it.

    `allowed_roles` is the whole access-control story for retrieval. It is
    applied as a SQL predicate before results exist, not as an instruction to
    the model after the fact.

    Stored as a comma-delimited string with sentinel commas on both ends
    (``",sales,support,"``) so that a single portable ``LIKE '%,sales,%'``
    predicate does the filtering on both SQLite and Postgres. A JSON column
    would have pushed the check into Python on SQLite, which would defeat the
    point.
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    allowed_roles: Mapped[str] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(40), default="authored")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    @staticmethod
    def encode_roles(roles: list[str]) -> str:
        """Wrap roles in sentinel commas so LIKE cannot match a partial name."""
        return "," + ",".join(roles) + ","


class Approval(Base):
    """A drafted outbound message waiting on a human.

    The follow-up agent writes rows here. Only an explicit human action flips
    `approved`, and only an approved row can be sent. There is no code path from
    an agent to a customer that does not pass through this table.
    """

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    draft: Mapped[str] = mapped_column(Text)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[str | None] = mapped_column(String(80))
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class Activity(Base):
    """The event stream, doubling as a transactional outbox."""

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(80), default="system")
    dispatched: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class AuditEntry(Base):
    """Every tool call an agent makes, including the ones that were denied.

    Denials are the interesting rows: they are the evidence that access control
    ran, so they are recorded with the same weight as successes.
    """

    __tablename__ = "audit_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(24))
    tool: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16))  # allowed | denied | blocked
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
