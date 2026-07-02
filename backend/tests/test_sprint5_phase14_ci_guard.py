"""Sprint 5 Phase 14 — CI depletion-isolation guard (tools/ci/check_no_llm_in_depletion.py).

Design: backend/docs/sprints/sprint-5-phase-14-notes.md. Enforces v5 fail-gate #1 (no LLM in
the depletion path) + decision 4 (no draft-table access). This is the ONLY enforcement of the
three-layer truth model's hardest boundary, so the guard's OWN quality gets the same
sensitivity discipline as everything since factor≠1:

  FOUR tests pick conditions where a MISSED violation shows — clean tree passes; a direct
  import is caught; a transitive three-hop import is caught; a draft-table query is caught.
  The FIFTH picks the condition where OVER-FIRING shows — a docstring/comment merely mentioning
  a draft table must PASS. A guard that false-positives gets skip-flagged, and a skip-flagged
  guard is worse than none.

The guard is STATIC (ast parse, never import), so these tests never import a provider SDK or
touch the network — they analyse synthetic source trees on disk.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_GUARD_PATH = Path(__file__).resolve().parents[1] / "tools" / "ci" / "check_no_llm_in_depletion.py"
_spec = importlib.util.spec_from_file_location("_depletion_guard", _GUARD_PATH)
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _mk(base: Path, relpath: str, content: str) -> Path:
    p = base / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _depletion(base: Path) -> Path:
    d = base / "app" / "modules" / "inventory" / "depletion"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── 1. clean tree passes (the REAL depletion package) ─────────────────────────────


def test_clean_real_tree_passes() -> None:
    assert guard.scan_llm(guard._DEPLETION, guard._BACKEND) == []
    assert guard.scan_drafts(guard._DEPLETION) == []
    assert guard.main() == 0


# ── 2. a DIRECT provider import is caught ─────────────────────────────────────────


def test_direct_llm_import_caught(tmp_path) -> None:
    dep = _depletion(tmp_path)
    _mk(dep, "seed.py", "import anthropic\n")
    chains = guard.scan_llm(dep, tmp_path)
    assert len(chains) == 1
    assert chains[0][-1] == "anthropic"

    # the `from openai import ...` form too
    dep2 = _depletion(tmp_path / "b")
    _mk(dep2, "seed.py", "from openai import OpenAI\n")
    chains2 = guard.scan_llm(dep2, tmp_path / "b")
    assert any(c[-1].split(".")[0] == "openai" for c in chains2)


# ── 3. a TRANSITIVE three-hop import is caught (the grep-can't-catch case) ─────────


def test_transitive_three_hop_import_caught(tmp_path) -> None:
    dep = _depletion(tmp_path)
    _mk(dep, "seed.py", "import app.foo\n")
    _mk(tmp_path, "app/foo.py", "import app.bar\n")
    _mk(tmp_path, "app/bar.py", "import anthropic\n")  # three hops from the depletion seed

    chains = guard.scan_llm(dep, tmp_path)
    assert len(chains) == 1
    chain = chains[0]
    assert chain[-1] == "anthropic"
    # the full hop path is reported so the violation is fixable from the message alone
    assert "app.foo" in chain and "app.bar" in chain
    assert chain.index("app.foo") < chain.index("app.bar") < chain.index("anthropic")


def test_lazy_import_inside_function_caught(tmp_path) -> None:
    """ast.walk visits nested nodes, so a provider hidden in a function body (the Phase 5
    lazy-import pattern) is caught, not just module-level imports."""
    dep = _depletion(tmp_path)
    _mk(dep, "seed.py", "def f():\n    import anthropic\n    return anthropic\n")
    chains = guard.scan_llm(dep, tmp_path)
    assert chains and chains[0][-1] == "anthropic"


# ── 4. a draft-table QUERY is caught ──────────────────────────────────────────────


def test_draft_table_query_caught(tmp_path) -> None:
    dep = _depletion(tmp_path)
    _mk(dep, "seed.py", 'from sqlalchemy import text\nq = text("SELECT id FROM recipe_drafts")\n')
    hits = guard.scan_drafts(dep)
    assert len(hits) == 1
    assert hits[0][1] == "recipe_drafts"


def test_draft_module_import_caught(tmp_path) -> None:
    dep = _depletion(tmp_path)
    _mk(dep, "seed.py", "from app.modules.recipes import modifier_drafts\n")
    hits = guard.scan_drafts(dep)
    assert any(tbl == "modifier_drafts" for _, tbl in hits)


# ── 5. the FALSE-POSITIVE guard: docstrings/comments mentioning the tables PASS ───


def test_docstring_and_comment_mentions_pass(tmp_path) -> None:
    """A docstring or comment that mentions a draft table is DOCUMENTATION, not a query — it
    must pass, or the guard cries wolf and gets skip-flagged. Mirrors the real invariant-
    documentation comments in depletion code."""
    dep = _depletion(tmp_path)
    _mk(
        dep,
        "seed.py",
        '"""This module never reads recipe_drafts — depletion materialises *_versions."""\n'
        "# NOTE: modifier_drafts is operator scratch; do not query it here.\n"
        "X = 1\n",
    )
    assert guard.scan_drafts(dep) == []


def test_prohibited_providers_is_a_named_constant() -> None:
    """The extension point (fail-gate #1) is an obvious named constant, not a buried hardcode."""
    assert "anthropic" in guard.PROHIBITED_PROVIDERS
    assert "openai" in guard.PROHIBITED_PROVIDERS
    assert isinstance(guard.PROHIBITED_PROVIDERS, frozenset)
