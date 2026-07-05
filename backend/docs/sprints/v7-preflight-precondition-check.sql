-- ============================================================================
-- V7 sandbox sale — PRE-FLIGHT PRECONDITION CHECK (run BEFORE ringing the sale)
-- ----------------------------------------------------------------------------
-- Purpose: prove the chain CAN move stock, so a green depletion result actually
-- means something. The #1 false-green is selling an item whose recipe isn't
-- confirmed (recipe_version_id NULL) — it ingests, marks the event 'processed',
-- and moves ZERO stock while looking fine.
--
-- This replicates inventory/services.py::on_hand for Mode A (recipe_deducted),
-- which is the mode the recipe-confirm flow assigns to every ingredient
-- (recipes/repo.py:387). on_hand(ModeA) = SUM(delta) over all movement_types
-- EXCEPT sale_signal / sale_signal_reversal.
--
-- HOW TO RUN (against STAGING, doadmin URI — same connection you run staging_sim.py with):
--   psql "$STAGING_DB_URL" \
--        -v tenant_slug="'bluebird-cafe'" \
--        -v item_name="'Bluebird Café Classic Burger'" \
--        -f docs/sprints/v7-preflight-precondition-check.sql
--
-- Adjust tenant_slug to the slug you used in /auth/register-tenant, and item_name
-- to the EXACT menu item name as it synced from Clover (Query 1 lists candidates
-- if the exact name misses).
-- ============================================================================

\echo '== GATE 0: does the tenant exist, and what menu items synced? =='
SELECT t.id AS tenant_id, t.slug, t.name,
       mi.id AS menu_item_id, mi.name AS menu_item_name,
       mi.pos_item_id,
       (mi.recipe_version_id IS NOT NULL) AS recipe_confirmed
FROM tenants t
LEFT JOIN menu_items mi ON mi.tenant_id = t.id
WHERE t.slug = :tenant_slug
ORDER BY mi.name;

\echo ''
\echo '== GATES 1-4: top-level readiness for the target burger =='
WITH tgt AS (
    SELECT mi.*
    FROM menu_items mi
    JOIN tenants t ON t.id = mi.tenant_id
    WHERE t.slug = :tenant_slug
      AND mi.name = :item_name
    LIMIT 1
)
SELECT
    (SELECT count(*) FROM tgt) = 1                               AS gate1_menu_item_found,
    (SELECT pos_item_id IS NOT NULL FROM tgt)                    AS gate2_pos_item_synced,
    (SELECT recipe_version_id IS NOT NULL FROM tgt)              AS gate3_recipe_confirmed,
    COALESCE((SELECT count(*) FROM recipe_ingredients ri
              WHERE ri.recipe_version_id = (SELECT recipe_version_id FROM tgt)), 0) >= 1
                                                                 AS gate4_has_ingredients,
    (SELECT recipe_version_id FROM tgt)                          AS recipe_version_id,
    (SELECT yield_quantity FROM recipe_versions
      WHERE id = (SELECT recipe_version_id FROM tgt))            AS yield_quantity;

\echo ''
\echo '== GATE 5: per-ingredient stock + expected deduction per 1 burger =='
\echo '   PASS row = mode is recipe_deducted AND on_hand > expected deduction.'
WITH tgt AS (
    SELECT mi.tenant_id, mi.recipe_version_id
    FROM menu_items mi
    JOIN tenants t ON t.id = mi.tenant_id
    WHERE t.slug = :tenant_slug AND mi.name = :item_name
    LIMIT 1
),
rv AS (
    SELECT id, yield_quantity FROM recipe_versions
    WHERE id = (SELECT recipe_version_id FROM tgt)
),
ing AS (
    SELECT ri.inventory_item_id,
           ii.name              AS ingredient,
           ii.inventory_mode    AS mode,
           ri.quantity          AS recipe_qty,
           ri.unit              AS recipe_unit,
           uom.name             AS storage_unit,
           (SELECT yield_quantity FROM rv) AS yield_quantity,
           -- on_hand, Mode A (recipe_deducted): straight ledger sum minus signals.
           -- Mode B is flagged separately below — the burger flow never produces it.
           COALESCE((
               SELECT SUM(m.delta)
               FROM inventory_movements m
               WHERE m.tenant_id = ri.tenant_id
                 AND m.inventory_item_id = ri.inventory_item_id
                 AND m.movement_type NOT IN ('sale_signal','sale_signal_reversal')
           ), 0) AS on_hand_mode_a
    FROM recipe_ingredients ri
    JOIN inventory_items ii ON ii.id = ri.inventory_item_id
    JOIN units_of_measure uom ON uom.id = ii.storage_unit_id
    WHERE ri.recipe_version_id = (SELECT recipe_version_id FROM tgt)
      AND ri.tenant_id = (SELECT tenant_id FROM tgt)
)
SELECT
    ingredient,
    mode,
    recipe_qty,
    recipe_unit,
    storage_unit,
    yield_quantity,
    on_hand_mode_a AS on_hand,
    -- Expected deduction per ONE burger, ONLY when recipe_unit = storage_unit.
    -- If they differ, depletion applies a unit conversion (see unit_conversions);
    -- this preview can't compute that, so it shows NULL and you verify post-sale.
    CASE WHEN recipe_unit = storage_unit
         THEN round(recipe_qty / NULLIF(yield_quantity,0), 6)
         ELSE NULL END AS expected_deduction_per_burger,
    -- Overall verdict for this ingredient
    CASE
        WHEN mode <> 'recipe_deducted'
            THEN 'CHECK: not Mode A — on_hand formula differs, verify manually'
        WHEN on_hand_mode_a <= 0
            THEN 'RED: no stock — add opening_balance before the sale'
        WHEN recipe_unit = storage_unit
             AND on_hand_mode_a <= recipe_qty / NULLIF(yield_quantity,0)
            THEN 'RED: stock <= one burger requirement'
        WHEN recipe_unit <> storage_unit
            THEN 'AMBER: unit conversion applies — on_hand>0 ok, verify delta post-sale'
        ELSE 'GREEN'
    END AS verdict
FROM ing
ORDER BY ingredient;

\echo ''
\echo '== If every Gate 1-5 row is GREEN, ring the sale. Re-run Query in the POST-SALE'
\echo '   trace doc to confirm pos_event_inbox=processed, sale_line_items=depleted,'
\echo '   and one negative inventory_movements row per ingredient =='
