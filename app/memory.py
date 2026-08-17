"""Durable sessions and cross-session memory.

Two different kinds of "remembering", and the fleet needs both:

- **Sessions** hold a conversation while it is happening. On Cloud Run the
  container is disposable, so an in-memory session service loses everything the
  moment an instance is recycled. Cloud SQL makes them survive.
- **Memory Bank** holds what is worth carrying between conversations weeks
  apart -- a customer's standing preference, an instruction someone gave the
  fleet once and expects it to still honour.

Both degrade to in-process implementations when their backing service is not
configured, so a laptop with no credentials still runs the whole system. The
degradation is logged rather than silent: a fleet that quietly forgets is worse
than one that says it will.
"""

from __future__ import annotations

import logging
import os

from google.adk.memory import BaseMemoryService, InMemoryMemoryService
from google.adk.sessions import BaseSessionService, InMemorySessionService

from app.config import get_settings, redact

logger = logging.getLogger(__name__)

#: Display name of the Agent Engine instance that hosts the memory bank. Reused
#: across deploys so redeploying does not orphan the memories.
AGENT_ENGINE_NAME = os.environ.get("AGENT_ENGINE_NAME", "gemini-ops-fleet-memory")


def _agent_engine_location() -> str:
    # Memory Bank is not offered in every region, and `global` is not a valid
    # Agent Engine location, so this is resolved separately from the model
    # location rather than reusing it.
    return os.environ.get("AGENT_ENGINE_LOCATION", "us-central1")


def ensure_agent_engine() -> str | None:
    """Return the id of the Agent Engine backing Memory Bank, creating it once.

    Reuses an instance with the same display name if one exists. Returns None
    when Vertex AI is unreachable, which is the signal to fall back.
    """
    settings = get_settings()
    if not settings.project_id:
        return None

    try:
        import vertexai
        from vertexai._genai.types import (
            AgentEngineConfig,
            ReasoningEngineContextSpec,
            ReasoningEngineContextSpecMemoryBankConfig,
        )

        client = vertexai.Client(
            project=settings.project_id, location=_agent_engine_location()
        )

        for engine in client.agent_engines.list():
            if engine.api_resource.display_name == AGENT_ENGINE_NAME:
                return engine.api_resource.name.split("/")[-1]

        engine = client.agent_engines.create(
            config=AgentEngineConfig(
                display_name=AGENT_ENGINE_NAME,
                context_spec=ReasoningEngineContextSpec(
                    memory_bank_config=ReasoningEngineContextSpecMemoryBankConfig()
                ),
            )
        )
        engine_id = engine.api_resource.name.split("/")[-1]
        logger.info("created Memory Bank agent engine %s", engine_id)
        return engine_id
    except Exception:
        logger.warning("Memory Bank unavailable; using in-process memory", exc_info=True)
        return None


def build_memory_service() -> BaseMemoryService:
    engine_id = os.environ.get("AGENT_ENGINE_ID") or ensure_agent_engine()
    if not engine_id:
        return InMemoryMemoryService()

    try:
        from google.adk.memory import VertexAiMemoryBankService

        service = VertexAiMemoryBankService(
            project=get_settings().project_id,
            location=_agent_engine_location(),
            agent_engine_id=engine_id,
        )
        logger.info("Memory Bank active (agent engine %s)", engine_id)
        return service
    except Exception:
        logger.warning("could not attach Memory Bank; using in-process memory")
        return InMemoryMemoryService()


def build_session_service() -> BaseSessionService:
    """Durable sessions on Cloud SQL, in-memory otherwise.

    Uses the async URL: ADK's session store runs on SQLAlchemy's asyncio
    extension and refuses a synchronous driver, while the fleet's own
    repository code is synchronous. Same database, two drivers.
    """
    settings = get_settings()
    if not settings.session_database_url:
        logger.info("sessions are in-process (no Cloud SQL configured)")
        return InMemorySessionService()

    try:
        from google.adk.sessions import DatabaseSessionService

        service = DatabaseSessionService(db_url=settings.session_database_url)
        logger.info("sessions persisted to Cloud SQL")
        return service
    except Exception as exc:
        # Never log the exception object or a traceback here. A failed
        # connection raises with the full URL -- password included -- in its
        # message, and logging it would publish the credential.
        logger.warning(
            "could not open the session database (%s); using in-process sessions: %s",
            redact(settings.session_database_url),
            type(exc).__name__,
        )
        return InMemorySessionService()
