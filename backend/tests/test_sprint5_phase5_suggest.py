"""Sprint 5 Phase 5 — LLM ingredient inference service.

The endpoint depends on the LLMClient Protocol, so every test injects a fake via the
get_llm_client dependency override — no network, no SDK key. The bound-transaction
harness (Phase 3/4) gives clean, residue-free runs.

What's proven: append-only storage, the suggestion-vs-draft boundary (zero writes to
recipe_drafts / recipe_versions / recipe_ingredients), per-ingredient validity flags
(invalid kept, not dropped), one repair retry, clean 503 on LLM failure + missing key,
RBAC, cross-tenant 404, and the depletion import-isolation boundary (fail-gate 1).
"""

from __future__ import annotations

import types
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest
import structlog
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from uuid6 import uuid7

from app.core.database import engine, get_db_session, make_bound_session
from app.core.security import Principal, get_principal
from app.main import create_app
from app.modules.recipes.llm_client import AnthropicLLMClient, LLMResult, LLMUnavailable
from app.modules.recipes.router import get_llm_client

_PROMPT = {"menu_item_name": "Latte", "restaurant_name": "Cafe", "full_menu": [], "modifiers": []}

R = "/api/v1/onboarding"


# ── fake LLM client ──────────────────────────────────────────────────────────


class FakeLLM:
    """Returns scripted payloads (one per call); records call count; can raise."""

    def __init__(
        self,
        payloads: list[dict[str, Any]],
        *,
        model: str = "claude-sonnet-4-6",
        raise_exc: Exception | None = None,
    ) -> None:
        self._payloads = payloads
        self._model = model
        self._raise = raise_exc
        self.calls = 0

    async def infer_recipe(
        self, *, prompt_inputs: dict[str, Any], repair_feedback: str | None = None
    ) -> LLMResult:
        if self._raise is not None:
            raise self._raise
        payload = self._payloads[min(self.calls, len(self._payloads) - 1)]
        self.calls += 1
        return LLMResult(payload=payload, model_version=self._model, input_tokens=100, output_tokens=50)


def _payload(base_ings: list[dict[str, Any]], modifiers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "base_recipe": {"ingredients": base_ings},
        "base_confidence": "likely",
        "modifiers": modifiers or [],
    }


# ── bound-transaction harness ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def app_instance() -> Any:
    return create_app()


@pytest.fixture
async def conn(app_instance: Any) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        await connection.begin()
        db = make_bound_session(connection)
        app_instance.dependency_overrides[get_db_session] = lambda: db
        try:
            yield connection
        finally:
            app_instance.dependency_overrides.clear()
            await connection.rollback()


@pytest.fixture
async def client(app_instance: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app_instance), base_url="http://test"
    ) as c:
        yield c


def _as(app_instance: Any, tenant_id: str, user_id: str, role: str) -> None:
    app_instance.dependency_overrides[get_principal] = lambda: Principal(
        user_id=user_id,
        workos_id=f"w_{user_id[:8]}",
        email="x@test.com",
        tenant_id=tenant_id,
        role=role,  # type: ignore[arg-type]
    )


def _use_llm(app_instance: Any, fake: FakeLLM) -> None:
    app_instance.dependency_overrides[get_llm_client] = lambda: fake


async def _seed_tenant_user(conn: AsyncConnection) -> tuple[str, str]:
    tid = str(uuid7())
    await conn.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'Cafe', :slug)"),
        {"id": tid, "slug": f"t-{uuid.uuid4().hex[:8]}"},
    )
    uid = (
        await conn.execute(
            text(
                "INSERT INTO users (workos_id, email, email_verified)"
                " VALUES (:w, :e, true) RETURNING id"
            ),
            {"w": f"u-{uuid.uuid4().hex[:8]}", "e": f"{uuid.uuid4().hex[:8]}@t.com"},
        )
    ).scalar_one()
    return tid, str(uid)


