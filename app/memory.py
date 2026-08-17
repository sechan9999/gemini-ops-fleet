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


def build_memory_bank_config():
    """Declare what is worth remembering between conversations.

    A memory bank created without topics extracts nothing -- the config is not
    an optional refinement, it is what tells the extractor to look. Three
    managed topics matter for back-office work:

    - EXPLICIT_INSTRUCTIONS: "from now on, escalate Acme tooling issues"
    - USER_PREFERENCES: how a colleague likes drafts written
    - KEY_CONVERSATION_DETAILS: outcomes worth carrying to the next ticket
    """
    from vertexai._genai.types import (
        ManagedTopicEnum,
        MemoryBankCustomizationConfig,
        MemoryBankCustomizationConfigMemoryTopic,
        MemoryBankCustomizationConfigMemoryTopicManagedMemoryTopic,
        MemoryGenerationTriggerConfig,
        MemoryGenerationTriggerConfigGenerationTriggerRule,
        ReasoningEngineContextSpecMemoryBankConfig,
        ReasoningEngineContextSpecMemoryBankConfigGenerationConfig,
    )

    topics = [
        ManagedTopicEnum.EXPLICIT_INSTRUCTIONS,
        ManagedTopicEnum.USER_PREFERENCES,
        ManagedTopicEnum.KEY_CONVERSATION_DETAILS,
    ]
    return ReasoningEngineContextSpecMemoryBankConfig(
        customization_configs=[
            MemoryBankCustomizationConfig(
                memory_topics=[
                    MemoryBankCustomizationConfigMemoryTopic(
                        managed_memory_topic=(
                            MemoryBankCustomizationConfigMemoryTopicManagedMemoryTopic(
                                managed_topic_enum=topic
                            )
                        )
                    )
                    for topic in topics
                ]
            )
        ],
        # Without a trigger rule, ingested events are stored and never turned
        # into memories -- ingestion succeeds, the bank stays empty, and nothing
        # in the response says why. Generating after a single event keeps a
        # colleague's standing instruction usable on the very next request,
        # which is the behaviour a back-office fleet needs.
        generation_config=ReasoningEngineContextSpecMemoryBankConfigGenerationConfig(
            generation_trigger_config=MemoryGenerationTriggerConfig(
                generation_rule=MemoryGenerationTriggerConfigGenerationTriggerRule(
                    event_count=1
                )
            )
        ),
    )


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
                    memory_bank_config=build_memory_bank_config()
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


def store_session_memories(session) -> int:
    """Turn a finished conversation into memories, and return how many events fed it.

    ADK's `add_session_to_memory` posts to `ingest_events`, which the service
    accepts and acknowledges -- and which produced no memories for us under any
    combination of topic and trigger configuration we tried. Generation from the
    same content works immediately, so this calls `memories.generate` directly
    rather than waiting on a path that reports success without doing anything.

    The scope is the same `{app_name, user_id}` pair ADK's retrieval uses, which
    is what lets `PreloadMemoryTool` find what this writes.
    """
    engine_id = os.environ.get("AGENT_ENGINE_ID") or ensure_agent_engine()
    settings = get_settings()
    if not engine_id or not settings.project_id:
        return 0

    events = []
    for event in getattr(session, "events", []) or []:
        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None) or []
        text = "".join(p.text or "" for p in parts).strip()
        if not text:
            continue
        events.append(
            {"content": {"role": getattr(content, "role", "user") or "user",
                         "parts": [{"text": text}]}}
        )

    if not events:
        return 0

    import vertexai

    client = vertexai.Client(
        project=settings.project_id, location=_agent_engine_location()
    )
    client.agent_engines.memories.generate(
        name=(
            f"projects/{settings.project_id}/locations/"
            f"{_agent_engine_location()}/reasoningEngines/{engine_id}"
        ),
        direct_contents_source={"events": events},
        scope={"app_name": session.app_name, "user_id": session.user_id},
        # Generation is a long-running operation. Returning before it finishes
        # means the next session can ask a question the bank cannot yet answer
        # -- which looks exactly like memory not working at all, and is how we
        # misdiagnosed this for an afternoon.
        config={"wait_for_completion": True},
    )
    return len(events)


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
