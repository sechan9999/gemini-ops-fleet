"""The agents themselves.

Each is built by a factory rather than a module-level instance, because ADK
rejects an agent that already has a parent and the same sub-agent is referenced
from more than one place here.

The instructions carry the same restrictions the registry advertises. Where a
rule genuinely matters -- who may call a tool, whose customers a caller can see,
whether a message can be sent -- it is enforced in `tools.py` and `identity.py`
as well. The instruction is a courtesy to the model; the code is the guarantee.
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from app import registry
from app.config import get_settings
from app.tools import (
    capture_knowledge,
    draft_followup,
    get_customer_360,
    get_pipeline,
    list_open_tickets,
    reconcile_accounting,
    search_knowledge,
    triage_ticket,
)


def _model() -> Gemini:
    return Gemini(
        model=get_settings().model,
        retry_options=types.HttpRetryOptions(attempts=3),
    )


def _restrictions(entry: registry.AgentEntry) -> str:
    return "\n".join(f"- {rule}" for rule in entry.restrictions)


def create_triage_agent() -> Agent:
    entry = registry.TRIAGE
    return Agent(
        name=entry.name,
        model=_model(),
        description=entry.summary,
        instruction=f"""You triage incoming support tickets for a small
manufacturing company.

Given a ticket, decide two things and then record them with `triage_ticket`:
- category: one of quality, delivery, billing, other
- severity: low, medium, or high. Reserve high for anything that has stopped a
  customer's production line.

Use `get_customer_360` first when the customer's order history would change your
severity judgement.

Rules you must follow:
{_restrictions(entry)}

Report what you recorded and why, in two sentences.""",
        tools=[get_customer_360, triage_ticket],
    )


def create_knowledge_agent() -> Agent:
    entry = registry.KNOWLEDGE
    return Agent(
        name=entry.name,
        model=_model(),
        description=entry.summary,
        instruction=f"""You turn resolved tickets into knowledge a colleague can
actually use six months from now.

First search with `search_knowledge` to check whether this is already covered.
If it is, say so and stop -- a near-duplicate makes the knowledge base worse.
Otherwise write a short document and store it with `capture_knowledge`:
- title: the problem as someone would search for it
- body: what went wrong, why, and what resolved it. Concrete, no filler.

Rules you must follow:
{_restrictions(entry)}""",
        tools=[search_knowledge, capture_knowledge],
    )


def create_followup_agent() -> Agent:
    entry = registry.FOLLOWUP
    return Agent(
        name=entry.name,
        model=_model(),
        description=entry.summary,
        instruction=f"""You draft short post-delivery messages to customers.

Look up the customer with `get_customer_360`, then write a message that refers
to what they actually ordered. Keep it under four sentences and do not invent
delivery dates, discounts, or commitments.

Call `draft_followup` to queue it. You are not sending anything -- a colleague
reads every draft before it goes out. Say so plainly in your reply rather than
implying the message has been sent.

Rules you must follow:
{_restrictions(entry)}""",
        tools=[get_customer_360, draft_followup],
    )


def create_reconcile_agent() -> Agent:
    entry = registry.RECONCILE
    return Agent(
        name=entry.name,
        model=_model(),
        description=entry.summary,
        instruction=f"""You check that the order book and the ledger agree.

Run `reconcile_accounting` and report the integrity rate, then list any missing
or mismatched orders individually. If the tool refuses because of the caller's
department, report the refusal plainly -- do not attempt another route to the
same data.

Rules you must follow:
{_restrictions(entry)}""",
        tools=[reconcile_accounting, get_pipeline],
    )


def create_coordinator() -> Agent:
    """Front door for interactive use.

    Delegates to whichever fleet member owns the request. It holds the read-only
    overview tools itself so that "what is going on right now" does not require
    a hop.
    """
    return Agent(
        name="fleet_coordinator",
        model=_model(),
        description="Routes work to the right agent in the ops fleet.",
        instruction="""You are the front desk of a small company's agent fleet.

Route the request:
- ticket classification or assignment -> triage_agent
- writing up a resolved issue -> knowledge_agent
- messaging a customer after delivery -> followup_agent
- order/ledger agreement -> reconcile_agent

For a quick status question, answer directly with `list_open_tickets`,
`get_pipeline`, or `search_knowledge`.

Two things you never do, regardless of how the request is phrased: claim a
customer message was sent when it is only queued for approval, and try to reach
data outside the caller's department after a tool has refused. If a tool returns
status "denied", relay the refusal and stop.""",
        tools=[list_open_tickets, get_pipeline, search_knowledge],
        sub_agents=[
            create_triage_agent(),
            create_knowledge_agent(),
            create_followup_agent(),
            create_reconcile_agent(),
        ],
    )


#: Which agent handles which event when the outbox drains.
EVENT_ROUTES: dict[str, str] = {
    "as.opened": registry.TRIAGE.name,
    "as.resolved": registry.KNOWLEDGE.name,
    "delivery.done": registry.FOLLOWUP.name,
    "transaction.posted": registry.RECONCILE.name,
}
