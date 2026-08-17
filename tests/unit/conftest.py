"""Test fixtures.

Every test runs against a throwaway SQLite file with freshly seeded demo data,
so no test can depend on another's leftovers and none of them need credentials.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolated_database() -> None:
    tmpdir = tempfile.mkdtemp(prefix="fleet-tests-")
    os.environ["DATABASE_URL"] = f"sqlite:///{tmpdir}/fleet.db"
    os.environ.pop("MODEL_ARMOR_TEMPLATE_ID", None)
    os.environ.pop("FLEET_DEV_TOKEN", None)

    from app.config import get_settings

    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_data(_isolated_database: None) -> None:
    from app import store

    store.reset_and_seed()


@dataclass
class FakeToolContext:
    """Stand-in for ADK's ToolContext.

    The tools only ever touch `.state`, and using a real ToolContext would drag
    an invocation and a runner into unit tests that are about authorisation.
    """

    state: dict


@pytest.fixture
def context_for():
    """Build a tool context carrying a given employee's verified identity."""
    from app.identity import IDENTITY_KEY, Identity, to_state
    from app.store import employee_by_token, session_scope

    def _make(token: str) -> FakeToolContext:
        with session_scope() as session:
            employee = employee_by_token(session, token)
            assert employee is not None, f"no employee for token {token}"
            identity = Identity(
                employee_id=employee.id, name=employee.name, role=employee.role
            )
        return FakeToolContext(state={IDENTITY_KEY: to_state(identity)})

    return _make
