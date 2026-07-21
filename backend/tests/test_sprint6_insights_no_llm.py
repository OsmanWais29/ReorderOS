"""CI guard — Stock Insights derives values ONLY from persisted deterministic
data. No LLM/provider import or call may be reachable from the insights modules,
even transitively. Reuses the proven static (ast, never-import) walker from the
depletion CI guard so this test never itself imports a provider SDK.

Insights is a deterministic calculator over inventory_movements / sale_line_items
/ POS ingest state; an LLM anywhere in its reachable graph would make the
"computed, not invented" claim false.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_GUARD_PATH = Path(__file__).resolve().parents[1] / "tools" / "ci" / "check_no_llm_in_depletion.py"
_spec = importlib.util.spec_from_file_location("_depletion_guard", _GUARD_PATH)
assert _spec is not None and _spec.loader is not None
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

_APP = Path(__file__).resolve().parents[1] / "app"
_INSIGHTS_MODULES = [
    _APP / "modules" / "inventory" / "insights.py",
    _APP / "modules" / "inventory" / "insights_schemas.py",
    _APP / "modules" / "inventory" / "balance_projection.py",
]


def test_insights_modules_have_no_llm_reachable() -> None:
    for m in _INSIGHTS_MODULES:
        assert m.exists(), m
    chains = guard.scan_llm_paths(_INSIGHTS_MODULES, _APP.parent)
    assert chains == [], f"LLM/provider reachable from insights: {chains}"


def test_guard_would_catch_a_provider_import(tmp_path: Path) -> None:
    # Prove the guard still bites for these seeds — a fake insights module that
    # imports a provider (even lazily) must be flagged, so a real leak can't slip.
    fake = tmp_path / "app" / "modules" / "inventory"
    fake.mkdir(parents=True)
    (fake / "insights.py").write_text("def f():\n    import anthropic\n    return anthropic\n")
    chains = guard.scan_llm_paths([fake / "insights.py"], tmp_path)
    assert chains and chains[0][-1] == "anthropic"


if __name__ == "__main__":  # allow running as a standalone CI step
    raise SystemExit(pytest.main([__file__, "-q"]))
