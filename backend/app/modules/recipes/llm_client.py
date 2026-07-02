"""LLM provider for recipe ingredient inference — the ONLY module that imports the
Anthropic SDK.

Quarantine rules (Sprint 5 fail-gate 1, policed by the Phase 14 CI guard):
  - `anthropic` is imported lazily INSIDE the concrete client, so this module imports
    cleanly without the SDK and nothing drags the SDK into an unexpected place.
  - Nothing under app/modules/inventory/depletion/ may import this module or anthropic.
  - The endpoint and service depend on the LLMClient Protocol, not the concrete class,
    so tests inject a fake with no network and no SDK.

Privacy: this module returns token counts + model from the response; the caller logs
those (never the prompt or menu content).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.core.logging import get_logger
from app.modules.inventory.depletion.units import CANONICAL_UNITS

log = get_logger(__name__)

# Tool name the model must call — forces structured output instead of free text.
_TOOL_NAME = "record_recipe_inference"
_CONFIDENCE = ["confident", "likely", "uncertain"]


class LLMUnavailable(Exception):
    """The LLM could not be reached or returned no usable tool output (→ 503).

    Raised for missing API key, timeout, transport/API error, or a response with no
    tool_use block. The endpoint maps it to 503; because /suggest only ever writes the
    append-only suggestion layer, this can never leave a draft or version half-written.
    """


@dataclass(frozen=True)
class LLMResult:
    """Raw tool input from the model plus the metadata the caller logs/stores."""

    payload: dict[str, Any]  # the tool_use input: base_recipe, base_confidence, modifiers
    model_version: str
    input_tokens: int
    output_tokens: int


class LLMClient(Protocol):
    """The seam the service/endpoint depend on. Real impl = Anthropic; tests = fake."""

    async def infer_recipe(
        self, *, prompt_inputs: dict[str, Any], repair_feedback: str | None = None
    ) -> LLMResult: ...


def _ingredient_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Ingredient name, e.g. 'Whole milk'"},
            "quantity": {"type": "number", "description": "Amount per one menu item"},
            # Constrain units to the canonical allowlist at the schema level (best effort;
            # still validated server-side — the model can ignore an enum).
            "unit": {"type": "string", "enum": sorted(CANONICAL_UNITS)},
        },
        "required": ["name", "quantity", "unit"],
    }


def _tool_schema() -> dict[str, Any]:
    ing_array = {"type": "array", "items": _ingredient_schema()}
    return {
        "name": _TOOL_NAME,
        "description": (
            "Record inferred base-recipe and per-modifier ingredients for one menu item. "
            "Use only the provided canonical units. Report honest confidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "base_recipe": {
                    "type": "object",
                    "properties": {"ingredients": ing_array},
                    "required": ["ingredients"],
                },
                "base_confidence": {"type": "string", "enum": _CONFIDENCE},
                "modifiers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "modifier_id": {"type": "string"},
                            "name": {"type": "string"},
                            "confidence": {"type": "string", "enum": _CONFIDENCE},
                            "ingredients": ing_array,
                        },
                        "required": ["name", "confidence", "ingredients"],
                    },
                },
            },
            "required": ["base_recipe", "base_confidence", "modifiers"],
        },
    }


def _build_prompt(prompt_inputs: dict[str, Any], repair_feedback: str | None) -> str:
    mods = prompt_inputs.get("modifiers", [])
    mod_lines = "\n".join(f"  - {m['name']} (modifier_id={m['modifier_id']})" for m in mods)
    menu = ", ".join(prompt_inputs.get("full_menu", []))
    parts = [
        f"Restaurant: {prompt_inputs.get('restaurant_name', 'Unknown')}",
        f"Menu (for cuisine context): {menu}",
        f"Infer the recipe for this menu item: {prompt_inputs['menu_item_name']}",
        "Known modifiers to infer ingredients for:" if mods else "No known modifiers.",
    ]
    if mod_lines:
        parts.append(mod_lines)
    parts.append(
        f"Use ONLY these canonical units: {', '.join(sorted(CANONICAL_UNITS))}. "
        f"Call the {_TOOL_NAME} tool with your answer."
    )
    if repair_feedback:
        parts.append(
            "Your previous answer had problems that must be fixed:\n"
            f"{repair_feedback}\n"
            "Return a corrected answer using only the canonical units above."
        )
    return "\n\n".join(parts)


class AnthropicLLMClient:
    """Concrete LLMClient backed by the Anthropic SDK (lazy-imported)."""

    _SYSTEM = (
        "You are a culinary operations assistant inferring likely ingredient lists for "
        "restaurant menu items, to seed an inventory system. Be concrete and conservative; "
        "prefer fewer, high-confidence ingredients over speculative ones. Always answer by "
        "calling the provided tool."
    )

    def __init__(self, api_key: str, model: str, *, timeout: float = 30.0) -> None:
        import anthropic  # lazy — keeps the SDK out of every import path but this one

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout)
        self._model = model

    async def infer_recipe(
        self, *, prompt_inputs: dict[str, Any], repair_feedback: str | None = None
    ) -> LLMResult:
        prompt = _build_prompt(prompt_inputs, repair_feedback)
        try:
            resp = await self._client.messages.create(  # type: ignore[call-overload]
                model=self._model,
                max_tokens=2048,
                system=self._SYSTEM,
                tools=[_tool_schema()],
                tool_choice={"type": "tool", "name": _TOOL_NAME},
                messages=[{"role": "user", "content": prompt}],
            )
        except self._anthropic.AnthropicError as exc:  # timeout, API, transport
            raise LLMUnavailable(f"anthropic call failed: {type(exc).__name__}") from exc

        payload = next(
            (b.input for b in resp.content if getattr(b, "type", None) == "tool_use"),
            None,
        )
        if not isinstance(payload, dict):
            raise LLMUnavailable("model returned no tool_use block")
        return LLMResult(
            payload=payload,
            model_version=resp.model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
