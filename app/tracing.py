"""Fleet decisions, attached to the OpenTelemetry trace.

ADK already emits the execution skeleton -- invocation, agent_run, call_llm,
execute_tool -- so this module does not re-trace any of that. What it adds is
the governance layer: who the caller was, whether authorisation held, what a
guardrail stopped, and which agent an event was routed to.

That distinction matters for the audit story. A latency trace tells you the
agent ran. These attributes tell you what it was allowed to do while it ran, and
they sit on the same span, so one trace answers both questions.

Attributes are namespaced `fleet.*` to keep them separable from the GenAI
semantic conventions ADK emits.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace

tracer = trace.get_tracer("gemini_ops_fleet")


def _current():
    span = trace.get_current_span()
    # A no-op span is returned when nothing is recording; writing to it is safe
    # and keeps every caller free of null checks.
    return span


def record_tool_outcome(tool: str, role: str, outcome: str, detail: str) -> None:
    """Attach an authorisation decision to the active span.

    Called for allowed and denied alike. A denial is the more interesting event
    of the two -- it is the evidence that the boundary was exercised -- so it is
    recorded with the same fidelity rather than being swallowed.
    """
    span = _current()
    span.set_attribute("fleet.tool", tool)
    span.set_attribute("fleet.caller_role", role)
    span.set_attribute("fleet.authorization", outcome)
    span.add_event(
        "fleet.tool_call",
        {
            "fleet.tool": tool,
            "fleet.caller_role": role,
            "fleet.authorization": outcome,
            "fleet.detail": detail[:400],
        },
    )
    if outcome == "denied":
        # Surfaces denials in trace search without needing an attribute filter.
        span.set_attribute("fleet.access_denied", True)


def record_guardrail_block(stage: str, reason: str) -> None:
    span = _current()
    span.set_attribute("fleet.guardrail_blocked", True)
    span.set_attribute("fleet.guardrail_reason", reason)
    span.add_event(
        "fleet.guardrail_block", {"fleet.stage": stage, "fleet.reason": reason}
    )


@contextmanager
def route_span(kind: str, agent_name: str, activity_id: int) -> Iterator[None]:
    """Wrap one asynchronous event dispatch.

    Gives the background path its own root span. Without it, work triggered by
    an event has no trace at all -- there is no incoming request to hang it off.
    """
    with tracer.start_as_current_span("fleet.dispatch") as span:
        span.set_attribute("fleet.event_kind", kind)
        span.set_attribute("fleet.routed_to", agent_name)
        span.set_attribute("fleet.activity_id", activity_id)
        yield


@contextmanager
def approval_span(action: str, approval_id: int, actor: str) -> Iterator[None]:
    """Wrap a human decision on the approval queue."""
    with tracer.start_as_current_span("fleet.human_gate") as span:
        span.set_attribute("fleet.action", action)
        span.set_attribute("fleet.approval_id", approval_id)
        span.set_attribute("fleet.human_actor", actor)
        yield
