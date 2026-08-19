"""Static (AST) LINT: detect enumerated post-commit session operations that run without an
intervening RLS-context re-establishment.

`get_rls_session` sets context with `SET LOCAL`, which reverts at the first `commit()`. Under
the non-bypassrls request role (`reorderos_app`) a post-commit read/write then runs with EMPTY
context → 0 rows / blocked WITH CHECK → 500. This lint fails the build if such a pattern is
(re)introduced.

Scope + explicit limits (this is a LINT, not a proof):
  - DETECTS, after `await <s>.commit()` and before a re-establishment of THAT session's context:
      * `await <s>.{execute,scalar,scalars,flush,refresh,get,delete,merge}(…)`, including
        nested forms like `(await <s>.execute(…)).scalar_one()`;
      * any `await helper(… <s> …)` that passes the committed session to a call (repo/helper).
  - Context re-establishment that clears the flag: a named setter
    (`set_rls_context`/`set_identity_read_context`/`set_identity_context`/`set_accept_invite_context`)
    applied to THAT session, or `<s>.execute(text("… set_config …"))`.
  - Per-session: resetting session A does NOT clear a still-committed session B.
  - LIMITS: it is NOT path-sensitive (branches are flattened), does not resolve aliasing, and
    treats any session-passing call conservatively (may over-flag a helper that receives but
    does not use the session post-commit). It does NOT prove every tenant-scoped request
    restores context — the restricted-role HTTP/integration tests are the primary evidence;
    this only catches the enumerated direct patterns.
"""

from __future__ import annotations

import ast
import pathlib

_CONTEXT_SETTERS = {
    "set_rls_context",
    "set_identity_read_context",
    "set_identity_context",
    "set_accept_invite_context",
}
_SESSION_OPS = {"execute", "scalar", "scalars", "flush", "refresh", "get", "delete", "merge"}

_ROOTS = [pathlib.Path("app/modules"), pathlib.Path("app/core")]


def _iter_body(stmts: list[ast.stmt]) -> list[ast.stmt]:
    """Statements in execution order, descending into with/if/try/for/while blocks."""
    out: list[ast.stmt] = []
    for s in stmts:
        out.append(s)
        for field in ("body", "orelse", "finalbody"):
            block = getattr(s, field, None)
            if isinstance(block, list):
                out.extend(_iter_body(block))
        for handler in getattr(s, "handlers", []) or []:
            out.extend(_iter_body(handler.body))
    return out


_BLOCK_FIELDS = {"body", "orelse", "finalbody", "handlers"}


def _awaited_calls(stmt: ast.stmt) -> list[ast.Call]:
    """Awaited Calls in the statement's OWN expressions (catches nested forms like
    `(await db.execute(…)).scalar_one()`), but NOT inside nested block bodies — those are
    yielded separately by _iter_body, so descending here would double-count and misorder."""
    out: list[ast.Call] = []
    for field, value in ast.iter_fields(stmt):
        if field in _BLOCK_FIELDS:
            continue
        for child in value if isinstance(value, list) else [value]:
            if isinstance(child, ast.AST):
                for sub in ast.walk(child):
                    if isinstance(sub, ast.Await) and isinstance(sub.value, ast.Call):
                        out.append(sub.value)
    return out


def _session_method(call: ast.Call) -> tuple[str, str] | None:
    """(session_name, method) if call is `<name>.<method>(…)`, else None."""
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return f.value.id, f.attr
    return None


def _plain_name(call: ast.Call) -> str | None:
    return call.func.id if isinstance(call.func, ast.Name) else None


def _sql_text(call: ast.Call) -> str:
    return " ".join(
        n.value for n in ast.walk(call) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )


def _arg_names(call: ast.Call) -> set[str]:
    names = {a.id for a in call.args if isinstance(a, ast.Name)}
    names |= {k.value.id for k in call.keywords if isinstance(k.value, ast.Name)}
    return names


_REQUEST_CONTEXT_MARKERS = _CONTEXT_SETTERS | {"get_rls_session"}


