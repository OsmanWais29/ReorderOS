// Active-tenant context for API clients.
//
// The backend REQUIRES `X-Tenant-Id` on every principal-resolving request
// (resolve_principal → 400 without it). AuthContext sets the id here after
// /auth/me resolves; every api/*.ts client attaches it via tenantHeader().
// Single-membership v1: the first tenant is the active one (the same choice
// more.tsx already made).

let activeTenantId: string | null = null;

export function setActiveTenantId(id: string | null): void {
  activeTenantId = id;
}

export function getActiveTenantId(): string | null {
  return activeTenantId;
}

export function tenantHeader(): Record<string, string> {
  return activeTenantId ? { 'X-Tenant-Id': activeTenantId } : {};
}
