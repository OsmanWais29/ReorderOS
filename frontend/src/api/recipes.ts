// Typed API client for the Sprint 5 Recipes surface (Appendix F.3).
//
// Thin wrappers over the REAL backend routes (verified against app/modules/recipes/
// router.py + modifiers_router.py, all under the /api/v1 prefix). NOTE: the appendix's
// endpoint table listed `POST /onboarding/recipes/suggest`, but the implemented route is
// per-menu-item `POST /onboarding/recipes/{menu_item_id}/suggest` — the client follows the
// real route, since that is what has to resolve at runtime.
//
// No new endpoints are invented here.
//
// TYPE-PIN: the response types below are hand-mirrored from app/modules/recipes/schemas.py
// (the source of truth). Unlike the unit list there is no cheap CI assertion for type drift,
// so the enforcement backstop is the server's own validation (a shape mismatch surfaces as a
// runtime parse/usage error, not silent corruption). If type drift ever bites, FastAPI's
// /openapi.json supports generated TS types (e.g. openapi-typescript) — recorded as the future
// option; not built now (codegen toolchain isn't worth it for this surface yet).

import { API_BASE } from '../auth/config';
import { tenantHeader } from './activeTenant';

// ── Error type: lets the UI branch on 409 (confirmed) / 400 (zero ingredients) ──────────
export class RecipeApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'RecipeApiError';
    this.status = status;
    this.detail = detail;
  }
}

async function req<T>(token: string, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}/api/v1${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      ...tenantHeader(), // X-Tenant-Id — required by resolve_principal (400 without)
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new RecipeApiError(res.status, body.detail ?? `Request failed (${res.status})`);
  }
  // 202/204 (sync-menu) may have no JSON body.
  if (res.status === 204) return undefined as T;
  return (await res.json().catch(() => undefined)) as T;
}

// ── Types (mirror app/modules/recipes/schemas.py) ───────────────────────────────────────
export type RecipeStatus = 'none' | 'draft' | 'confirmed' | 'skipped';
export type ModifierStatus = 'draft' | 'confirmed' | 'skipped';
export type ModifierType = 'additive' | 'subtractive' | 'substitution';
export type Confidence = 'confident' | 'likely' | 'uncertain';

export type IngredientIn = {
  name: string;
  quantity: number;
  unit: string;
  inventory_item_id?: string | null;
};

export type RecipeListItem = {
  menu_item_id: string;
  name: string;
  active: boolean;
  status: RecipeStatus;
  ingredient_count: number;
  modifier_count: number;
  volume_30d: number;
};

export type RecipeDetail = {
  menu_item_id: string;
  name: string;
  status: RecipeStatus;
  ingredients: IngredientIn[];
};

export type Progress = {
  total: number;
  confirmed: number;
  skipped: number;
  denominator: number;
  percent: number | null; // null when denominator is 0
};

export type SuggestionIngredient = {
  name: string;
  quantity: number;
  unit: string;
  valid: boolean; // false when unit non-canonical / name empty / qty<=0 — operator fixes
  issue: string | null;
};

export type ModifierSuggestionOut = {
  modifier_id: string | null;
  name: string;
  confidence: Confidence;
  ingredients: SuggestionIngredient[];
};

export type SuggestResponse = {
  suggestion_id: string;
  menu_item_id: string;
  base_ingredients: SuggestionIngredient[];
  confidence: Confidence;
  modifiers: ModifierSuggestionOut[];
  model_version: string;
};

export type ModifierListItem = {
  modifier_id: string;
  name: string;
  modifier_type: ModifierType;
  status: ModifierStatus;
  ingredient_count: number;
};

export type ModifierDetail = {
  modifier_id: string;
  name: string;
  modifier_type: ModifierType;
  status: ModifierStatus;
  ingredients: IngredientIn[];
};

// ── Recipes (§5) ────────────────────────────────────────────────────────────────────────
export const getRecipes = (token: string) =>
  req<RecipeListItem[]>(token, '/onboarding/recipes');

export const getRecipe = (token: string, menuItemId: string) =>
  req<RecipeDetail>(token, `/onboarding/recipes/${menuItemId}`);

/** Auto-save edits. Throws RecipeApiError(409) when the recipe is currently confirmed. */
export const patchRecipe = (token: string, menuItemId: string, ingredients: IngredientIn[]) =>
  req<RecipeDetail>(token, `/onboarding/recipes/${menuItemId}`, {
    method: 'PATCH',
    body: JSON.stringify({ ingredients }),
  });

/** Throws RecipeApiError(400) when the draft has zero ingredients. */
export const confirmRecipe = (token: string, menuItemId: string) =>
  req<RecipeDetail>(token, `/onboarding/recipes/${menuItemId}/confirm`, { method: 'POST' });

export const unconfirmRecipe = (token: string, menuItemId: string) =>
  req<RecipeDetail>(token, `/onboarding/recipes/${menuItemId}/unconfirm`, { method: 'POST' });

export const skipRecipe = (token: string, menuItemId: string) =>
  req<RecipeDetail>(token, `/onboarding/recipes/${menuItemId}/skip`, { method: 'POST' });

export const suggest = (token: string, menuItemId: string) =>
  req<SuggestResponse>(token, `/onboarding/recipes/${menuItemId}/suggest`, { method: 'POST' });

export const getProgress = (token: string) =>
  req<Progress>(token, '/onboarding/progress');

// ── Modifiers (§3) ──────────────────────────────────────────────────────────────────────
export const getModifiers = (token: string, menuItemId: string) =>
  req<ModifierListItem[]>(token, `/onboarding/recipes/${menuItemId}/modifiers`);

/** Read-only modifier detail (Staff+) — used to display/edit a saved modifier and re-hydrate
 * a draft after app-close. The read-only partner of getRecipe; pure read (no unskip/409). */
export const getModifier = (token: string, menuItemId: string, modifierId: string) =>
  req<ModifierDetail>(token, `/onboarding/recipes/${menuItemId}/modifiers/${modifierId}`);

export const patchModifier = (
  token: string,
  menuItemId: string,
  modifierId: string,
  ingredients: IngredientIn[],
) =>
  req<ModifierDetail>(token, `/onboarding/recipes/${menuItemId}/modifiers/${modifierId}`, {
    method: 'PATCH',
    body: JSON.stringify({ ingredients }),
  });

export const confirmModifier = (token: string, menuItemId: string, modifierId: string) =>
  req<ModifierDetail>(
    token,
    `/onboarding/recipes/${menuItemId}/modifiers/${modifierId}/confirm`,
    { method: 'POST' },
  );

export const unconfirmModifier = (token: string, menuItemId: string, modifierId: string) =>
  req<ModifierDetail>(
    token,
    `/onboarding/recipes/${menuItemId}/modifiers/${modifierId}/unconfirm`,
    { method: 'POST' },
  );

export const skipModifier = (token: string, menuItemId: string, modifierId: string) =>
  req<ModifierDetail>(
    token,
    `/onboarding/recipes/${menuItemId}/modifiers/${modifierId}/skip`,
    { method: 'POST' },
  );

// ── Menu sync (§4) ──────────────────────────────────────────────────────────────────────
export const syncMenu = (token: string) =>
  req<void>(token, '/pos/clover/sync-menu', { method: 'POST' });
