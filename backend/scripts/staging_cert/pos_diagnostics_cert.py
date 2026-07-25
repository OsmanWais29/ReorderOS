"""Independent POS-diagnostics staging certification for the insights endpoint.

STAGING ONLY. Makes controlled writes (test tenants + the real depletion
pipeline) against the DEPLOYED insights endpoint, then verifies the response
against hand-frozen expectations. Run inside the deployed image on staging:

    cd /srv && ALLOW_STAGING_CERT=1 python3 -m scripts.staging_cert.pos_diagnostics_cert

Safeguards:
  * _guard() refuses to run unless ALLOW_STAGING_CERT=1 (set only on staging) and
    no production marker is present — it MUST NEVER run against production.
  * every tenant created is deleted (CASCADE) in a finally block — staging is
    left clean whether the run passes, fails, or raises.
  * no secret/token is printed; the process exits non-zero on ANY failed invariant.

Independence discipline: does NOT import or call _pos_diagnostics, _stage_status,
_pct, _e2e_partition, the eligibility helpers, or on_hand, and does NOT translate
their branch logic. Expected values are HAND-FROZEN literals written before the
endpoint is called. The only app code under test we invoke is
handler.process_line (the real depletion pipeline) — statuses produced that way
are labeled 'real'; statuses inserted directly are labeled 'projection_only'.

Per scenario: (1) seed controlled raw rows (real pipeline where feasible),
(2) compare the deployed endpoint's pos.dimensions to the frozen expected,
(3) assert the reconciliation identity depleted+failures+pending+unknown==eligible
on the ENDPOINT's own reported counts.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import UTC
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.security import Principal, get_principal
from app.core.service_db import get_service_sessionmaker
from app.main import create_app
from app.modules.inventory.depletion import handler  # the pipeline UNDER TEST

UTC = UTC
RUN = uuid.uuid4().hex[:8]
EV: dict = {"run": RUN, "scenarios": {}, "errors": []}
# Every tenant this run creates — deleted (CASCADE) in the finally block so the
# staging DB is left clean whether the run passes, fails, or raises.
CREATED_TENANTS: list[str] = []


def _guard() -> None:
    """HARD staging-only gate. This script makes controlled writes and MUST NEVER
    run against production. It refuses unless ALLOW_STAGING_CERT=1 is set (only on
    staging) and the DB is not flagged as production. Never prints secrets."""
    if os.environ.get("ALLOW_STAGING_CERT") != "1":
        print(
            "REFUSING: set ALLOW_STAGING_CERT=1 (staging only — NEVER production).",
            file=sys.stderr,
        )
        sys.exit(2)
    if os.environ.get("REORDEROS_PRODUCTION") == "1" or os.environ.get("IS_PRODUCTION") == "1":
        print("REFUSING: production marker present.", file=sys.stderr)
        sys.exit(2)


SM = get_service_sessionmaker()
NOW_SALE = None  # set at runtime


async def svc(tid, fn, uid=None, role="owner"):
    async with SM() as s:
        async with s.begin():
            await s.execute(
                text(
                    "SELECT set_config('app.user_id',:u,true),set_config('app.tenant_id',:t,true),"
                    "set_config('app.user_role',:r,true),set_config('app.rls_mode','',true)"
                ),
                {"u": str(uid or uuid.uuid4()), "t": str(tid), "r": role},
            )
            return await fn(s)


APP = create_app()
CUR = {"p": None}
APP.dependency_overrides[get_principal] = lambda: CUR["p"]


async def api(client, path, principal):
    CUR["p"] = principal
    r = await client.get(path)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, None


def P(tid, role, uid):
    return Principal(
        user_id=str(uid),
        workos_id=f"w_{RUN}_{str(uid)[:8]}",
        email=f"{role}@cert.test",
        tenant_id=str(tid),
        role=role,
    )


# ── seed primitives ──────────────────────────────────────────────────────────
async def base(s, tid, slug, uid):
    CREATED_TENANTS.append(str(tid))
    await s.execute(
        text("INSERT INTO tenants (id,name,slug) VALUES (:i,:n,:s)"),
        {"i": tid, "n": slug, "s": slug},
    )
    await s.execute(
        text(
            "INSERT INTO users (id,workos_id,email,email_verified) "
            "VALUES (:i,:w,:e,true) ON CONFLICT DO NOTHING"
        ),
        {"i": uid, "w": f"w_{RUN}_{str(uid)[:8]}", "e": f"{str(uid)[:6]}@c.test"},
    )


async def uom(s, tid, name, utype):
    return (
        await s.execute(
            text(
                "INSERT INTO units_of_measure (tenant_id,name,abbreviation,unit_type) "
                "VALUES (:t,:n,:n,:u) RETURNING id"
            ),
            {"t": tid, "n": name, "u": utype},
        )
    ).scalar_one()


async def item(s, tid, u):
    return (
        await s.execute(
            text(
                "INSERT INTO inventory_items (tenant_id,name,inventory_mode,storage_unit_id,recipe_unit_id,"
                "storage_to_recipe_factor) VALUES (:t,:n,'recipe_deducted',:u,:u,1.0) RETURNING id"
            ),
            {"t": tid, "n": f"it-{uuid.uuid4().hex[:8]}", "u": u},
        )
    ).scalar_one()


async def menu(s, tid, item_id, mapped, unit="g"):
    mi = (
        await s.execute(
            text("INSERT INTO menu_items (tenant_id,name) VALUES (:t,:n) RETURNING id"),
            {"t": tid, "n": f"mi-{uuid.uuid4().hex[:8]}"},
        )
    ).scalar_one()
    rv = None
    if mapped:
        rid = (
            await s.execute(
                text(
                    "INSERT INTO recipes (tenant_id,menu_item_id,status) "
                    "VALUES (:t,:m,'confirmed') RETURNING id"
                ),
                {"t": tid, "m": mi},
            )
        ).scalar_one()
        rv = (
            await s.execute(
                text(
                    "INSERT INTO recipe_versions (tenant_id,recipe_id,version_number,"
                    "yield_quantity,name) VALUES (:t,:r,1,1.0,:n) RETURNING id"
                ),
                {"t": tid, "r": rid, "n": "rv"},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO recipe_ingredients (tenant_id,recipe_version_id,"
                "inventory_item_id,quantity,unit) VALUES (:t,:rv,:i,2,:un)"
            ),
            {"t": tid, "rv": rv, "i": item_id, "un": unit},
        )
        await s.execute(
            text("UPDATE menu_items SET recipe_version_id=:rv WHERE id=:m"), {"rv": rv, "m": mi}
        )
    return str(mi), (str(rv) if rv else None)


async def conn(s, tid, state, merchant, recon_age_s=600):
    cid = uuid.uuid4()
    await s.execute(
        text("""
        INSERT INTO tenant_pos_connections
          (connection_id,tenant_id,vendor,merchant_id,environment,access_token_enc,
           access_token_expires_at,refresh_token_enc,refresh_token_expires_at,state,
           last_reconciliation_at,updated_at)
        VALUES (:c,:t,'clover',:m,'sandbox','x',now()+interval '1 day','x',now()+interval '1 day',:st,
           now()-make_interval(secs => CAST(:ra AS int)), now()-interval '10 seconds')"""),
        {"c": cid, "t": tid, "m": merchant, "st": state, "ra": recon_age_s},
    )
    return cid


async def inbox(s, tid, cid, obj_type, state, recv_age_s, processed=False):
    iid = uuid.uuid4()
    r = await s.execute(
        text("""
        INSERT INTO pos_event_inbox
          (inbox_id,tenant_id,connection_id,vendor,vendor_event_id,vendor_object_type,
           vendor_event_type,vendor_ts,raw_payload,signature_verified,source,state,received_at,processed_at)
        VALUES (:i,:t,:c,'clover',:v,:ot,'UPDATE',0,'{}',false,'webhook',:st,
           now()-make_interval(secs => CAST(:ra AS int)),
           CASE WHEN :pr THEN now()-make_interval(secs => CAST(:ra AS int))+interval '1 min' ELSE NULL END)
        RETURNING received_at"""),
        {
            "i": iid,
            "t": tid,
            "c": cid,
            "v": f"E:{uuid.uuid4().hex[:10]}",
            "ot": obj_type,
            "st": state,
            "ra": recv_age_s,
            "pr": processed,
        },
    )
    return iid, r.scalar_one()


async def line(s, tid, inbox_id, menu_id, rv, dep_status, dep_reason, net_rev):
    oid = uuid.uuid4()
    await s.execute(
        text("""
        INSERT INTO orders (id,tenant_id,pos_event_inbox_id,clover_order_id,total_amount_cents,
           state,payment_state,closed_at,processed_at)
        VALUES (:o,:t,:i,:c,0,'locked','PAID',now()-interval '1 hour',now())"""),
        {"o": oid, "t": tid, "i": inbox_id, "c": f"o_{uuid.uuid4().hex[:8]}"},
    )
    sli = uuid.uuid4()
    await s.execute(
        text("""
        INSERT INTO sale_line_items
          (id,tenant_id,order_id,clover_line_item_id,menu_item_id,name_at_sale,quantity,
           price_cents_at_sale,net_revenue_cents,is_refunded,is_voided,recipe_version_id,
           depletion_status,depletion_reason)
        VALUES (:id,:t,:o,:c,:m,'L',1,0,:nr,false,false,:rv,:ds,:dr)"""),
        {
            "id": sli,
            "t": tid,
            "o": oid,
            "c": f"l_{uuid.uuid4().hex[:8]}",
            "m": menu_id,
            "nr": net_rev,
            "rv": rv,
            "ds": dep_status,
            "dr": dep_reason,
        },
    )
    return str(sli)


# specs: list of dicts {kind, rev}. kind in:
#   'depleted'  -> real process_line on a g-mapped line (expect status depleted)
#   'no_recipe' -> real process_line on an unmapped line (expect unmapped/no_recipe)
#   'missing'   -> real process_line on a ml-mapped line, item storage g (expect failed/missing_conversion)
#   'pending'   -> seed depletion_status='pending' (natural pre-processing state)
#   'unknown'   -> seed status='failed' reason='sale_ineligible' (PROJECTION-ONLY: artificial)
REAL_KINDS = {"depleted", "no_recipe", "missing"}


async def build_scenario(
    tid, uid, specs, with_revoked=False, with_catalog=False, historical_vs_current=False
):
    async def _seed(s):
        gu = await uom(s, tid, "g", "weight")
        vu = await uom(s, tid, "ml", "volume")
        probe = await item(s, tid, gu)
        it_g = probe
        it_ml = await item(s, tid, gu)  # storage g; recipe will demand ml -> missing conversion
        g_mi, g_rv = await menu(s, tid, it_g, True, "g")
        ml_mi, ml_rv = await menu(s, tid, it_ml, True, "ml")
        un_mi, _ = await menu(
            s, tid, None if True else 0, False
        )  # unmapped menu (no item ref needed)
        AC = await conn(s, tid, "active", f"ac-{RUN}-{str(tid)[:4]}")
        inb, order_ev = await inbox(s, tid, AC, "O", "processed", 3600, processed=True)
        cat_ev = None
        if with_catalog:
            _, cat_ev = await inbox(
                s, tid, AC, "I", "processed", 120, processed=True
            )  # newer catalog
        hist_unresolved = 0
        if with_revoked:
            RC = await conn(s, tid, "revoked", f"rc-{RUN}-{str(tid)[:4]}")
            await inbox(s, tid, RC, "O", "pending", 172800)
            await inbox(s, tid, RC, "O", "failed", 259200)
            hist_unresolved = 2
        real = []
        for sp in specs:
            k = sp["kind"]
            rev = sp["rev"]
            if k == "depleted":
                sli = await line(s, tid, inb, g_mi, g_rv, "pending", None, rev)
                real.append((sli, k))
            elif k == "no_recipe":
                sli = await line(s, tid, inb, un_mi, None, "pending", None, rev)
                real.append((sli, k))
            elif k == "missing":
                sli = await line(s, tid, inb, ml_mi, ml_rv, "pending", None, rev)
                real.append((sli, k))
            elif k == "pending":
                await line(s, tid, inb, g_mi, g_rv, "pending", None, rev)
            elif k == "unknown":
                await line(s, tid, inb, g_mi, g_rv, "failed", "sale_ineligible", rev)
            elif k == "comp_error":
                await line(s, tid, inb, g_mi, g_rv, "failed", "computation_error", rev)
        if historical_vs_current:
            # frozen historical miss (no_recipe) whose menu is CURRENTLY mapped:
            await line(s, tid, inb, g_mi, g_rv, "unmapped", "no_recipe", 0)
        return (
            probe,
            real,
            (order_ev.isoformat() if order_ev else None),
            (cat_ev.isoformat() if cat_ev else None),
            hist_unresolved,
        )

    probe, real, order_ev, cat_ev, hist = await svc(tid, _seed)
    # drive the REAL pipeline for real specs (each its own txn, like the worker)
    for sli, k in real:
        await svc(
            tid,
            lambda s, _sli=sli: handler.process_line(
                s,
                uuid.UUID(str(tid)),
                uuid.UUID(_sli),
                recorded_at=NOW_SALE,
                partial_refunds_enabled=True,
            ),
            uid=uid,
        )
    return str(probe), order_ev, cat_ev, hist


def dims(j):
    p = (j.get("pos", {}) if isinstance(j, dict) else {}).get("dimensions", {})
    return p if isinstance(p, dict) else {}


async def main():
    global NOW_SALE
    async with SM() as s0:
        async with s0.begin():
            NOW_SALE = (await s0.execute(text("SELECT now()-interval '1 hour'"))).scalar_one()
    transport = ASGITransport(app=APP)
    async with AsyncClient(transport=transport, base_url="http://cert") as client:
        # Each scenario: (specs, frozen expected recipe_mapping partition+status, opts)
        # Expected values are HAND-COMPUTED, frozen here BEFORE any endpoint call.
        SCEN = {
            # failure + unknown + pending + pass -> 'failures'
            "S1_fail_dominates": {
                "specs": [
                    {"kind": "depleted", "rev": 100},
                    {"kind": "no_recipe", "rev": 100},
                    {"kind": "pending", "rev": 100},
                    {"kind": "unknown", "rev": 100},
                ],
                "exp_status": "failures",
                "exp": {
                    "eligible": 4,
                    "with_recipe": 1,
                    "no_recipe": 1,
                    "invalid": 0,
                    "pending": 1,
                    "unknown": 1,
                },
            },
            # unknown + pending + pass, NO failure -> 'unknown'
            "S2_unknown": {
                "specs": [
                    {"kind": "depleted", "rev": 100},
                    {"kind": "pending", "rev": 100},
                    {"kind": "unknown", "rev": 100},
                ],
                "exp_status": "unknown",
                "exp": {
                    "eligible": 3,
                    "with_recipe": 1,
                    "no_recipe": 0,
                    "invalid": 0,
                    "pending": 1,
                    "unknown": 1,
                },
            },
            # pending only -> 'in_progress'
            "S3_in_progress": {
                "specs": [
                    {"kind": "depleted", "rev": 100},
                    {"kind": "pending", "rev": 100},
                    {"kind": "pending", "rev": 100},
                ],
                "exp_status": "in_progress",
                "exp": {
                    "eligible": 3,
                    "with_recipe": 1,
                    "no_recipe": 0,
                    "invalid": 0,
                    "pending": 2,
                    "unknown": 0,
                },
            },
            # all passed -> 'ok'
            "S4_ok": {
                "specs": [{"kind": "depleted", "rev": 100}, {"kind": "depleted", "rev": 100}],
                "exp_status": "ok",
                "exp": {
                    "eligible": 2,
                    "with_recipe": 2,
                    "no_recipe": 0,
                    "invalid": 0,
                    "pending": 0,
                    "unknown": 0,
                },
            },
            # zero denominator -> 'unavailable'
            "S5_unavailable": {
                "specs": [],
                "exp_status": "unavailable",
                "exp": {
                    "eligible": 0,
                    "with_recipe": 0,
                    "no_recipe": 0,
                    "invalid": 0,
                    "pending": 0,
                    "unknown": 0,
                },
            },
        }
        for name, sc in SCEN.items():
            try:
                T, U = uuid.uuid4(), uuid.uuid4()
                await svc(T, lambda s: base(s, T, f"{name}-{RUN}", U))
                probe, *_ = await build_scenario(T, U, sc["specs"])
                st, j = await api(
                    client, f"/api/v1/inventory/items/{probe}/insights?window=30d", P(T, "owner", U)
                )
                d = dims(j)
                rm = d.get("recipe_mapping", {})
                hw = rm.get("historical_window", {})
                e = sc["exp"]
                got = {
                    "eligible": hw.get("eligible_sale_line_count"),
                    "with_recipe": hw.get("with_recipe_count"),
                    "no_recipe": hw.get("no_recipe_count"),
                    "invalid": hw.get("invalid_recipe_count"),
                    "pending": hw.get("pending_count"),
                    "unknown": hw.get("unknown_count"),
                }
                # reconciliation identity on the ENDPOINT's own numbers
                den = got["eligible"]
                partsum = (
                    (got["with_recipe"] or 0)
                    + (got["no_recipe"] or 0)
                    + (got["invalid"] or 0)
                    + (got["pending"] or 0)
                    + (got["unknown"] or 0)
                )
                checks = {
                    "insights_200": st == 200,
                    "status_matches_frozen": rm.get("status") == sc["exp_status"],
                    "counts_match_frozen": got == e,
                    "reconciliation_identity": den is not None and partsum == den,
                }
                EV["scenarios"][name] = {
                    "frozen_expected_status": sc["exp_status"],
                    "frozen_expected_counts": e,
                    "endpoint_status": rm.get("status"),
                    "endpoint_counts": got,
                    "partition_sum": partsum,
                    "denominator": den,
                    "pipeline": "real(depleted/no_recipe) + natural pending + projection-only(unknown)",
                    "checks": checks,
                    "PASS": all(checks.values()),
                }
            except Exception as ex:
                import traceback

                EV["scenarios"][name] = {
                    "ERROR": f"{type(ex).__name__}: {ex}",
                    "tb": traceback.format_exc()[-600:],
                }
                EV["errors"].append(f"{name}: {ex}")

        # ── S6: conversion stage via REAL pipeline; missing_conversion must NOT be
        #    mislabeled as recipe-unmapped (recipe stage stays 'ok'). ──
        try:
            T, U = uuid.uuid4(), uuid.uuid4()
            await svc(T, lambda s: base(s, T, f"S6-{RUN}", U))
            probe, *_ = await build_scenario(
                T, U, [{"kind": "depleted", "rev": 100}, {"kind": "missing", "rev": 100}]
            )
            st, j = await api(
                client, f"/api/v1/inventory/items/{probe}/insights?window=30d", P(T, "owner", U)
            )
            d = dims(j)
            rm, cc = d.get("recipe_mapping", {}), d.get("conversion_coverage", {})
            # FROZEN (hand-computed): recipe stage sees 2 pass (depleted + missing both mapped),
            # conversion stage denominator=2, converted=1, missing=1, unknown=0 -> 'failures'.
            wr = cc.get("with_recipe_count")
            conv_sum = (
                (cc.get("converted_count") or 0)
                + (cc.get("missing_conversion_count") or 0)
                + (cc.get("unknown_count") or 0)
            )
            checks = {
                "insights_200": st == 200,
                "recipe_stage_ok_not_failures": rm.get("status") == "ok",
                "conversion_stage_failures": cc.get("status") == "failures",
                "converted_is_1": cc.get("converted_count") == 1,
                "missing_conversion_is_1": cc.get("missing_conversion_count") == 1,
                "conversion_reconciliation": wr is not None and conv_sum == wr == 2,
                "missing_not_mislabeled_unmapped": (
                    rm.get("historical_window", {}).get("no_recipe_count") == 0
                ),
            }
            EV["scenarios"]["S6_conversion_real_pipeline"] = {
                "pipeline": "real process_line: 1 depleted + 1 missing_conversion (ml recipe vs g storage)",
                "recipe_status": rm.get("status"),
                "conversion_status": cc.get("status"),
                "converted": cc.get("converted_count"),
                "missing": cc.get("missing_conversion_count"),
                "with_recipe_denominator": wr,
                "checks": checks,
                "PASS": all(checks.values()),
            }
        except Exception as ex:
            import traceback

            EV["scenarios"]["S6_conversion_real_pipeline"] = {
                "ERROR": f"{type(ex).__name__}: {ex}",
                "tb": traceback.format_exc()[-600:],
            }
            EV["errors"].append(f"S6: {ex}")

        # ── revenue adversarial (frozen expected for end_to_end) ──
        REV = {
            "R1_positive": {
                "specs": [
                    {"kind": "depleted", "rev": 750},
                    {"kind": "depleted", "rev": 750},
                    {"kind": "pending", "rev": 500},
                    {"kind": "pending", "rev": 500},
                ],
                "exp": {
                    "line": "50.0",
                    "rev": "60.0",
                    "eff": "50.0",
                    "applicable": True,
                    "elig_rev": 2500,
                    "dep_rev": 1500,
                    "status": "in_progress",
                },
            },
            "R2_zero": {
                "specs": [{"kind": "depleted", "rev": 0}, {"kind": "depleted", "rev": 0}],
                "exp": {
                    "line": "100.0",
                    "rev": None,
                    "eff": "100.0",
                    "applicable": False,
                    "elig_rev": 0,
                    "dep_rev": 0,
                    "status": "complete",
                },
            },
            "R3_negative": {
                "specs": [{"kind": "depleted", "rev": -1000}, {"kind": "depleted", "rev": 500}],
                "exp": {
                    "line": "100.0",
                    "rev": None,
                    "eff": "100.0",
                    "applicable": False,
                    "elig_rev": -500,
                    "dep_rev": -500,
                    "status": "complete",
                },
            },
            "R4_over_100_guard": {
                "specs": [{"kind": "depleted", "rev": 1000}, {"kind": "pending", "rev": -600}],
                "exp": {
                    "line": "50.0",
                    "rev": None,
                    "eff": "50.0",
                    "applicable": False,
                    "elig_rev": 400,
                    "dep_rev": 1000,
                    "status": "in_progress",
                },
            },
        }
        for name, sc in REV.items():
            try:
                T, U = uuid.uuid4(), uuid.uuid4()
                await svc(T, lambda s: base(s, T, f"{name}-{RUN}", U))
                probe, *_ = await build_scenario(T, U, sc["specs"])
                st, j = await api(
                    client, f"/api/v1/inventory/items/{probe}/insights?window=30d", P(T, "owner", U)
                )
                e2e = dims(j).get("end_to_end_coverage", {})
                e = sc["exp"]
                got = {
                    "line": e2e.get("line_coverage_pct"),
                    "rev": e2e.get("revenue_coverage_pct"),
                    "eff": e2e.get("effective_coverage_pct"),
                    "applicable": e2e.get("revenue_coverage_applicable"),
                    "elig_rev": e2e.get("eligible_net_revenue_cents"),
                    "dep_rev": e2e.get("depleted_net_revenue_cents"),
                    "status": e2e.get("status"),
                }
                rev_val = got["rev"]
                sane = rev_val is None or (0 <= float(rev_val) <= 100)
                checks = {
                    "insights_200": st == 200,
                    "matches_frozen": got == e,
                    "revenue_never_out_of_range": sane,
                }
                EV["scenarios"][name] = {
                    "frozen_expected": e,
                    "endpoint": got,
                    "checks": checks,
                    "PASS": all(checks.values()),
                }
            except Exception as ex:
                import traceback

                EV["scenarios"][name] = {
                    "ERROR": f"{type(ex).__name__}: {ex}",
                    "tb": traceback.format_exc()[-600:],
                }
                EV["errors"].append(f"{name}: {ex}")

        # ── current vs historical separation ──
        try:
            T, U = uuid.uuid4(), uuid.uuid4()
            await svc(T, lambda s: base(s, T, f"ISO-{RUN}", U))
            probe, order_ev, cat_ev, _hist = await build_scenario(
                T,
                U,
                [{"kind": "depleted", "rev": 100}],
                with_revoked=True,
                with_catalog=True,
                historical_vs_current=True,
            )
            st, j = await api(
                client, f"/api/v1/inventory/items/{probe}/insights?window=30d", P(T, "owner", U)
            )
            d = dims(j)
            ea, pr, cn = (
                d.get("event_activity", {}),
                d.get("processing", {}),
                d.get("connection", {}),
            )
            rm = d.get("recipe_mapping", {})
            hw, cc = rm.get("historical_window", {}), rm.get("current_catalog", {})
            checks = {
                "insights_200": st == 200,
                "only_active_connection_connected": cn.get("status") == "connected",
                "historical_unresolved_is_2": pr.get("historical_unresolved_event_count") == 2,
                "current_pending_zero": pr.get("pending_event_count") == 0,
                "current_failed_zero": pr.get("failed_event_count") == 0,
                "last_received_is_order_not_catalog": (
                    ea.get("latest_sales_data_received_at") == order_ev
                    and ea.get("latest_sales_data_received_at") != cat_ev
                ),
                # frozen historical miss present AND its menu currently mapped (fix != rewrite)
                "historical_frozen_miss_present": (hw.get("no_recipe_count") or 0) >= 1,
                "current_catalog_shows_mapped": (cc.get("menu_items_mapped") or 0) >= 1,
            }
            EV["scenarios"]["ISO_current_vs_historical"] = {
                "order_event_at": order_ev,
                "catalog_event_at": cat_ev,
                "endpoint_last_received": ea.get("latest_sales_data_received_at"),
                "historical_no_recipe_count": hw.get("no_recipe_count"),
                "current_catalog_mapped": cc.get("menu_items_mapped"),
                "historical_unresolved": pr.get("historical_unresolved_event_count"),
                "checks": checks,
                "PASS": all(checks.values()),
            }
        except Exception as ex:
            import traceback

            EV["scenarios"]["ISO_current_vs_historical"] = {
                "ERROR": f"{type(ex).__name__}: {ex}",
                "tb": traceback.format_exc()[-600:],
            }
            EV["errors"].append(f"ISO: {ex}")

        # ── S7: reconcile ALL FOUR stages exactly in one rich fixture ──
        # Hand-frozen partition: 6 eligible = depleted 1 + no_recipe 1 + missing 1
        #   + computation_error 1 + pending 1 + sale_ineligible(unknown) 1.
        #   recipe:     with_recipe 3 (depleted+missing+comp_err) + no_recipe 1 + inval 0 + pending 1 + unknown 1 = 6
        #   conversion: conv_pass 2 (depleted+comp_err) + missing 1 + unknown 0 = 3 (=with_recipe)
        #   depletion:  depleted 1 + dep_fail 1 (comp_err) + unknown 0 = 2 (=conv_pass)
        #   e2e:        depleted 1 + failures 3 (no_recipe+missing+comp_err) + pending 1 + recipe_unknown 1 = 6
        try:
            T, U = uuid.uuid4(), uuid.uuid4()
            await svc(T, lambda s: base(s, T, f"S7-{RUN}", U))
            probe, *_ = await build_scenario(
                T,
                U,
                [
                    {"kind": "depleted", "rev": 100},
                    {"kind": "no_recipe", "rev": 100},
                    {"kind": "missing", "rev": 100},
                    {"kind": "comp_error", "rev": 100},
                    {"kind": "pending", "rev": 100},
                    {"kind": "unknown", "rev": 100},
                ],
            )
            st, j = await api(
                client, f"/api/v1/inventory/items/{probe}/insights?window=30d", P(T, "owner", U)
            )
            d = dims(j)
            rm = d.get("recipe_mapping", {})
            hw = rm.get("historical_window", {})
            cc = d.get("conversion_coverage", {})
            de = d.get("depletion_execution", {})
            e2e = d.get("end_to_end_coverage", {})
            g = {
                "eligible": hw.get("eligible_sale_line_count"),
                "with_recipe": hw.get("with_recipe_count"),
                "no_recipe": hw.get("no_recipe_count"),
                "invalid": hw.get("invalid_recipe_count"),
                "r_pending": hw.get("pending_count"),
                "r_unknown": hw.get("unknown_count"),
                "c_denom": cc.get("with_recipe_count"),
                "converted": cc.get("converted_count"),
                "c_missing": cc.get("missing_conversion_count"),
                "c_unknown": cc.get("unknown_count"),
                "d_denom": de.get("convertible_count"),
                "depleted": de.get("depleted_count"),
                "d_fail": de.get("depletion_failure_count"),
                "d_unknown": de.get("unknown_count"),
                "e_eligible": e2e.get("eligible_sale_line_count"),
                "e_depleted": e2e.get("depleted_sale_line_count"),
                "e_failures": e2e.get("failure_count"),
                "e_pending": e2e.get("pending_line_count"),
                # e2e MUST expose its own unknown_line_count (the patched contract) —
                # the identity below uses ONLY end_to_end_coverage fields, no borrowing.
                "e_unknown": e2e.get("unknown_line_count"),
                "e_overlap": e2e.get("overlap_line_count"),
            }

            def _i(x):
                return x if isinstance(x, int) else -999

            recipe_id = (
                _i(g["with_recipe"])
                + _i(g["no_recipe"])
                + _i(g["invalid"])
                + _i(g["r_pending"])
                + _i(g["r_unknown"])
            )
            conv_id = _i(g["converted"]) + _i(g["c_missing"]) + _i(g["c_unknown"])
            dep_id = _i(g["depleted"]) + _i(g["d_fail"]) + _i(g["d_unknown"])
            e2e_id = (
                _i(g["e_depleted"]) + _i(g["e_failures"]) + _i(g["e_pending"]) + _i(g["e_unknown"])
            )
            frozen = {
                "eligible": 6,
                "with_recipe": 3,
                "no_recipe": 1,
                "invalid": 0,
                "r_pending": 1,
                "r_unknown": 1,
                "c_denom": 3,
                "converted": 2,
                "c_missing": 1,
                "c_unknown": 0,
                "d_denom": 2,
                "depleted": 1,
                "d_fail": 1,
                "d_unknown": 0,
                "e_eligible": 6,
                "e_depleted": 1,
                "e_failures": 3,
                "e_pending": 1,
                "e_unknown": 1,
                "e_overlap": 0,
            }
            # frozen expected STATUS per stage (all 'failures' — a known fail in each stage)
            checks = {
                "insights_200": st == 200,
                "all_counts_match_frozen": g == frozen,
                "recipe_reconciles_e2e_only": recipe_id == _i(g["eligible"]) == 6,
                "conversion_reconciles": conv_id == _i(g["c_denom"]) == 3,
                "depletion_reconciles": dep_id == _i(g["d_denom"]) == 2,
                "e2e_reconciles_from_e2e_fields_only": e2e_id == _i(g["e_eligible"]) == 6,
                "recipe_status_failures": rm.get("status") == "failures",
                "conversion_status_failures": cc.get("status") == "failures",
                "depletion_status_failures": de.get("status") == "failures",
                "e2e_status_failures": e2e.get("status") == "failures",
                "e2e_exposes_unknown_count": isinstance(g["e_unknown"], int),
                "e2e_exposes_overlap_count": isinstance(g["e_overlap"], int),
                "e2e_overlap_zero_when_valid": g["e_overlap"] == 0,
                "reason_breakdown_unknown": e2e.get("reason_breakdown", {}).get("UNKNOWN") == 1,
            }
            EV["scenarios"]["S7_all_stages_reconcile"] = {
                "frozen": frozen,
                "endpoint": g,
                "stage_statuses": {
                    "recipe": rm.get("status"),
                    "conversion": cc.get("status"),
                    "depletion": de.get("status"),
                    "e2e": e2e.get("status"),
                },
                "identities": {
                    "recipe": f"{recipe_id}==6",
                    "conversion": f"{conv_id}==3",
                    "depletion": f"{dep_id}==2",
                    "e2e_from_e2e_fields": f"{e2e_id}==6",
                },
                "checks": checks,
                "PASS": all(checks.values()),
            }
        except Exception as ex:
            import traceback

            EV["scenarios"]["S7_all_stages_reconcile"] = {
                "ERROR": f"{type(ex).__name__}: {ex}",
                "tb": traceback.format_exc()[-700:],
            }
            EV["errors"].append(f"S7: {ex}")

        # ── S8: verify RAW process_line output directly (status/reason/movements +
        #    movement identity), independent of insights. Each kind isolated. ──
        async def raw_state(kind):
            T, U = uuid.uuid4(), uuid.uuid4()
            await svc(T, lambda s: base(s, T, f"S8{kind}-{RUN}", U))
            probe, *_ = await build_scenario(T, U, [{"kind": kind, "rev": 100}])

            async def rd(s):
                r = (
                    (
                        await s.execute(
                            text(
                                "SELECT id, depletion_status ds, depletion_reason dr FROM sale_line_items "
                                "WHERE tenant_id=:t"
                            ),
                            {"t": T},
                        )
                    )
                    .mappings()
                    .one()
                )
                movs = (
                    (
                        await s.execute(
                            text(
                                "SELECT id, tenant_id tid, inventory_item_id iid, movement_type mt, delta, "
                                "source_type stype, source_id sid, COALESCE(idempotency_key,'') ik "
                                "FROM inventory_movements "
                                "WHERE tenant_id=:t AND movement_type IN ('sale_depletion','sale_signal')"
                            ),
                            {"t": T},
                        )
                    )
                    .mappings()
                    .all()
                )
                return {
                    "sli": str(r["id"]),
                    "ds": r["ds"],
                    "dr": r["dr"],
                    "movs": [dict(m) for m in movs],
                }

            return T, U, probe, await svc(T, rd)

        try:
            Td, Ud, probe_d, dep = await raw_state("depleted")
            _, _, _, nr = await raw_state("no_recipe")
            _, _, _, mc = await raw_state("missing")
            dm = dep["movs"][0] if len(dep["movs"]) == 1 else None
            # replay process_line on the SAME depleted line -> must stay exactly 1, identical id
            replay_err = None
            try:
                await svc(
                    Td,
                    lambda s: handler.process_line(
                        s,
                        uuid.UUID(str(Td)),
                        uuid.UUID(dep["sli"]),
                        recorded_at=NOW_SALE,
                        partial_refunds_enabled=True,
                    ),
                    uid=Ud,
                )
            except Exception as re:
                replay_err = f"{type(re).__name__}: {re}"

            async def after(s):
                return (
                    (
                        await s.execute(
                            text(
                                "SELECT id, delta, COALESCE(idempotency_key,'') ik FROM inventory_movements "
                                "WHERE tenant_id=:t AND movement_type='sale_depletion'"
                            ),
                            {"t": Td},
                        )
                    )
                    .mappings()
                    .all()
                )

            after_movs = [dict(m) for m in await svc(Td, after)]
            checks = {
                "depleted_status_reason": dep["ds"] == "depleted" and dep["dr"] is None,
                "depleted_exactly_one_movement": len(dep["movs"]) == 1,
                "movement_type_sale_depletion": dm and dm["mt"] == "sale_depletion",
                "movement_delta_is_minus_2": dm and Decimal(str(dm["delta"])) == Decimal("-2"),
                "movement_item_matches_probe": dm and str(dm["iid"]) == probe_d,
                "movement_tenant_matches": dm and str(dm["tid"]) == str(Td),
                # EXACT identity: source_id is the tested sale line; key is scoped
                # to that exact line id; source_type names the sale line.
                "movement_source_id_is_tested_line": dm and str(dm["sid"]) == dep["sli"],
                "movement_source_type_sale_line": dm and dm["stype"] == "sale_line_item",
                "movement_key_scoped_to_line": dm
                and dm["ik"].startswith(f"sale_line:{dep['sli']}:"),
                "no_recipe_status_reason": nr["ds"] == "unmapped" and nr["dr"] == "no_recipe",
                "no_recipe_no_movement": len(nr["movs"]) == 0,
                "missing_status_reason": mc["ds"] == "failed" and mc["dr"] == "missing_conversion",
                "missing_no_movement": len(mc["movs"]) == 0,
                "replay_no_duplicate_exactly_one": len(after_movs) == 1,
                "replay_same_movement_id": dm
                and len(after_movs) == 1
                and str(after_movs[0]["id"]) == str(dm["id"]),
                "replay_no_error": replay_err is None,
            }
            EV["scenarios"]["S8_pipeline_raw_state"] = {
                "depleted": {
                    "status": dep["ds"],
                    "reason": dep["dr"],
                    "movement": dm
                    and {
                        "type": dm["mt"],
                        "delta": str(dm["delta"]),
                        "source_id": str(dm["sid"]),
                        "idempotency_key": dm["ik"],
                        "item_matches": str(dm["iid"]) == probe_d,
                    },
                },
                "no_recipe": {"status": nr["ds"], "reason": nr["dr"], "movements": len(nr["movs"])},
                "missing_conversion": {
                    "status": mc["ds"],
                    "reason": mc["dr"],
                    "movements": len(mc["movs"]),
                },
                "replay": {"movement_count_after": len(after_movs), "error": replay_err},
                "note": "raw movements queried directly; exactly-one asserted; replay proves no duplicate",
                "checks": checks,
                "PASS": all(bool(v) for v in checks.values()),
            }
        except Exception as ex:
            import traceback

            EV["scenarios"]["S8_pipeline_raw_state"] = {
                "ERROR": f"{type(ex).__name__}: {ex}",
                "tb": traceback.format_exc()[-700:],
            }
            EV["errors"].append(f"S8: {ex}")

        # ── S9: end-to-end UNKNOWN partition (the defect S7 exposed). On a9cfb5a
        #    this FAILS (no unknown_line_count, status falls to none/partial);
        #    post-patch it PASSES. Unknown-only fixture. ──
        try:
            T, U = uuid.uuid4(), uuid.uuid4()
            await svc(T, lambda s: base(s, T, f"S9-{RUN}", U))
            probe, *_ = await build_scenario(
                T, U, [{"kind": "unknown", "rev": 100}, {"kind": "unknown", "rev": 100}]
            )
            st, j = await api(
                client, f"/api/v1/inventory/items/{probe}/insights?window=30d", P(T, "owner", U)
            )
            e2e = dims(j).get("end_to_end_coverage", {})
            uc = e2e.get("unknown_line_count")
            oc = e2e.get("overlap_line_count")
            rb = e2e.get("reason_breakdown", {})
            parts = [
                e2e.get("depleted_sale_line_count"),
                e2e.get("failure_count"),
                e2e.get("pending_line_count"),
                uc,
                oc,
            ]
            identity_ok = all(isinstance(x, int) for x in parts) and (
                parts[0] + parts[1] + parts[2] + parts[3]
            ) == e2e.get("eligible_sale_line_count")
            # reason + forecast-blocker behaviour for unknown lines (patched contract)
            reason_codes = {
                r.get("code") for r in (j.get("reasons", []) if isinstance(j, dict) else [])
            }
            fe = dims(j).get("forecast_eligibility", {})
            blocker_codes = {b.get("code") for b in fe.get("blockers", [])}
            checks = {
                "insights_200": st == 200,
                "unknown_line_count_present": isinstance(uc, int),
                "unknown_line_count_is_2": uc == 2,
                "overlap_line_count_zero": oc == 0,
                "status_is_unknown_not_none": e2e.get("status") == "unknown",
                "reason_breakdown_UNKNOWN_2": rb.get("UNKNOWN") == 2,
                "e2e_partition_closes": identity_ok,
                "no_negative_counts": all(isinstance(x, int) and x >= 0 for x in parts),
                "unknown_sale_lines_reason": "UNKNOWN_SALE_LINES" in reason_codes,
                "not_blamed_on_recipe": "RECIPE_COVERAGE_FAILURES" not in reason_codes,
                "unknown_forecast_blocker": "UNKNOWN_SALE_LINES" in blocker_codes,
            }
            EV["scenarios"]["S9_e2e_unknown_not_hidden"] = {
                "endpoint_status": e2e.get("status"),
                "unknown_line_count": uc,
                "overlap_line_count": oc,
                "reason_breakdown_UNKNOWN": rb.get("UNKNOWN"),
                "reason_codes": sorted(c for c in reason_codes if c),
                "forecast_blockers": sorted(c for c in blocker_codes if c),
                "eligible": e2e.get("eligible_sale_line_count"),
                "note": "unknown-only fixture; on pre-patch SHAs expect FAIL (defect), post-patch PASS",
                "checks": checks,
                "PASS": all(checks.values()),
            }
        except Exception as ex:
            import traceback

            EV["scenarios"]["S9_e2e_unknown_not_hidden"] = {
                "ERROR": f"{type(ex).__name__}: {ex}",
                "tb": traceback.format_exc()[-700:],
            }
            EV["errors"].append(f"S9: {ex}")

        # ── CROSS: real foreign-tenant item -> 404, AND random uuid -> 404 (separate) ──
        try:
            TA, UA = uuid.uuid4(), uuid.uuid4()
            TB, UB = uuid.uuid4(), uuid.uuid4()
            await svc(TA, lambda s: base(s, TA, f"CROSSA-{RUN}", UA))
            await svc(TB, lambda s: base(s, TB, f"CROSSB-{RUN}", UB))
            probeA, *_ = await build_scenario(TA, UA, [{"kind": "depleted", "rev": 100}])
            # tenant B principal fetches tenant A's REAL item
            stB_real, _ = await api(
                client, f"/api/v1/inventory/items/{probeA}/insights?window=30d", P(TB, "owner", UB)
            )
            stB_rand, _ = await api(
                client,
                f"/api/v1/inventory/items/{uuid.uuid4()}/insights?window=30d",
                P(TB, "owner", UB),
            )
            # tenant A owner can see its own item (control)
            stA_own, _ = await api(
                client, f"/api/v1/inventory/items/{probeA}/insights?window=30d", P(TA, "owner", UA)
            )
            checks = {
                "foreign_real_item_404": stB_real == 404,
                "random_uuid_404": stB_rand == 404,
                "own_item_200_control": stA_own == 200,
            }
            EV["scenarios"]["CROSS_tenant_isolation"] = {
                "foreign_real_item_status": stB_real,
                "random_uuid_status": stB_rand,
                "own_item_control_status": stA_own,
                "note": "real cross-tenant item AND random uuid asserted separately",
                "checks": checks,
                "PASS": all(checks.values()),
            }
        except Exception as ex:
            import traceback

            EV["scenarios"]["CROSS_tenant_isolation"] = {
                "ERROR": f"{type(ex).__name__}: {ex}",
                "tb": traceback.format_exc()[-700:],
            }
            EV["errors"].append(f"CROSS: {ex}")

    EXPECTED_SCENARIO_NAMES = {
        "S1_fail_dominates",
        "S2_unknown",
        "S3_in_progress",
        "S4_ok",
        "S5_unavailable",
        "S6_conversion_real_pipeline",
        "R1_positive",
        "R2_zero",
        "R3_negative",
        "R4_over_100_guard",
        "ISO_current_vs_historical",
        "S7_all_stages_reconcile",
        "S8_pipeline_raw_state",
        "S9_e2e_unknown_not_hidden",
        "CROSS_tenant_isolation",
    }
    names_present = set(EV["scenarios"])
    EV["expected_scenarios"] = sorted(EXPECTED_SCENARIO_NAMES)
    EV["all_expected_present"] = names_present == EXPECTED_SCENARIO_NAMES
    EV["missing_scenarios"] = sorted(EXPECTED_SCENARIO_NAMES - names_present)
    EV["all_pass"] = (
        names_present == EXPECTED_SCENARIO_NAMES
        and all(EV["scenarios"][n].get("PASS") is True for n in EXPECTED_SCENARIO_NAMES)
        and not EV["errors"]
    )
    print("===CERT_JSON_START===")
    print(json.dumps(EV, indent=2, default=str))
    print("===CERT_JSON_END===")


async def _cleanup() -> None:
    """Deterministically remove every tenant this run created (CASCADE). Best-effort
    per tenant so one failure doesn't strand the rest; leaves staging clean."""
    if not CREATED_TENANTS:
        return
    deleted = 0
    for tid in CREATED_TENANTS:
        try:
            async with SM() as s:
                async with s.begin():
                    await s.execute(text("SELECT set_config('app.tenant_id',:t,true)"), {"t": tid})
                    await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
            deleted += 1
        except Exception as exc:
            print(f"CLEANUP WARN: tenant {tid[:8]}: {type(exc).__name__}", file=sys.stderr)
    print(f"CLEANUP: removed {deleted}/{len(CREATED_TENANTS)} cert tenants", file=sys.stderr)


async def _run() -> bool:
    _guard()
    try:
        await main()
    finally:
        await _cleanup()
    return bool(EV.get("all_pass"))


if __name__ == "__main__":
    _ok = asyncio.run(_run())
    sys.exit(0 if _ok else 1)
