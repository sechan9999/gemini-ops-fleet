# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Fleet entry point: the root agent and the App that wraps it."""

from __future__ import annotations

import logging
import os

from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App

from app.guardrails import GuardrailPlugin
from app.identity import IDENTITY_KEY, Identity, to_state
from app.store import employee_by_token, init_db, session_scope

logger = logging.getLogger(__name__)


def _route_to_vertex() -> None:
    """Point the GenAI SDK at Vertex AI using the ambient credentials.

    Cloud Run supplies the project through the metadata server and locally it
    comes from ADC, so neither path needs the value hard-coded. Existing values
    win, which keeps the offline test run free of any cloud dependency.
    """
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")
    if os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return
    try:
        import google.auth

        _, project_id = google.auth.default()
    except Exception:  # pragma: no cover - no credentials on a bare checkout
        logger.warning("no ambient GCP credentials; model calls will fail")
        return
    if project_id:
        os.environ["GOOGLE_CLOUD_PROJECT"] = project_id


def _seed_dev_identity(callback_context: CallbackContext) -> None:
    """Attach a caller identity for local runs.

    Only fires when `FLEET_DEV_TOKEN` is set, and only when the session does not
    already carry a verified identity. In deployment the identity is written by
    the server from the authenticated principal and this is a no-op -- a session
    with no identity is meant to fail closed, and a convenience default that
    papers over that would turn the access-control demo into theatre.
    """
    if callback_context.state.get(IDENTITY_KEY):
        return

    token = os.environ.get("FLEET_DEV_TOKEN")
    if not token:
        return

    with session_scope() as session:
        employee = employee_by_token(session, token)
        if employee is None:
            logger.warning("FLEET_DEV_TOKEN does not match any employee")
            return
        identity = Identity(
            employee_id=employee.id, name=employee.name, role=employee.role
        )
    callback_context.state[IDENTITY_KEY] = to_state(identity)
    logger.info("dev identity attached: %s (%s)", identity.name, identity.role.value)


def build_root_agent():
    # Imported lazily so that importing this module stays cheap for tooling that
    # only wants the App metadata.
    from app.fleet import create_coordinator

    agent = create_coordinator()
    agent.before_agent_callback = _seed_dev_identity
    return agent


_route_to_vertex()
init_db()

root_agent = build_root_agent()

app = App(
    name="app",
    root_agent=root_agent,
    plugins=[GuardrailPlugin()],
)
