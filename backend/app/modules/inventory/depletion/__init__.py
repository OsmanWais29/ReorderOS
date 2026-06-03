"""Deterministic inventory depletion (Sprint 5).

This package is the recipe-walk + depletion engine. It contains NO LLM imports
(directly or transitively) and never reads draft tables — both invariants are
enforced by the Phase 14 CI guard (tools/ci/check_no_llm_in_depletion.py).
"""
