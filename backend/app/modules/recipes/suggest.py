"""Recipe LLM suggestion service (Sprint 5 Phase 5).

The promotion chain has three actor-owned layers; this service writes ONLY the first:

    LLM   → recipe_llm_suggestions / modifier_llm_suggestions   (append-only — here)
    operator → recipe_drafts        (via PATCH — the deliberate promotion, Phase 3)
    confirm  → recipe_versions      (immutable — the only thing depletion reads, Phase 4)

So /suggest never writes drafts, versions, or recipe_ingredients. Failure safety is a
consequence: an LLM error can at most write no suggestion — it cannot corrupt a draft or
version, because it never touches them.

Output is validated server-side even though the tool schema constrains units: the model
can ignore an enum. Invalid ingredients are kept with valid=false and surfaced to the
operator ("needs input"), never silently dropped — a dropped ingredient is invisible.
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.modules.inventory.depletion.units import is_canonical
from app.modules.recipes.llm_client import LLMClient, LLMUnavailable
from app.modules.recipes.repo import MenuItemNotFound

log = get_logger(__name__)

_VALID_CONFIDENCE = {"confident", "likely", "uncertain"}

# Approximate USD per 1M tokens, for the cost log only (not billing). Unknown model → None.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8": (15.0, 75.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


def _validate_ingredients(raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (validated ingredients with per-item valid flag, list of issue strings).

    Reuses Phase 1 units.is_canonical — the single source of truth, not a parallel list."""
    out: list[dict[str, Any]] = []
    issues: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        name = str(item.get("name", "")).strip()
        unit = str(item.get("unit", ""))
        try:
            quantity = float(item.get("quantity"))
        except (TypeError, ValueError):
            quantity = 0.0
        issue: str | None = None
        if not name:
            issue = "empty name"
        elif quantity <= 0:
            issue = f"quantity not > 0 ({item.get('quantity')!r})"
        elif not is_canonical(unit):
            issue = f"non-canonical unit {unit!r}"
        valid = issue is None
        if issue:
            issues.append(f"{name or '?'}: {issue}")
        out.append(
            {"name": name, "quantity": quantity, "unit": unit, "valid": valid, "issue": issue}
        )
    return out, issues


def _norm_confidence(value: Any) -> str:
    return value if value in _VALID_CONFIDENCE else "uncertain"


