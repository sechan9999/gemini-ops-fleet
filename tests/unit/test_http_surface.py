"""The HTTP governance surface.

Mounts the fleet router on a bare FastAPI app rather than importing
`app.fast_api_app`, which pulls in Cloud Logging and telemetry at import time.
These tests are about routing and authorisation, so they should not need
credentials to run.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import tools
from app.routes import router


@pytest.fixture
def client() -> TestClient:
    api = FastAPI()
    api.include_router(router)
    return TestClient(api)


def auth(token: str) -> dict:
    """The header the deployed service uses.

    Cloud Run's IAM layer claims Authorization, so the employee token travels
    in its own header.
    """
    return {"X-Fleet-Token": token}


def legacy_auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- Authentication --------------------------------------------------------


def test_protected_routes_reject_a_missing_token(client: TestClient):
    assert client.get("/fleet/approvals").status_code == 401
    assert client.get("/fleet/audit").status_code == 401


def test_protected_routes_reject_an_unknown_token(client: TestClient):
    response = client.get("/fleet/approvals", headers=auth("tok-nonexistent"))
    assert response.status_code == 401


def test_the_authorization_header_still_works(client: TestClient):
    """Kept as a fallback for local runs and unauthenticated ingress."""
    response = client.get("/fleet/approvals", headers=legacy_auth("tok-manager"))
    assert response.status_code == 200


# --- Discovery -------------------------------------------------------------


def test_registry_lists_every_agent(client: TestClient):
    body = client.get("/fleet/registry").json()
    assert body["count"] == 4
    names = {a["name"] for a in body["agents"]}
    assert "reconcile_agent" in names


def test_registry_filters_by_department(client: TestClient):
    body = client.get("/fleet/registry", params={"department": "accounting"}).json()
    assert [a["name"] for a in body["agents"]] == ["reconcile_agent"]


# --- Human gate ------------------------------------------------------------


def test_the_send_endpoint_refuses_an_unapproved_draft(client: TestClient, context_for):
    queued = tools.draft_followup(1, "Draft text.", context_for("tok-sales"))
    approval_id = queued["approval_id"]

    response = client.post(f"/fleet/approvals/{approval_id}/send", headers=auth("tok-sales"))

    assert response.status_code == 409
    assert "human sign-off" in response.json()["detail"]


def test_approve_then_send_succeeds(client: TestClient, context_for):
    queued = tools.draft_followup(1, "Draft text.", context_for("tok-sales"))
    approval_id = queued["approval_id"]

    approved = client.post(
        f"/fleet/approvals/{approval_id}/approve", headers=auth("tok-manager")
    )
    assert approved.status_code == 200
    assert approved.json()["approved_by"] == "Han (manager)"

    sent = client.post(
        f"/fleet/approvals/{approval_id}/send", headers=auth("tok-manager")
    )
    assert sent.status_code == 200
    assert sent.json()["status"] == "sent"


def test_the_queue_reports_pending_drafts(client: TestClient, context_for):
    tools.draft_followup(1, "First.", context_for("tok-sales"))
    body = client.get("/fleet/approvals", headers=auth("tok-support")).json()
    assert body["count"] == 1
    assert body["approvals"][0]["sent"] is False


# --- Audit -----------------------------------------------------------------


def test_audit_endpoint_surfaces_denials(client: TestClient, context_for):
    tools.get_customer_360(2, context_for("tok-sales"))  # another rep's customer

    body = client.get("/fleet/audit", headers=auth("tok-manager")).json()
    outcomes = {e["outcome"] for e in body["entries"]}
    assert "denied" in outcomes


# --- Pub/Sub ---------------------------------------------------------------


def _envelope(kind: str, payload: dict) -> dict:
    data = base64.b64encode(
        json.dumps({"kind": kind, "payload": payload}).encode()
    ).decode()
    return {"message": {"data": data, "messageId": "1"}}


def test_pubsub_push_routes_the_event(client: TestClient):
    response = client.post(
        "/fleet/trigger/pubsub", json=_envelope("as.opened", {"ticket_id": 1})
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "accepted"
    assert body["dispatched"] == 1
    assert body["details"][0]["routed_to"] == "triage_agent"


def test_pubsub_push_acknowledges_an_undecodable_message(client: TestClient):
    """A malformed payload is dropped, not retried forever."""
    response = client.post(
        "/fleet/trigger/pubsub",
        json={"message": {"data": base64.b64encode(b"not json").decode()}},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_pubsub_push_acknowledges_an_empty_message(client: TestClient):
    response = client.post("/fleet/trigger/pubsub", json={"message": {}})
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_manual_drain_endpoint(client: TestClient, context_for):
    tools.triage_ticket(1, "quality", "high", context_for("tok-support"))

    body = client.post("/fleet/events/drain", headers=auth("tok-manager")).json()
    # as.triaged has no owning agent, so it is skipped rather than dispatched.
    assert body["skipped"] + body["dispatched"] >= 1
