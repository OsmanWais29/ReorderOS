"""Clover catalog sync — populate menu_items + modifiers from the POS catalog.

Mirrors ReconciliationService: runs as the service_worker role via
get_service_sessionmaker(), one connection at a time. Triggered off the HTTP path
by a FastAPI BackgroundTask (immediately at the OAuth callback, and on the
operator-driven POST /pos/clover/sync-menu retry endpoint).

SAFETY — partial pulls must never drive soft-deletes (review tweak 1):
  We fetch ALL pages of items + modifier_groups FIRST. Only if the entire pull
  succeeds do we write (upsert + soft-delete). If any page fails, we abort LOUDLY
  (log an error) and make NO active-status change, leaving the prior catalog
  intact for the operator to retry. A transient Clover error on one page must not
  deactivate half a menu.

Field ownership on re-sync:
  - menu_items: sync updates name/active only; recipe_version_id is operator-owned
    and never written (also enforced by the 0017 column grant).
  - modifiers: new rows are status='draft'; existing rows are left untouched
    (ON CONFLICT DO NOTHING) so an operator's confirmation is never reset.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import bindparam, text

from app.core.encryption import TokenEncryption
from app.core.logging import get_logger
from app.core.service_db import get_service_sessionmaker
from app.modules.pos.clover_client import CloverClient

log = get_logger(__name__)


class CatalogSyncService:
    BATCH_SIZE: int = 100
    MAX_PAGES: int = 50

    # ── Public entry points ───────────────────────────────────────────────────

    async def sync_connection(self, connection_id: str) -> None:
        """Sync one connection's Clover catalog into menu_items + modifiers.

        Never raises — API/processing errors are caught and logged so a
        BackgroundTask failure is observable but doesn't crash the caller.
        Takes only the connection_id (a scalar) and opens its own service session,
        so it is safe to run from a FastAPI BackgroundTask after the response.
        """
        conn = await self._get_connection(connection_id)
        if conn is None:
            log.warning("catalog_sync.connection_not_found", connection_id=connection_id)
            return

        tenant_id = str(conn["tenant_id"])
        merchant_id = str(conn["merchant_id"])
        environment = str(conn["environment"])

        try:
            access_token = TokenEncryption().decrypt(conn["access_token_enc"])
        except Exception as exc:
            log.error(
                "catalog_sync.decrypt_failed",
                connection_id=connection_id,
                error=str(exc)[:200],
            )
            return

        clover = CloverClient(
            access_token=access_token, merchant_id=merchant_id, environment=environment
        )

        # Phase A — fetch the WHOLE catalog first. Any failure aborts before any
        # write, so a partial pull can never drive a soft-delete (tweak 1).
        try:
            items = await self._fetch_all(clover.list_inventory_items)
            groups = await self._fetch_all(clover.list_modifier_groups)
        except Exception as exc:
            log.error(
                "catalog_sync.api_error_abort",
                connection_id=connection_id,
                tenant_id=tenant_id,
                error=str(exc)[:500],
            )
            return  # loud abort, no writes

        # Phase B — full pull succeeded; safe to upsert + soft-delete.
        counts = await self._apply(tenant_id, items, groups)
        log.info(
            "catalog_sync.complete",
            connection_id=connection_id,
            tenant_id=tenant_id,
            **counts,
        )

    async def run_all(self) -> None:
        """Sync every active connection (parity with ReconciliationService).

        Not wired to a worker in Sprint 5 (sync is BackgroundTask-triggered);
        provided for a future durable retry tick.
        """
        sm = get_service_sessionmaker()
        async with sm() as session:
            result = await session.execute(
                text("SELECT connection_id FROM tenant_pos_connections WHERE state = 'active'")
            )
            ids = [str(r[0]) for r in result.fetchall()]
        for cid in ids:
            try:
                await self.sync_connection(cid)
            except Exception as exc:  # pragma: no cover - sync_connection never raises
                log.error("catalog_sync.run_all_error", connection_id=cid, error=str(exc)[:500])

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _fetch_all(
        self, fetch_page: Callable[..., Awaitable[list[dict[str, Any]]]]
    ) -> list[dict[str, Any]]:
        """Page through a Clover collection to completion (raises on API error)."""
        out: list[dict[str, Any]] = []
        offset = 0
        for _ in range(self.MAX_PAGES):
            page = await fetch_page(offset=offset, limit=self.BATCH_SIZE)
            out.extend(page)
            if len(page) < self.BATCH_SIZE:
                return out
            offset += self.BATCH_SIZE
        log.warning("catalog_sync.max_pages_reached", pages=self.MAX_PAGES)
        return out

    async def _get_connection(self, connection_id: str) -> Any:
        sm = get_service_sessionmaker()
        async with sm() as session:
            result = await session.execute(
                text(
                    "SELECT connection_id, tenant_id, merchant_id, environment, access_token_enc"
                    " FROM tenant_pos_connections WHERE connection_id = :cid AND state = 'active'"
                ),
                {"cid": connection_id},
            )
            return result.mappings().fetchone()

    async def _apply(
        self, tenant_id: str, items: list[dict[str, Any]], groups: list[dict[str, Any]]
    ) -> dict[str, int]:
        """Upsert menu_items + modifiers, then soft-delete absent items.

        Runs in one service session with app.tenant_id set (menu_items/modifiers
        are T1-scoped for service_worker).
        """
        # group_id -> [modifier dicts] (modifier detail comes from the groups call)
        group_mods: dict[str, list[dict[str, Any]]] = {}
        for g in groups:
            gid = g.get("id")
            if gid is None:
                continue
            group_mods[str(gid)] = (g.get("modifiers") or {}).get("elements") or []

        n_items = 0
        n_modifiers = 0
        deactivated = 0
        sm = get_service_sessionmaker()
        async with sm() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
            )

            seen: list[str] = []
            for item in items:
                pos_item_id = item.get("id")
                if pos_item_id is None:
                    continue
                pos_item_id = str(pos_item_id)
                name = item.get("name") or "(unnamed)"
                seen.append(pos_item_id)

                # Race-safe upsert via the 0018 partial unique. The conflict target
                # must match the partial index (WHERE pos_item_id IS NOT NULL). The
                # DO UPDATE touches ONLY catalog-owned name/active — recipe_version_id
                # (operator-owned) is never in the SET list, and the 0017 column grant
                # blocks writing it anyway. (menu_items has no updated_at column.)
                menu_item_id = (
                    await session.execute(
                        text(
                            "INSERT INTO menu_items (tenant_id, pos_item_id, name, active)"
                            " VALUES (:tid, :pid, :name, true)"
                            " ON CONFLICT (tenant_id, pos_item_id) WHERE pos_item_id IS NOT NULL"
                            " DO UPDATE SET name = EXCLUDED.name, active = true"
                            " RETURNING id"
                        ),
                        {"tid": tenant_id, "pid": pos_item_id, "name": name},
                    )
                ).scalar_one()
                n_items += 1

                for ig in (item.get("modifierGroups") or {}).get("elements") or []:
                    gid = ig.get("id")
                    if gid is None:
                        continue
                    for m in group_mods.get(str(gid), []):
                        pos_mod_id = m.get("id")
                        if pos_mod_id is None:
                            continue
                        await session.execute(
                            text(
                                "INSERT INTO modifiers"
                                " (tenant_id, menu_item_id, pos_modifier_id, name,"
                                "  modifier_type, status)"
                                " VALUES (:tid, :mid, :pmid, :name, 'additive', 'draft')"
                                " ON CONFLICT (tenant_id, menu_item_id, pos_modifier_id)"
                                "   WHERE pos_modifier_id IS NOT NULL"
                                " DO NOTHING"
                            ),
                            {
                                "tid": tenant_id,
                                "mid": menu_item_id,
                                "pmid": str(pos_mod_id),
                                "name": m.get("name") or "(unnamed)",
                            },
                        )
                        n_modifiers += 1

            # Soft-delete: items previously synced (have a pos_item_id) but absent
            # from this COMPLETE pull. Skipped when the pull returned zero items
            # (conservative — don't mass-deactivate on an empty response).
            if seen:
                stmt = text(
                    "UPDATE menu_items SET active = false"
                    " WHERE tenant_id = :tid AND pos_item_id IS NOT NULL"
                    "   AND active = true AND pos_item_id NOT IN :seen"
                ).bindparams(bindparam("seen", expanding=True))
                res = await session.execute(stmt, {"tid": tenant_id, "seen": seen})
                deactivated = int(getattr(res, "rowcount", 0) or 0)

            await session.commit()

        return {"items": n_items, "modifiers": n_modifiers, "deactivated": deactivated}
