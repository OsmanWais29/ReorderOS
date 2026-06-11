# Sprint 5 — Phase 14 notes (CI depletion-isolation guard)

> Enforcement infrastructure: `tools/ci/check_no_llm_in_depletion.py`. Closes gate 33; enforces
> fail-gate #1. Per-phase-note convention (see phase-13 notes). No migration.
>
> Why this guard gets disproportionate scrutiny: every other guard in this sprint protects
> against *doing the wrong thing* (double depletion, clobbered pointers, premature `depleted`).
> This one protects against *becoming the wrong architecture* — an LLM in the depletion path
> isn't a bug, it's the product's core determinism claim becoming false (v5 made it fail-gate
> #1 for that reason). It must outlive everyone's memory of why it exists, so it's built to
> catch three hops deep, never cry wolf on docstrings, and stay fast enough to never get
> skipped.

## N1 — Two layers, static AST only

The fail-gate says "direct **or** transitive," so the guard does both, as **static analysis**
(parse with `ast`, never import/execute — so the guard can never itself import a provider SDK
and never touches the network):

- **Direct:** every `import`/`from` in each `depletion/` module is checked against
  `PROHIBITED_PROVIDERS`. `ast.walk` visits nested nodes, so a **lazy import inside a function
  body** (the Phase 5 `llm_client` pattern) is caught too — not just module-level imports.
- **Transitive:** the import graph is walked from every `depletion/` module through
  **project-internal (`app.*`) modules only**, transitively; **third-party packages are leaves
  checked against the prohibited list, not recursed into.** A grep would pass while
  `depletion/x → app.foo → app.bar → anthropic` smuggles a provider in three hops deep; an
  unbounded walk into `site-packages` would be slow and could false-positive on a library that
  optionally imports a provider deep in its internals — and slow/flaky guards get disabled,
  the same death as false positives by a different route.

## N2 — Precision is what keeps the guard installed

**A guard with false positives gets skip-flagged, and a skip-flagged guard is worse than none**
— everyone still believes the protection stands. So detection is AST-level, firing on *code
that does the thing*, never *text that mentions the thing*:

- **LLM:** actual import nodes (not substring matches on file text).
- **Draft tables (decision 4):** non-docstring string literals containing `recipe_drafts` /
  `modifier_drafts` (e.g. SQL passed to `text(...)`), plus imports of a draft module.
  **Docstrings are exempted and comments are invisible to the AST**, so the invariant-
  documentation that already exists in depletion code (`"...never reads recipe_drafts..."`)
  passes. `session.execute(text("SELECT ... FROM recipe_drafts"))` fails; a docstring saying
  "depletion never reads recipe_drafts" passes. That distinction is mechanical at the AST
  level and impossible at the grep level — which is the whole reason the guard is AST-based.

## N3 — Five sensitivity tests (both failure modes)

Four pick conditions where a **missed violation** shows; the fifth picks where **over-firing**
shows. Both failure modes need a test sensitive to them:

1. clean real tree passes (`scan_llm` + `scan_drafts` empty, `main()==0`);
2. direct provider import caught (`import anthropic`, `from openai import ...`);
3. transitive **three-hop** import caught (synthetic `seed → app.foo → app.bar → anthropic`),
   asserting the reported chain preserves the hop order;
4. draft-table query caught (`text("... FROM recipe_drafts")`);
5. **false-positive guard:** a depletion file with the table names in a docstring + comment
   **passes**.

(Plus: lazy-import-in-a-function caught; draft-module import caught; `PROHIBITED_PROVIDERS` is a
named constant.)

## N4 — Extension point + failure message

- `PROHIBITED_PROVIDERS` is a **named `frozenset` constant** with a comment pointing at
  fail-gate #1 — when a new provider dependency (`google-generativeai`, `litellm`, `cohere`)
  enters the repo, extending the guard is one obvious line, not a missed hardcode buried in the
  walk. This is the difference between a guard that ages well and one that silently narrows as
  the ecosystem moves.
- The failure message names the offending file and, for transitive hits, the **full hop path**
  (`depletion/x.py → app.foo → app.bar → anthropic`), so a violation is fixable from the
  message alone.

## N5 — Enforcement now; CI placement is a separate, deferred decision

The guard is enforced **today** by `tests/test_sprint5_phase14_ci_guard.py` — the suite runs in
CI, so a violation fails CI from the moment this phase lands. **A guard that exists but isn't
run is documentation, not enforcement**; the pytest gives the enforcement property immediately.

A standalone pre-commit hook / dedicated CI step (run `python tools/ci/check_no_llm_in_depletion.py`
before the full suite, for a faster fail) is a **placement** optimization that touches CI
config — left to the operator to wire where it fits (`.pre-commit-config.yaml`, a Makefile
target, or the deploy pipeline). **This phase does not modify CI config.**
