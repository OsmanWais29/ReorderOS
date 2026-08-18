"""The local dev/test database coordinates are a repository CONTRACT.

The 2026-08-17 correction round found that "local PG" had silently been a different
server than the compose container (another service bound the same host port), so tests
were not exercising the engine they claimed to. The guard here is structural: the
compose service's DEFAULT host port, every localhost DSN in .env.example, and the test
fallback in tests/conftest.py must all name the SAME canonical port. Per-machine
conflicts are handled with the REORDEROS_DB_PORT override plus matching DATABASE_URL/
SERVICE_DATABASE_URL env vars — never by editing the committed default.
"""

from __future__ import annotations

import pathlib
import re

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_CANONICAL_PORT = "5433"


def test_compose_port_is_overridable_with_canonical_default() -> None:
    compose = (_BACKEND / "docker-compose.yml").read_text()
    match = re.search(r'"\$\{REORDEROS_DB_PORT:-(\d+)\}:5432"', compose)
    assert match, (
        "compose db host port must be per-machine overridable via "
        '`"${REORDEROS_DB_PORT:-<default>}:5432"` with a numeric committed default'
    )
    assert match.group(1) == _CANONICAL_PORT


def test_compose_pins_postgres_17() -> None:
    """Provider-shaped role-admin tests depend on PG 17 CREATEROLE semantics (and
    test_role_admin_provider.py fails loudly on any other major version)."""
    compose = (_BACKEND / "docker-compose.yml").read_text()
    assert re.search(r"image:\s*postgres:17", compose), "compose db image must be postgres:17"


def test_env_example_and_conftest_agree_with_compose_default() -> None:
    env_example = (_BACKEND / ".env.example").read_text()
    env_ports = set(re.findall(r"@localhost:(\d+)/", env_example))
    assert env_ports == {_CANONICAL_PORT}, (
        f".env.example localhost DSN port(s) {sorted(env_ports)} must all be the "
        f"canonical {_CANONICAL_PORT}"
    )
    conftest = (_BACKEND / "tests" / "conftest.py").read_text()
    match = re.search(r"@localhost:(\d+)/reorderos", conftest)
    assert match, "conftest.py must carry the localhost fallback DSN"
    assert match.group(1) == _CANONICAL_PORT