async def _menu_item(conn: AsyncConnection, tid: str, name: str = "Latte") -> str:
    mid = (
        await conn.execute(
            text("INSERT INTO menu_items (tenant_id, name, active) VALUES (:t, :n, true) RETURNING id"),
            {"t": tid, "n": name},
        )
    ).scalar_one()
    return str(mid)


async def _modifier(conn: AsyncConnection, tid: str, mid: str, name: str = "Extra shot") -> str:
    rid = (
        await conn.execute(
            text(
                "INSERT INTO modifiers (tenant_id, menu_item_id, name, modifier_type, status)"
                " VALUES (:t, :m, :n, 'additive', 'draft') RETURNING id"
            ),
            {"t": tid, "m": mid, "n": name},
        )
    ).scalar_one()
    return str(rid)


async def _count(conn: AsyncConnection, sql: str, params: dict[str, Any]) -> int:
    return int((await conn.execute(text(sql), params)).scalar_one())


_GOOD = [{"name": "Whole milk", "quantity": 200, "unit": "ml"}]


# ── happy path + storage ─────────────────────────────────────────────────────


@pytest.mark.integration
async def test_suggest_stores_base_and_modifier_appendonly(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    modid = await _modifier(conn, tid, mid)
    _as(app_instance, tid, uid, "manager")
    _use_llm(
        app_instance,
        FakeLLM([_payload(
            _GOOD,
            [{"modifier_id": modid, "name": "Extra shot", "confidence": "confident",
              "ingredients": [{"name": "Espresso", "quantity": 7, "unit": "g"}]}],
        )]),
    )

    resp = await client.post(f"{R}/recipes/{mid}/suggest")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model_version"] == "claude-sonnet-4-6"
    assert body["base_ingredients"][0] == {
        "name": "Whole milk", "quantity": 200.0, "unit": "ml", "valid": True, "issue": None
    }
    assert body["modifiers"][0]["modifier_id"] == modid

    assert await _count(
        conn, "SELECT count(*) FROM recipe_llm_suggestions WHERE tenant_id=:t AND menu_item_id=:m",
        {"t": tid, "m": mid},
    ) == 1
    assert await _count(
        conn,
        "SELECT count(*) FROM modifier_llm_suggestions WHERE tenant_id=:t AND modifier_id=:r",
        {"t": tid, "r": modid},
    ) == 1


@pytest.mark.integration
async def test_suggest_does_not_write_drafts_versions(app_instance, conn, client) -> None:
    """The boundary: /suggest writes only the suggestion layer."""
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    _use_llm(app_instance, FakeLLM([_payload(_GOOD)]))

    assert (await client.post(f"{R}/recipes/{mid}/suggest")).status_code == 200

    for table in ("recipe_drafts", "recipe_versions", "recipe_ingredients"):
        assert await _count(conn, f"SELECT count(*) FROM {table} WHERE tenant_id=:t", {"t": tid}) == 0


@pytest.mark.integration
async def test_suggest_preserves_existing_operator_draft(app_instance, conn, client) -> None:
    """The headline boundary requirement: an operator's in-progress draft is NOT
    clobbered by a suggest (the data-loss risk the whole three-layer model guards)."""
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    # operator builds a draft first
    await client.patch(
        f"{R}/recipes/{mid}",
        json={"ingredients": [{"name": "Operator milk", "quantity": 111, "unit": "ml"}]},
    )
    q = text(
        "SELECT rd.draft_ingredients::text AS di, r.status AS status, rd.updated_at AS up"
        " FROM recipe_drafts rd JOIN recipes r ON r.id = rd.recipe_id"
        " WHERE r.tenant_id = :t AND r.menu_item_id = :m"
    )
    before = (await conn.execute(q, {"t": tid, "m": mid})).mappings().one()

    _use_llm(app_instance, FakeLLM([_payload(_GOOD)]))  # suggests different ingredients
    assert (await client.post(f"{R}/recipes/{mid}/suggest")).status_code == 200

    after = (await conn.execute(q, {"t": tid, "m": mid})).mappings().one()
    assert after["di"] == before["di"]  # operator's draft ingredients untouched
    assert after["status"] == before["status"] == "draft"
    assert after["up"] == before["up"]  # not even touched (no UPDATE)


@pytest.mark.integration
async def test_suggest_appends_on_rerun(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    _use_llm(app_instance, FakeLLM([_payload(_GOOD)]))

    await client.post(f"{R}/recipes/{mid}/suggest")
    await client.post(f"{R}/recipes/{mid}/suggest")
    assert await _count(
        conn, "SELECT count(*) FROM recipe_llm_suggestions WHERE tenant_id=:t AND menu_item_id=:m",
        {"t": tid, "m": mid},
    ) == 2  # append-only history, not replace


# ── validation: invalid kept-and-flagged, repair retry ───────────────────────


@pytest.mark.integration
async def test_invalid_unit_kept_flagged_not_dropped(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    bad = [
        {"name": "Whole milk", "quantity": 200, "unit": "ml"},   # canonical
        {"name": "Vanilla", "quantity": 1, "unit": "ounces"},     # non-canonical
    ]
    # returns the bad payload on both the first call and the repair retry
    fake = FakeLLM([_payload(bad)])
    _use_llm(app_instance, fake)

    body = (await client.post(f"{R}/recipes/{mid}/suggest")).json()
    by_name = {i["name"]: i for i in body["base_ingredients"]}
    assert len(by_name) == 2  # nothing dropped
    assert by_name["Whole milk"]["valid"] is True
    assert by_name["Vanilla"]["valid"] is False
    assert "non-canonical" in by_name["Vanilla"]["issue"]
    assert fake.calls == 2  # one repair retry was attempted


@pytest.mark.integration
async def test_repair_retry_fixes_invalid(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    bad = _payload([{"name": "Vanilla", "quantity": 1, "unit": "ounces"}])
    good = _payload([{"name": "Vanilla", "quantity": 5, "unit": "ml"}])
    fake = FakeLLM([bad, good])  # first invalid, retry valid; 100/50 tokens each call
    _use_llm(app_instance, fake)

    with structlog.testing.capture_logs() as logs:
        body = (await client.post(f"{R}/recipes/{mid}/suggest")).json()
    assert fake.calls == 2
    assert body["base_ingredients"][0]["valid"] is True
    assert body["base_ingredients"][0]["unit"] == "ml"
    # cost log SUMS both billed calls, not just the retry
    cost = next(e for e in logs if e["event"] == "llm.suggest")
    assert cost["input_tokens"] == 200
    assert cost["output_tokens"] == 100


# ── failure handling ─────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_llm_failure_returns_503_and_writes_nothing(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "manager")
    _use_llm(app_instance, FakeLLM([], raise_exc=LLMUnavailable("timeout")))

    resp = await client.post(f"{R}/recipes/{mid}/suggest")
    assert resp.status_code == 503
    # nothing written anywhere — not even a suggestion row
    for table in ("recipe_llm_suggestions", "recipe_drafts", "recipe_versions"):
        assert await _count(conn, f"SELECT count(*) FROM {table} WHERE tenant_id=:t", {"t": tid}) == 0


@pytest.mark.integration
async def test_missing_api_key_yields_503(monkeypatch) -> None:
    """get_llm_client raises a clean 503 when no key is configured (no override)."""
    from app.modules.recipes import router as recipes_router

    class _NoKey:
        anthropic_api_key = None
        anthropic_model = "claude-sonnet-4-6"

    monkeypatch.setattr(recipes_router, "get_settings", lambda: _NoKey())
    with pytest.raises(HTTPException) as ei:
        recipes_router.get_llm_client()
    assert ei.value.status_code == 503


# ── RBAC + cross-tenant ──────────────────────────────────────────────────────


@pytest.mark.integration
async def test_suggest_staff_403(app_instance, conn, client) -> None:
    tid, uid = await _seed_tenant_user(conn)
    mid = await _menu_item(conn, tid)
    _as(app_instance, tid, uid, "staff")
    _use_llm(app_instance, FakeLLM([_payload(_GOOD)]))
    assert (await client.post(f"{R}/recipes/{mid}/suggest")).status_code == 403


@pytest.mark.integration
async def test_suggest_cross_tenant_404(app_instance, conn, client) -> None:
    tid_b, _ = await _seed_tenant_user(conn)
    mid_b = await _menu_item(conn, tid_b, "B-item")
    tid_a, uid_a = await _seed_tenant_user(conn)
    _as(app_instance, tid_a, uid_a, "manager")
    _use_llm(app_instance, FakeLLM([_payload(_GOOD)]))
    assert (await client.post(f"{R}/recipes/{mid_b}/suggest")).status_code == 404


# ── real AnthropicLLMClient (SDK call stubbed, no network) ───────────────────


def _real_client() -> AnthropicLLMClient:
    # constructs the SDK client (no network at construction); we stub messages.create
    return AnthropicLLMClient(api_key="test-key", model="claude-sonnet-4-6")


@pytest.mark.integration
async def test_real_client_parses_tool_use_and_usage() -> None:
    c = _real_client()

    async def fake_create(**_kw: Any) -> Any:
        return types.SimpleNamespace(
            content=[
                types.SimpleNamespace(type="text", text="reasoning"),
                types.SimpleNamespace(
                    type="tool_use",
                    input={
                        "base_recipe": {"ingredients": [{"name": "Milk", "quantity": 1, "unit": "ml"}]},
                        "base_confidence": "likely",
                        "modifiers": [],
                    },
                ),
            ],
            model="claude-sonnet-4-6",
            usage=types.SimpleNamespace(input_tokens=123, output_tokens=45),
        )

    c._client.messages.create = fake_create  # type: ignore[method-assign]
    result = await c.infer_recipe(prompt_inputs=_PROMPT)
    assert result.payload["base_confidence"] == "likely"
    assert result.input_tokens == 123
    assert result.output_tokens == 45
    assert result.model_version == "claude-sonnet-4-6"


@pytest.mark.integration
async def test_real_client_timeout_maps_to_unavailable() -> None:
    c = _real_client()

    async def boom(**_kw: Any) -> Any:
        raise anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com"))

    c._client.messages.create = boom  # type: ignore[method-assign]
    with pytest.raises(LLMUnavailable):
        await c.infer_recipe(prompt_inputs=_PROMPT)


@pytest.mark.integration
async def test_real_client_no_tool_block_maps_to_unavailable() -> None:
    c = _real_client()

    async def text_only(**_kw: Any) -> Any:
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="sorry")],
            model="claude-sonnet-4-6",
            usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
        )

    c._client.messages.create = text_only  # type: ignore[method-assign]
    with pytest.raises(LLMUnavailable):
        await c.infer_recipe(prompt_inputs=_PROMPT)


# ── fail-gate 1: depletion never imports the LLM ─────────────────────────────


def test_depletion_does_not_import_llm() -> None:
    """No module under inventory/depletion/ may import the SDK or the LLM modules.
    String-level check here exercises the boundary; the Phase 14 guard does the full
    direct+transitive import-graph version."""
    root = Path(__file__).resolve().parents[1] / "app" / "modules" / "inventory" / "depletion"
    forbidden = ("anthropic", "llm_client", "recipes.suggest", "from app.modules.recipes")
    offenders = [
        (p.name, token)
        for p in root.rglob("*.py")
        for token in forbidden
        if token in p.read_text()
    ]
    assert not offenders, f"depletion imports LLM/recipes: {offenders}"
