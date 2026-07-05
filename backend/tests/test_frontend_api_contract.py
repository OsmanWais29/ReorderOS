"""API contract guards (Sprint 5 Phase 16) — institutionalising two lessons as CI.

ROUTE PARITY — the single-modifier route set must mirror the single-recipe route set
verb-for-verb (GET detail, PATCH, confirm, unconfirm, skip). This is the test that would have
caught the missing modifier-detail GET at birth (the consumer-less-API gap): a write-only
config surface is incomplete by construction.

CLIENT ↔ SERVER PATH SYNC — every path the TS clients (frontend/src/api/*.ts) call must
resolve to a real FastAPI route. Catches a client built against a path the server doesn't
expose (a 404 in production) — the inverse failure of route parity.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.main import create_app

_ROOT = Path(__file__).resolve().parents[2]
_API_CLIENTS_DIR = _ROOT / "frontend" / "src" / "api"

_app = create_app()
_ROUTE_PATHS: set[str] = {getattr(r, "path", "") for r in _app.routes}


def _ops_under(single_resource_re: str) -> set[tuple[str, str]]:
    """(method, suffix) operations whose path matches the regex (one capture group = suffix)."""
    ops: set[tuple[str, str]] = set()
    pat = re.compile(single_resource_re)
    for r in _app.routes:
        path = getattr(r, "path", "")
        m = pat.match(path)
        if not m:
            continue
        suffix = m.group(1) or ""
        for verb in (getattr(r, "methods", set()) or set()) - {"HEAD", "OPTIONS"}:
            ops.add((verb, suffix))
    return ops


# The verb-for-verb core every single configurable resource must expose.
_CORE = {
    ("GET", ""),
    ("PATCH", ""),
    ("POST", "/confirm"),
    ("POST", "/unconfirm"),
    ("POST", "/skip"),
}


def test_single_recipe_exposes_the_core_verbs() -> None:
    recipe_ops = _ops_under(r"^/api/v1/onboarding/recipes/\{menu_item_id\}(/\w+)?$")
    assert _CORE <= recipe_ops, f"recipe surface missing core verbs: {_CORE - recipe_ops}"


def test_single_modifier_mirrors_recipe_verb_for_verb() -> None:
    """The gap-catcher: modifiers must expose the same core as recipes — including the read
    (GET detail) whose absence forced the write-as-read anti-pattern."""
    mod_ops = _ops_under(
        r"^/api/v1/onboarding/recipes/\{menu_item_id\}/modifiers/\{modifier_id\}(/\w+)?$"
    )
    assert _CORE <= mod_ops, (
        f"modifier surface does NOT mirror recipe verb-for-verb — missing: {_CORE - mod_ops}"
    )


def _snake(camel: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", camel).lower()


def _client_paths() -> set[str]:
    """Path templates every TS client under src/api/ calls, normalised to FastAPI route
    templates (`${itemId}` → `{item_id}` generically, so new clients need no test edit)."""
    out: set[str] = set()
    for ts in sorted(_API_CLIENTS_DIR.glob("*.ts")):
        src = ts.read_text(encoding="utf-8")
        # req<...>(token, `...`) or req<...>(token, '...') — capture the path (arg after token)
        for p in re.findall(r"req<[^>]*>\(\s*token,\s*[`'\"]([^`'\"]+)[`'\"]", src):
            p = re.sub(r"\$\{(\w+)\}", lambda m: "{" + _snake(m.group(1)) + "}", p)
            out.add(f"/api/v1{p}")
    return out


def test_client_paths_resolve_to_server_routes() -> None:
    client = _client_paths()
    assert client, "no client paths extracted from src/api/*.ts — the regex drifted"
    missing = {p for p in client if p not in _ROUTE_PATHS}
    assert not missing, (
        "TS client calls paths the server does not expose (production 404):\n"
        + "\n".join(sorted(missing))
    )
