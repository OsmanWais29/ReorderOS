"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.core.config import get_settings
from app.core.database import dispose_engine
from app.core.logging import configure_logging, get_logger
from app.modules.observability.router import router as observability_router


async def _assert_schema_at_head() -> None:
    """Raise RuntimeError if the DB schema is behind the alembic head revision.

    Catches stale workers, missed migrations on deploy, and containers started
    with the wrong DATABASE_URL before any requests are served.
    """
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from app.core.database import get_engine

    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    head_rev = script.get_current_head()

    def _read_current_rev(sync_conn: Any) -> str | None:
        ctx = MigrationContext.configure(sync_conn)
        return ctx.get_current_revision()

    async with get_engine().connect() as conn:
        current_rev = await conn.run_sync(_read_current_rev)

    if current_rev != head_rev:
        raise RuntimeError(
            f"Schema at {current_rev!r}, alembic head is {head_rev!r}. "
            "Run `alembic upgrade head` before starting the app."
        )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log = get_logger("app.lifespan")
    settings = get_settings()
    log.info("startup", env=settings.app_env, version=__version__)
    await _assert_schema_at_head()
    log.info("schema.ok")
    try:
        yield
    finally:
        await dispose_engine()
        log.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="ReorderOS API",
        version=__version__,
        description="Modular FastAPI monolith for ReorderOS v1 (Clover-only).",
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs" if not settings.is_production else None,
        redoc_url=None,
        lifespan=lifespan,
        default_response_class=JSONResponse,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(observability_router)

    # Sprint 2: auth, tenants, invitations
    from app.modules.auth.router import router as auth_router
    from app.modules.invitations.router import router as invitations_router
    from app.modules.tenants.router import router as tenants_router

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(tenants_router, prefix="/api/v1")
    app.include_router(invitations_router, prefix="/api/v1")

    # Sprint 3: inventory
    from app.modules.inventory.router import router as inventory_router

    app.include_router(inventory_router, prefix="/api/v1")

    # Sprint 4: Clover POS OAuth + Webhook
    from app.modules.pos.router import router as pos_router
    from app.modules.pos.webhook import router as webhook_router

    app.include_router(pos_router, prefix="/api/v1")
    app.include_router(webhook_router, prefix="/api/v1")

    # Sprint 5: onboarding Recipes (draft side) + modifier config
    from app.modules.recipes.modifiers_router import router as modifiers_router
    from app.modules.recipes.router import router as recipes_router

    app.include_router(recipes_router, prefix="/api/v1")
    app.include_router(modifiers_router, prefix="/api/v1")

    return app


app = create_app()