def _validate_payload(
    payload: dict[str, Any], known_ids: set[str]
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Validate base + modifier ingredients. Returns (base, modifiers, all_issues)."""
    base_ings, issues = _validate_ingredients(payload.get("base_recipe", {}).get("ingredients"))
    base = {
        "ingredients": base_ings,
        "confidence": _norm_confidence(payload.get("base_confidence")),
    }

    modifiers: list[dict[str, Any]] = []
    for m in payload.get("modifiers", []) if isinstance(payload.get("modifiers"), list) else []:
        m_ings, m_issues = _validate_ingredients(m.get("ingredients"))
        issues.extend(m_issues)
        mid = str(m.get("modifier_id")) if m.get("modifier_id") else None
        modifiers.append(
            {
                "modifier_id": mid if mid in known_ids else None,  # only trust known ids
                "name": str(m.get("name", "")).strip() or "Unnamed modifier",
                "confidence": _norm_confidence(m.get("confidence")),
                "ingredients": m_ings,
            }
        )
    return base, modifiers, issues


async def _gather_inputs(
    db: AsyncSession, tenant_id: UUID, menu_item_id: UUID
) -> dict[str, Any]:
    mi = (
        await db.execute(
            text("SELECT id, name FROM menu_items WHERE id = :mid AND tenant_id = :tid"),
            {"mid": menu_item_id, "tid": tenant_id},
        )
    ).mappings().fetchone()
    if mi is None:
        raise MenuItemNotFound

    restaurant = (
        await db.execute(text("SELECT name FROM tenants WHERE id = :tid"), {"tid": tenant_id})
    ).scalar_one()
    menu = [
        r["name"]
        for r in (
            await db.execute(
                text(
                    "SELECT name FROM menu_items WHERE tenant_id = :tid AND active = true"
                    " ORDER BY name"
                ),
                {"tid": tenant_id},
            )
        ).mappings().all()
    ]
    mods = [
        {"modifier_id": str(r["id"]), "name": r["name"]}
        for r in (
            await db.execute(
                text(
                    "SELECT id, name FROM modifiers"
                    " WHERE tenant_id = :tid AND menu_item_id = :mid ORDER BY name"
                ),
                {"tid": tenant_id, "mid": menu_item_id},
            )
        ).mappings().all()
    ]
    # cuisine_type is not a column on tenants; the full menu carries cuisine context (v5 §2).
    return {
        "menu_item_id": str(menu_item_id),
        "menu_item_name": mi["name"],
        "restaurant_name": restaurant,
        "full_menu": menu,
        "modifiers": mods,
    }


def _log_cost(
    *,
    model_version: str,
    input_tokens: int,
    output_tokens: int,
    tenant_id: UUID,
    menu_item_id: UUID,
    latency_ms: int,
) -> None:
    """Structured cost event — tokens/model/cost/latency ONLY. Never prompt or menu content.
    Token counts are SUMMED across the initial call and any repair retry, so a retry (the
    higher-spend case) is not undercounted."""
    price = _PRICES.get(model_version)
    est_cost = (
        round(input_tokens / 1e6 * price[0] + output_tokens / 1e6 * price[1], 6)
        if price
        else None
    )
    log.info(
        "llm.suggest",
        tenant_id=str(tenant_id),
        menu_item_id=str(menu_item_id),
        model=model_version,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        est_cost_usd=est_cost,
        latency_ms=latency_ms,
    )


async def suggest_recipe(
    db: AsyncSession, llm: LLMClient, tenant_id: UUID, menu_item_id: UUID
) -> dict[str, Any]:
    """Infer base + modifier ingredients and store them append-only. Caller commits.

    Raises MenuItemNotFound (404) / LLMUnavailable (503)."""
    inputs = await _gather_inputs(db, tenant_id, menu_item_id)
    known_ids = {m["modifier_id"] for m in inputs["modifiers"]}

    start = time.monotonic()
    result = await llm.infer_recipe(prompt_inputs=inputs)
    input_tokens, output_tokens = result.input_tokens, result.output_tokens
    model_version = result.model_version
    base, modifiers, issues = _validate_payload(result.payload, known_ids)

    # one repair retry max: feed the issues back and re-validate. If the retry CALL itself
    # fails, keep the first (flagged) result — "retry once, then store what you have" —
    # rather than 503-ing away usable output.
    if issues:
        try:
            retry = await llm.infer_recipe(
                prompt_inputs=inputs, repair_feedback="; ".join(issues)
            )
        except LLMUnavailable:
            log.warning(
                "llm.suggest.repair_failed",
                tenant_id=str(tenant_id),
                menu_item_id=str(menu_item_id),
            )
        else:
            input_tokens += retry.input_tokens  # SUM both billed calls
            output_tokens += retry.output_tokens
            model_version = retry.model_version
            base, modifiers, _ = _validate_payload(retry.payload, known_ids)
    latency_ms = int((time.monotonic() - start) * 1000)

    _log_cost(
        model_version=model_version,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tenant_id=tenant_id,
        menu_item_id=menu_item_id,
        latency_ms=latency_ms,
    )

    # ── append-only storage — NO drafts/versions/ingredients ──────────────────
    suggestion_id = (
        await db.execute(
            text("""
                INSERT INTO recipe_llm_suggestions
                    (tenant_id, menu_item_id, suggested_ingredients, model_version, confidence)
                VALUES (:tid, :mid, CAST(:ings AS jsonb), :model, :conf)
                RETURNING id
            """),
            {
                "tid": tenant_id,
                "mid": menu_item_id,
                "ings": json.dumps(base["ingredients"]),
                "model": model_version,
                "conf": base["confidence"],
            },
        )
    ).scalar_one()

    for m in modifiers:
        await db.execute(
            text("""
                INSERT INTO modifier_llm_suggestions
                    (tenant_id, menu_item_id, modifier_id, modifier_name,
                     suggested_ingredients, model_version, confidence)
                VALUES (:tid, :mid, :modid, :mname, CAST(:ings AS jsonb), :model, :conf)
            """),
            {
                "tid": tenant_id,
                "mid": menu_item_id,
                "modid": m["modifier_id"],
                "mname": m["name"],
                "ings": json.dumps(m["ingredients"]),
                "model": model_version,
                "conf": m["confidence"],
            },
        )

    return {
        "suggestion_id": suggestion_id,
        "menu_item_id": menu_item_id,
        "base_ingredients": base["ingredients"],
        "confidence": base["confidence"],
        "modifiers": modifiers,
        "model_version": model_version,
    }
