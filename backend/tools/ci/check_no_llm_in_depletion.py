#!/usr/bin/env python3
"""CI guard — no LLM provider (or draft-table access) in the depletion path.

Enforces Sprint 5 v5 **fail-gate #1** (no LLM anywhere in the depletion path) and **decision 4**
(depletion materialises from `*_versions`; it never imports or queries `recipe_drafts` /
`modifier_drafts`, which are operator scratch). The depletion engine's determinism is the
product's core claim — an LLM import, even transitive three hops deep, or a draft-table read
would make that claim false. This guard is the ONLY enforcement of that boundary, so it is
built to two standards at once:

  (1) Catch violations, including MULTI-HOP transitive imports — a grep would pass while
      `depletion/x -> app.foo -> app.bar -> anthropic` smuggles a provider in three hops deep.
  (2) Never cry wolf on docstrings/comments that merely *mention* the names — a guard that
      false-positives gets skip-flagged, and a skip-flagged guard is worse than none because
      everyone still believes the protection stands.

STATIC ANALYSIS ONLY: every file is parsed with `ast`, never imported or executed. So the
guard can never itself import a provider SDK and never touches the network. The import walk
follows PROJECT-INTERNAL (`app.*`) modules transitively and treats third-party packages as
leaves checked against the prohibited list — it does NOT recurse into site-packages (which
would be slow, fragile, and could false-positive on a library that optionally imports a
provider deep in its own internals). `ast.walk` visits nested nodes too, so a lazy import
hidden inside a function body is caught, not just module-level imports.

Run: ``python tools/ci/check_no_llm_in_depletion.py``  → exits 1 on any violation, printing
the offending file and, for transitive hits, the full import chain so it is fixable from the
message alone.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# ── Prohibited surfaces ──────────────────────────────────────────────────────────
# v5 fail-gate #1. EXTEND HERE when a new LLM provider dependency enters the repo
# (e.g. 'google-generativeai', 'litellm', 'cohere'): one obvious line, never a hardcode
# buried in the walk logic.
PROHIBITED_PROVIDERS: frozenset[str] = frozenset({"anthropic", "openai"})
# decision 4: the draft tables are operator scratch; depletion reads only *_versions.
PROHIBITED_DRAFT_TABLES: tuple[str, ...] = ("recipe_drafts", "modifier_drafts")

_BACKEND = Path(__file__).resolve().parents[2]
_APP_ROOT = _BACKEND / "app"
_DEPLETION = _APP_ROOT / "modules" / "inventory" / "depletion"


# ── Module/path resolution (static; no imports) ──────────────────────────────────


def _module_name(path: Path, base: Path) -> str:
    rel = path.relative_to(base).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_internal(modname: str, base: Path) -> Path | None:
    """Map a dotted `app.*` module name to its file, or None for a third-party/leaf name."""
    if not modname or modname.split(".")[0] != "app":
        return None
    parts = modname.split(".")
    as_module = base.joinpath(*parts).with_suffix(".py")
    if as_module.exists():
        return as_module
    as_package = base.joinpath(*parts, "__init__.py")
    if as_package.exists():
        return as_package
    return None


def _imported_names(path: Path, base: Path) -> list[str]:
    """All absolute dotted import targets in a file (module-level AND nested/lazy).

    For `from X import a, b` yields X plus X.a / X.b (so a submodule import resolves and a
    name import simply fails resolution → treated as a leaf). Relative imports are resolved
    to absolute against the file's package."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mod_dotted = _module_name(path, base)
    pkg_parts = mod_dotted.split(".")
    if path.name != "__init__.py" and len(pkg_parts) > 1:
        pkg_parts = pkg_parts[:-1]  # the file's containing package

    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:  # relative → absolute
                up = node.level - 1
                base_parts = pkg_parts[: len(pkg_parts) - up] if up else pkg_parts
                mod = ".".join([*base_parts, node.module]) if node.module else ".".join(base_parts)
            else:
                mod = node.module or ""
            if mod:
                out.append(mod)
                out.extend(f"{mod}.{alias.name}" for alias in node.names)
    return out


# ── Scans ────────────────────────────────────────────────────────────────────────


def scan_llm_paths(seeds: list[Path], base: Path) -> list[list[str]]:
    """Return one import chain per LLM-provider reachability (direct or transitive) from any
    of `seeds`. Walks `app.*` internally; third-party names are leaves checked against
    PROHIBITED_PROVIDERS."""
    violations: list[list[str]] = []
    visited: set[Path] = set()

    def walk(path: Path, chain: list[str]) -> None:
        if path in visited:
            return
        visited.add(path)
        for name in _imported_names(path, base):
            if name.split(".")[0] in PROHIBITED_PROVIDERS:
                violations.append([*chain, name])
                continue
            target = _resolve_internal(name, base)
            if target is not None:
                walk(target, [*chain, name])

    for seed in seeds:
        walk(seed, [_module_name(seed, base)])
    return violations


def scan_llm(depletion_dir: Path, base: Path) -> list[list[str]]:
    """LLM-provider reachability from any module under `depletion_dir`."""
    return scan_llm_paths(sorted(depletion_dir.rglob("*.py")), base)


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """ids of the Constant nodes that are module/class/function docstrings (to be exempted —
    a docstring mentioning a draft table is documentation, not a query)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def scan_drafts(depletion_dir: Path) -> list[tuple[Path, str]]:
    """Return (file, table) for each draft-table reference in code — string literals that are
    NOT docstrings (e.g. SQL text), and imports of a draft module. Comments are invisible to
    the AST and docstrings are exempted, so documentation that mentions the tables passes."""
    hits: list[tuple[Path, str]] = []
    for path in sorted(depletion_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        doc_ids = _docstring_constant_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in doc_ids
            ):
                for tbl in PROHIBITED_DRAFT_TABLES:
                    if tbl in node.value:
                        hits.append((path, tbl))
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in PROHIBITED_DRAFT_TABLES or (
                        node.module and node.module.split(".")[-1] in PROHIBITED_DRAFT_TABLES
                    ):
                        hits.append((path, alias.name))
    return hits


# ── Entry point ──────────────────────────────────────────────────────────────────


# Sprint 6 D-606-07: the COMMIT path must also be LLM-free — an extracted value
# reaches the ledger only through a human commit, never an inline model call. The
# commit logic lives in inventory/services.py (commit_receipt).
_COMMIT_MODULE = _APP_ROOT / "modules" / "inventory" / "services.py"


def main() -> int:
    llm = scan_llm(_DEPLETION, _BACKEND)
    commit_llm = scan_llm_paths([_COMMIT_MODULE], _BACKEND) if _COMMIT_MODULE.exists() else []
    drafts = scan_drafts(_DEPLETION)
    if not llm and not commit_llm and not drafts:
        print("OK: depletion + commit paths are free of LLM imports; no draft-table access.")
        return 0
    for chain in llm:
        print(
            "FAIL [LLM in depletion path, v5 fail-gate #1]: " + " -> ".join(chain),
            file=sys.stderr,
        )
    for chain in commit_llm:
        print(
            "FAIL [LLM in commit path, D-606-07]: " + " -> ".join(chain),
            file=sys.stderr,
        )
    for path, tbl in drafts:
        rel = path.relative_to(_BACKEND)
        print(
            f"FAIL [depletion reads a draft table, decision 4]: {rel} references {tbl!r} "
            "(depletion must read only *_versions)",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
