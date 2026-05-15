#!/usr/bin/env python3
"""Live auth smoke test against the deployed DO API.

Usage
-----
Step 1 — get a real WorkOS JWT (pick one):

  A) WorkOS Dashboard → Users → open a user → "Impersonate" →
     copy the access_token from the resulting URL fragment.

  B) WorkOS API (requires your secret key + password auth enabled):
       export WORKOS_SECRET_KEY=sk_...
       python scripts/smoke_auth.py --get-token --email you@example.com --password yourpw

Step 2 — run the full smoke test with that token:
       python scripts/smoke_auth.py --token <paste_jwt_here>

Or combine both steps:
       WORKOS_SECRET_KEY=sk_... python scripts/smoke_auth.py \
         --get-token --email you@example.com --password yourpw
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

BASE_URL = "https://reorderos-api-7d4et.ondigitalocean.app"
WORKOS_CLIENT_ID = "client_01KQT0CRCZP8W8AMAYE5Y1F9SP"
WORKOS_AUTH_URL = "https://api.workos.com/user_management/authenticate"


def _parse(r: httpx.Response) -> object:
    try:
        return r.json()
    except Exception:
        return r.text[:200]


def ok(label: str, status: int, expected: int, body: object) -> bool:
    passed = status == expected
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}]  {label:55s}  HTTP {status}")
    if not passed:
        print(f"         expected {expected} — body: {json.dumps(body)[:200]}")
    return passed


def get_token(email: str, password: str) -> str:
    secret = os.environ.get("WORKOS_SECRET_KEY", "")
    if not secret:
        sys.exit("Set WORKOS_SECRET_KEY env var first")

    print(f"Authenticating {email} via WorkOS...")
    r = httpx.post(
        WORKOS_AUTH_URL,
        json={
            "client_id": WORKOS_CLIENT_ID,
            "client_secret": secret,
            "grant_type": "password",
            "email": email,
            "password": password,
        },
        timeout=10,
    )
    if r.status_code != 200:
        sys.exit(f"WorkOS auth failed ({r.status_code}): {r.text}")

    token: str = r.json()["access_token"]
    print(f"Got JWT ({len(token)} chars, first 40): {token[:40]}...\n")
    return token


def smoke_test(token: str) -> None:
    auth = {"Authorization": f"Bearer {token}"}
    passed = 0
    total = 0

    print(f"Target: {BASE_URL}\n")

    with httpx.Client(timeout=10) as c:

        print("── Sprint 1: health endpoints ───────────────────────────────")
        for path, method, exp in [
            ("/health/live", "GET", 200),
            ("/health/ready", "GET", 200),
            ("/version", "GET", 200),
        ]:
            r = c.request(method, BASE_URL + path)
            total += 1
            passed += ok(f"{method} {path}", r.status_code, exp, _parse(r))

        print("\n── Sprint 2: no token → 401 (not 503) ──────────────────────")
        for path, method in [
            ("/api/v1/auth/me", "GET"),
            ("/api/v1/auth/register-tenant", "POST"),
            ("/api/v1/tenants", "GET"),
            ("/api/v1/invitations", "GET"),
        ]:
            r = c.request(method, BASE_URL + path)
            total += 1
            passed += ok(f"{method} {path} (no token)", r.status_code, 401, _parse(r))

        print("\n── Sprint 2: bad/expired token → 401 ───────────────────────")
        for label, bad_token in [
            ("malformed JWT", "not.a.jwt"),
            ("unsigned alg:none", "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ1In0."),
        ]:
            r = c.get(
                BASE_URL + "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {bad_token}"},
            )
            total += 1
            passed += ok(f"GET /api/v1/auth/me ({label})", r.status_code, 401, _parse(r))

        print("\n── Sprint 2: valid token → 200 on /auth/me ──────────────────")
        r = c.get(BASE_URL + "/api/v1/auth/me", headers=auth)
        total += 1
        if r.status_code == 200:
            passed += ok("GET /api/v1/auth/me (authenticated)", r.status_code, 200, _parse(r))
            body = r.json()
            print(f"         email: {body['user']['email']}")
            print(f"         workos_id: {body['user']['workos_id']}")
            print(f"         tenants: {[t['slug'] for t in body.get('tenants', [])]}")
            tenants = body.get("tenants", [])
        elif r.status_code == 404:
            passed += ok(
                "GET /api/v1/auth/me (user not yet registered — call register-tenant)",
                r.status_code,
                404,
                _parse(r),
            )
            tenants = []
        else:
            ok("GET /api/v1/auth/me (authenticated)", r.status_code, 200, _parse(r))
            tenants = []

        if tenants:
            print("\n── Sprint 2: tenant-scoped RBAC ─────────────────────────────")
            tid = tenants[0]["id"]
            tenant_h = {**auth, "X-Tenant-Id": tid}

            r = c.get(BASE_URL + "/api/v1/tenants", headers=auth)
            total += 1
            passed += ok("GET /api/v1/tenants", r.status_code, 200, _parse(r))

            r = c.get(BASE_URL + "/api/v1/invitations", headers=tenant_h)
            total += 1
            if r.status_code in (200, 403):
                role_hint = "owner → 200" if r.status_code == 200 else "manager/staff → 403"
                passed += ok(
                    f"GET /api/v1/invitations ({role_hint})",
                    r.status_code, r.status_code, _parse(r)
                )
            else:
                ok("GET /api/v1/invitations", r.status_code, 200, _parse(r))

            r = c.patch(
                BASE_URL + "/api/v1/auth/active-tenant",
                json={"tenant_id": tid},
                headers=tenant_h,
            )
            total += 1
            passed += ok("PATCH /auth/active-tenant", r.status_code, 200, _parse(r))
            if r.status_code == 200:
                print(f"         role resolved: {r.json().get('role')}")
        else:
            print("\n  (skipping tenant-scoped tests — register a tenant first)")
            print(
                "  Run: curl -X POST "
                f"{BASE_URL}/api/v1/auth/register-tenant \\\n"
                '       -H "Authorization: Bearer <token>" \\\n'
                '       -H "Content-Type: application/json" \\\n'
                '       -d \'{"slug":"my-company","name":"My Company"}\''
            )

    print(f"\n{'=' * 65}")
    print(f"  {passed}/{total} checks passed")
    if passed == total:
        print("  ALL PASSED — Sprint 2 auth is live on DO")
    else:
        print("  SOME FAILED — see above")
    print("=" * 65)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", help="WorkOS JWT")
    parser.add_argument("--get-token", action="store_true")
    parser.add_argument("--email")
    parser.add_argument("--password")
    args = parser.parse_args()

    if args.get_token:
        if not args.email or not args.password:
            sys.exit("--get-token requires --email and --password")
        token = get_token(args.email, args.password)
    elif args.token:
        token = args.token
    else:
        parser.print_help()
        sys.exit(1)

    smoke_test(token)


if __name__ == "__main__":
    main()
