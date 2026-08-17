"""Database engine, session helper, event emission, and demo seed data."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.domain import (
    Activity,
    Base,
    Customer,
    Document,
    Employee,
    Order,
    Role,
    Ticket,
    Transaction,
)

_engine = None
_Session: sessionmaker[Session] | None = None


def get_engine():
    global _engine, _Session
    if _engine is None:
        url = get_settings().database_url
        kwargs = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
        _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _Session is not None
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    Base.metadata.create_all(get_engine())


def emit(session: Session, kind: str, payload: dict, actor: str) -> Activity:
    """Append an event to the stream inside the caller's transaction.

    Deliberately takes the caller's session rather than opening its own: the
    event and the business change it describes must commit together or not at
    all, otherwise the outbox can promise work that never happened.
    """
    activity = Activity(kind=kind, payload=payload, actor=actor)
    session.add(activity)
    return activity


def reset_and_seed() -> None:
    """Drop, recreate, and populate a small demo company.

    Four employees across four departments, two customers owned by different
    reps, an open ticket, a delivered order, and two documents -- one public to
    the company, one visible only to accounting. That last pair is what makes
    the access-control demo possible.
    """
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with session_scope() as s:
        jin = Employee(name="Jin (sales)", role=Role.SALES, api_token="tok-sales")
        min_ = Employee(name="Min (support)", role=Role.SUPPORT, api_token="tok-support")
        acc = Employee(
            name="Soo (accounting)", role=Role.ACCOUNTING, api_token="tok-accounting"
        )
        boss = Employee(name="Han (manager)", role=Role.MANAGER, api_token="tok-manager")
        s.add_all([jin, min_, acc, boss])
        s.flush()

        acme = Customer(
            name="Acme Machining",
            email="ops@acme.example",
            phone="+82-10-0000-0001",
            owner_employee_id=jin.id,
        )
        bolt = Customer(
            name="Bolt Fabrication",
            email="buy@bolt.example",
            phone="+82-10-0000-0002",
            owner_employee_id=boss.id,
        )
        s.add_all([acme, bolt])
        s.flush()

        order = Order(
            customer_id=acme.id, product="CNC bracket, 500u", amount=12500.0,
            status="delivered",
        )
        s.add(order)
        s.flush()
        s.add(Transaction(order_id=order.id, kind="sale", amount=12500.0))

        s.add(
            Ticket(
                customer_id=acme.id,
                subject="Bracket surface finish is inconsistent",
                body=(
                    "Roughly one in twenty brackets from the last delivery has "
                    "visible tooling marks on the mating face. Production is "
                    "blocked on the affected units."
                ),
            )
        )

        s.add_all(
            [
                Document(
                    title="Refund policy",
                    body=(
                        "Customers may request a refund within 14 days of "
                        "receipt. Refunds are issued to the original payment "
                        "method within 5 business days of approval."
                    ),
                    allowed_roles=Document.encode_roles(
                        ["sales", "support", "accounting", "manager"]
                    ),
                ),
                Document(
                    title="Q3 margin reconciliation memo",
                    body=(
                        "Gross margin on the CNC line fell to 21.4% after the "
                        "tooling rework. Do not share externally or with the "
                        "sales organisation before the quarter closes."
                    ),
                    allowed_roles=Document.encode_roles(["accounting", "manager"]),
                ),
            ]
        )


def employee_by_token(session: Session, token: str) -> Employee | None:
    return session.scalar(select(Employee).where(Employee.api_token == token))
