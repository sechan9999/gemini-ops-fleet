"""Inline guardrails at the model boundary.

Registered as an ADK plugin so it applies to every agent in the fleet at once --
a per-agent callback would leave a hole the moment someone adds an agent and
forgets to wire it up.

Two backends behind one interface: Model Armor when the project is configured
for it, and a small heuristic screen otherwise. The heuristic is not a security
control and does not pretend to be; it exists so the guardrail path is exercised
on a laptop with no credentials, and so tests can prove the plumbing works.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from google.adk.agents.invocation_context import InvocationContext
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools import BaseTool, ToolContext
from google.genai import types

from app import tracing
from app.config import get_settings

logger = logging.getLogger(__name__)

BLOCKED_PROMPT_MESSAGE = (
    "That request was blocked by the fleet's inline guardrail before it reached "
    "the model."
)
BLOCKED_TOOL_MESSAGE = (
    "The tool result was withheld by the fleet's inline guardrail."
)

# Attempts to talk the agent out of its own rules. Matching here is coarse on
# purpose: the guardrail is one layer, and the layer that actually holds is that
# roles are resolved server-side and cannot be argued with.
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I),
    re.compile(r"disregard\s+(your|the)\s+(rules|instructions|policy)", re.I),
    re.compile(r"you\s+are\s+now\s+(in\s+)?(developer|admin|god)\s+mode", re.I),
    re.compile(r"reveal\s+(your|the)\s+(system\s+)?prompt", re.I),
    re.compile(r"pretend\s+(you\s+are|to\s+be)\s+(an?\s+)?(admin|accountant)", re.I),
    re.compile(r"act\s+as\s+(if\s+you\s+are\s+)?(the\s+)?accounting", re.I),
]

# Contact details that should not be echoed back out of a tool result.
_PII_PATTERNS = [
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    re.compile(r"\+?\d{1,3}[-\s]?\d{2,4}[-\s]?\d{3,4}[-\s]?\d{3,4}\b"),
]


class HeuristicScreen:
    """Offline stand-in for Model Armor."""

    def scan_prompt(self, text: str) -> str | None:
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(text):
                return "prompt_injection"
        return None

    def scan_output(self, text: str) -> str | None:
        for pattern in _PII_PATTERNS:
            if pattern.search(text):
                return "pii"
        return None


class ModelArmorScreen:
    """Model Armor backend, used when a template is configured."""

    def __init__(self, project_id: str, location: str, template_id: str) -> None:
        from google.api_core.client_options import ClientOptions
        from google.cloud import modelarmor_v1

        self._modelarmor = modelarmor_v1
        self._name = (
            f"projects/{project_id}/locations/{location}/templates/{template_id}"
        )
        self._client = modelarmor_v1.ModelArmorClient(
            client_options=ClientOptions(
                api_endpoint=f"modelarmor.{location}.rep.googleapis.com"
            )
        )

    def _flagged(self, response: Any) -> str | None:
        result = getattr(response, "sanitization_result", None)
        if result is None:
            return None
        # Model Armor reports a match verdict per filter; any match is a block.
        if getattr(result, "filter_match_state", 0) == 1:
            return "model_armor"
        return None

    def scan_prompt(self, text: str) -> str | None:
        request = self._modelarmor.SanitizeUserPromptRequest(
            name=self._name,
            user_prompt_data=self._modelarmor.DataItem(text=text),
        )
        return self._flagged(self._client.sanitize_user_prompt(request=request))

    def scan_output(self, text: str) -> str | None:
        request = self._modelarmor.SanitizeModelResponseRequest(
            name=self._name,
            model_response_data=self._modelarmor.DataItem(text=text),
        )
        return self._flagged(self._client.sanitize_model_response(request=request))


def build_screen():
    settings = get_settings()
    if settings.model_armor_enabled:
        try:
            return ModelArmorScreen(
                settings.project_id, settings.location, settings.model_armor_template
            )
        except Exception:  # pragma: no cover - depends on cloud availability
            logger.warning("Model Armor unavailable; falling back to heuristics")
    return HeuristicScreen()


class GuardrailPlugin(BasePlugin):
    """Blocks injection attempts on the way in and leaks on the way out."""

    def __init__(self, screen: Any | None = None) -> None:
        super().__init__(name="fleet_guardrail")
        self._screen = screen or build_screen()
        self.blocked: list[dict] = []

    def _record(self, stage: str, reason: str, sample: str) -> None:
        entry = {"stage": stage, "reason": reason, "sample": sample[:120]}
        self.blocked.append(entry)
        logger.warning("guardrail blocked %s: %s", stage, reason)
        tracing.record_guardrail_block(stage, reason)

    async def on_user_message_callback(
        self,
        invocation_context: InvocationContext,
        user_message: types.Content,
    ) -> types.Content | None:
        text = "".join(part.text or "" for part in (user_message.parts or []))
        reason = self._screen.scan_prompt(text)
        if reason is None:
            return None
        self._record("prompt", reason, text)
        invocation_context.session.state["fleet:prompt_blocked"] = reason
        # The replacement is an instruction, not a statement. Handing the model
        # a bare notice let it treat the turn as an open-ended request and go
        # off to do unrelated work -- correct behaviour from its point of view,
        # and a confusing thing to watch. Telling it exactly what to say keeps a
        # blocked turn inert and identical every time.
        return types.Content(
            role="user",
            parts=[
                types.Part.from_text(
                    text=(
                        "The message you were about to receive was removed by a "
                        "safety guardrail. Reply with exactly this sentence and "
                        "nothing else, and call no tools: "
                        f"{BLOCKED_PROMPT_MESSAGE}"
                    )
                )
            ],
        )

    async def before_run_callback(
        self, invocation_context: InvocationContext
    ) -> types.Content | None:
        """Stop the run outright when the incoming prompt was blocked."""
        reason = invocation_context.session.state.pop("fleet:prompt_blocked", None)
        if reason is None:
            return None
        return types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text=f"{BLOCKED_PROMPT_MESSAGE} (reason: {reason})"
                )
            ],
        )

    async def after_tool_callback(
        self,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        reason = self._screen.scan_output(str(result))
        if reason is None:
            return None
        self._record(f"tool:{tool.name}", reason, str(result))
        return {"status": "blocked", "reason": BLOCKED_TOOL_MESSAGE}
