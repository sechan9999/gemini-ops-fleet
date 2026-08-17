"""Agent registry.

The catalogue another department reads before deciding whether an agent already
exists for their problem. Each entry records who owns the agent, what it can do,
which tools it is allowed to reach, and -- the part that matters for a fleet --
what it is explicitly not allowed to do.

Registration is declarative and lives next to the agents themselves, so an agent
cannot quietly ship without appearing in the catalogue.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class AgentEntry:
    name: str
    version: str
    owner_department: str
    summary: str
    triggers: list[str]
    tools: list[str]
    restrictions: list[str]
    autonomy: str  # "autonomous" | "drafts_only" | "read_only"
    tags: list[str] = field(default_factory=list)


_REGISTRY: dict[str, AgentEntry] = {}


def register(entry: AgentEntry) -> AgentEntry:
    _REGISTRY[entry.name] = entry
    return entry


def get(name: str) -> AgentEntry | None:
    return _REGISTRY.get(name)


def catalog() -> list[dict]:
    """Every registered agent, newest registration order preserved."""
    return [asdict(entry) for entry in _REGISTRY.values()]


def discover(department: str | None, tag: str | None) -> list[dict]:
    """Find agents by owning department and/or capability tag."""
    results = []
    for entry in _REGISTRY.values():
        if department and entry.owner_department != department:
            continue
        if tag and tag not in entry.tags:
            continue
        results.append(asdict(entry))
    return results


TRIAGE = register(
    AgentEntry(
        name="triage_agent",
        version="0.1.0",
        owner_department="support",
        summary="Classifies incoming tickets and assigns an owner.",
        triggers=["as.opened"],
        tools=["get_customer_360", "triage_ticket"],
        restrictions=[
            "Never edits the customer's original message.",
            "Assignment is computed by load, not chosen by the model.",
        ],
        autonomy="autonomous",
        tags=["ticketing", "routing"],
    )
)

KNOWLEDGE = register(
    AgentEntry(
        name="knowledge_agent",
        version="0.1.0",
        owner_department="support",
        summary="Turns resolved tickets into searchable knowledge.",
        triggers=["as.resolved"],
        tools=["search_knowledge", "capture_knowledge"],
        restrictions=[
            "Captured documents inherit the author's department scope.",
            "Cannot widen who may read an existing document.",
        ],
        autonomy="autonomous",
        tags=["knowledge", "retrieval"],
    )
)

FOLLOWUP = register(
    AgentEntry(
        name="followup_agent",
        version="0.1.0",
        owner_department="sales",
        summary="Drafts post-delivery customer messages for human approval.",
        triggers=["delivery.done"],
        tools=["get_customer_360", "draft_followup"],
        restrictions=[
            "Cannot send. Drafts only ever reach the approval queue.",
            "A human approval is required before any message leaves.",
        ],
        autonomy="drafts_only",
        tags=["customer", "outbound"],
    )
)

RECONCILE = register(
    AgentEntry(
        name="reconcile_agent",
        version="0.1.0",
        owner_department="accounting",
        summary="Checks orders against the ledger and reports discrepancies.",
        triggers=["transaction.posted"],
        tools=["reconcile_accounting", "get_pipeline"],
        restrictions=[
            "Read-only against financial records.",
            "Reports discrepancies; never posts, alters, or reverses entries.",
            "Callable only by accounting and management.",
        ],
        autonomy="read_only",
        tags=["finance", "integrity"],
    )
)