def _establishes_request_context(fn: ast.AST) -> bool:
    """In scope only if the function establishes REQUEST-scoped RLS context — it depends on
    get_rls_session or calls a context setter. Service/worker paths (service_worker sessions,
    USING(true) policies) don't set SET-LOCAL request context, so post-commit reversion is a
    non-issue for them and they are intentionally out of scope."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id in _REQUEST_CONTEXT_MARKERS:
            return True
        if isinstance(node, ast.Attribute) and node.attr in _REQUEST_CONTEXT_MARKERS:
            return True
    return False


def _violations_in_function(fn: ast.AST, path: str) -> list[str]:
    """Scoped: only audit functions that establish request RLS context."""
    if not _establishes_request_context(fn):
        return []
    return _detect(fn, path)


def _detect(fn: ast.AST, path: str) -> list[str]:
    """Core detection (no scoping) — used directly by the self-tests."""
    found: list[str] = []
    committed: set[str] = set()  # session names committed and not re-established
    fname = getattr(fn, "name", "?")
    for stmt in _iter_body(getattr(fn, "body", [])):
        for call in _awaited_calls(stmt):
            sm = _session_method(call)
            name = _plain_name(call)
            # 1) commit → session becomes context-less
            if sm and sm[1] == "commit":
                committed.add(sm[0])
                continue
            # 2) named context setter re-establishes ITS session (first Name arg)
            if name in _CONTEXT_SETTERS:
                for a in call.args:
                    if isinstance(a, ast.Name):
                        committed.discard(a.id)
                        break
                continue
            # 3) session method op
            if sm:
                s, method = sm
                if method == "execute" and "set_config" in _sql_text(call):
                    committed.discard(s)  # explicit context re-set on this session
                    continue
                if method in _SESSION_OPS and s in committed:
                    found.append(
                        f"{path}:{stmt.lineno} {fname}() post-commit {s}.{method}(…) "
                        f"without RLS re-set"
                    )
                    committed.discard(s)
                continue
            # 4) helper/repo call receiving a still-committed session
            hit = _arg_names(call) & committed
            if hit:
                found.append(
                    f"{path}:{stmt.lineno} {fname}() post-commit "
                    f"{name or '<call>'}(… {sorted(hit)[0]} …) without RLS re-set"
                )
                committed.discard(sorted(hit)[0])
    return found


def _violations_in_source(src: str, path: str = "<snippet>", *, scoped: bool = False) -> list[str]:
    """Parse `src` and return violations. scoped=True applies the request-context scope
    (real-code scan); scoped=False tests raw detection (self-tests)."""
    tree = ast.parse(src, filename=path)
    fn_analyze = _violations_in_function if scoped else _detect
    out: list[str] = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
            out.extend(fn_analyze(fn, path))
    return out


def test_no_post_commit_db_op_without_rls_reset() -> None:
    """The real app tree (request-context-establishing functions only) must be clean of the
    enumerated post-commit patterns."""
    violations: list[str] = []
    for root in _ROOTS:
        for py in sorted(root.rglob("*.py")):
            violations.extend(_violations_in_source(py.read_text(), str(py), scoped=True))
    assert not violations, "post-commit session op(s) without RLS re-establishment:\n" + "\n".join(
        violations
    )


def test_scope_excludes_service_path_without_request_context() -> None:
    """A function that never establishes request context (a service/worker path) is out of
    scope even with a post-commit op — its session isn't a SET-LOCAL request session."""
    service = (
        "async def worker(s):\n"
        "    await s.commit()\n"
        "    await s.execute(text('SELECT 1'))\n"  # no get_rls_session / setter → out of scope
    )
    assert _violations_in_source(service, scoped=True) == []
    assert _violations_in_source(service, scoped=False)  # raw detection DOES see it


# ── biting self-tests: every enumerated bad pattern MUST be detected ──────────
_BAD = {
    "wrong_session": (
        "async def wrong_session(db, other, principal):\n"
        "    await db.commit()\n"
        "    await set_rls_context(other, principal)\n"  # resets OTHER, not db
        "    await db.execute(text('SELECT 1'))\n"
    ),
    "scalar_after_commit": (
        "async def scalar_after_commit(db):\n"
        "    await db.commit()\n"
        "    await db.scalar(text('SELECT 1'))\n"
    ),
    "nested_execute": (
        "async def nested_execute(db):\n"
        "    await db.commit()\n"
        "    value = (await db.execute(text('SELECT 1'))).scalar_one()\n"
    ),
    "helper_after_commit": (
        "async def helper_after_commit(db):\n"
        "    await db.commit()\n"
        "    await arbitrary_repo_write(db)\n"
    ),
}


def test_audit_detects_every_enumerated_pattern() -> None:
    for label, src in _BAD.items():
        assert _violations_in_source(src), f"audit failed to detect: {label}"


def test_audit_allows_correct_per_session_reset() -> None:
    # resetting the RIGHT session before the op is clean…
    ok = (
        "async def right(db, principal):\n"
        "    await db.commit()\n"
        "    await set_rls_context(db, principal)\n"
        "    await db.execute(text('SELECT 1'))\n"
    )
    assert _violations_in_source(ok) == []
    # …and a raw set_config re-set on the same session is also clean.
    ok2 = (
        "async def right2(db):\n"
        "    await db.commit()\n"
        "    await db.execute(text(\"SELECT set_config('app.tenant_id', :t, true)\"))\n"
        "    await db.execute(text('SELECT 1'))\n"
    )
    assert _violations_in_source(ok2) == []
