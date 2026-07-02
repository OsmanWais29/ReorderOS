"""i18n completeness guard (Sprint 5 Phase 16, Appendix F.4 / FE-8).

WHOLE-TREE scan with a frozen, shrink-only legacy exclusion list — NOT an allowlist of "new"
files. The allowlist pattern fails silently: a future screen that forgets to add itself ships
unscanned with the guard green. Inverting it (scan everything, exclude a known-dirty legacy
set) means new files are covered by default; the only way to escape the guard is to be on the
explicit exclusion list, and that list can only SHRINK (a test asserts each excluded file is
still genuinely dirty, so cleaning one forces its removal — the legacy debt drains, never grows).

Checks (mechanical, no renderer):
  1. EN/FR key PARITY (Charter of the French Language / Bill 96 — NOT Law 25, which is privacy).
  2. USED ⊆ DEFINED — every t.<key> a scanned file references is defined.
  3. NO BARE LITERALS — no hardcoded user-facing string in a scanned file.
  4. EXCLUSIONS SHRINK-ONLY — every legacy-excluded file still has a violation (else remove it).
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_FE = _ROOT / "frontend"
_STRINGS_TS = _FE / "src" / "i18n" / "strings.ts"

# Frozen legacy exclusions — files that predate the bilingual discipline and hardcode English.
# SHRINK-ONLY: remove a path here when its file is migrated into strings.ts; never add to it
# (a new file with bare literals is a bug to fix, not an exclusion to grow). Tracked as
# DEBT-i18n-legacy (Bill 96), scheduled before the first French-speaking pilot.
_LEGACY_EXCLUDED: frozenset[str] = frozenset(
    {
        "app/(app)/home.tsx",
        "app/(app)/more.tsx",
        "app/onboarding/connecting.tsx",
        "app/onboarding/found-summary.tsx",
        "app/onboarding/sign-in.tsx",
        "app/onboarding/welcome.tsx",
    }
)

_PROP_RE = re.compile(
    r"(?:placeholder|accessibilityLabel)\s*=\s*[\"']([^\"']*[A-Za-z]{2,}[^\"']*)[\"']"
)
# JSX text between > and < that is purely word-like; (?<!=) drops `=> Promise<unknown>`.
_JSX_TEXT_RE = re.compile(r"(?<!=)>\s*([A-Za-z][A-Za-z .,!?'-]*[A-Za-z])\s*<")


def _all_tsx() -> list[Path]:
    return sorted(p for d in ("app", "src") for p in (_FE / d).rglob("*.tsx"))


def _rel(p: Path) -> str:
    return str(p.relative_to(_FE))


def _bare_literals(src: str) -> list[str]:
    return _PROP_RE.findall(src) + _JSX_TEXT_RE.findall(src)


def _scanned() -> list[Path]:
    return [p for p in _all_tsx() if _rel(p) not in _LEGACY_EXCLUDED]


def _locale_keys(block: str) -> set[str]:
    return set(re.findall(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*:", block, re.M))


def _locale_blocks() -> tuple[set[str], set[str]]:
    text = _STRINGS_TS.read_text(encoding="utf-8")
    en = re.search(r"\ben\s*:\s*\{(.*?)\n\s*\},\s*\n\s*fr\s*:", text, re.S)
    fr = re.search(r"\bfr\s*:\s*\{(.*?)\n\s*\},\s*\n\}\s*as const", text, re.S)
    assert en and fr, "could not isolate en/fr blocks in strings.ts"
    return _locale_keys(en.group(1)), _locale_keys(fr.group(1))


def test_en_fr_string_keys_are_at_parity() -> None:
    en_keys, fr_keys = _locale_blocks()
    assert en_keys == fr_keys, (
        "EN/FR string keys are not at parity (FE-8 / Bill 96) — "
        f"EN-only={sorted(en_keys - fr_keys)}, FR-only={sorted(fr_keys - en_keys)}"
    )


def test_scanned_screens_keys_used_are_defined() -> None:
    en_keys, _ = _locale_blocks()
    used: set[str] = set()
    for p in _scanned():
        used |= set(re.findall(r"(?<![A-Za-z0-9_.])t\.([A-Za-z][A-Za-z0-9_]*)", p.read_text()))
    undefined = used - en_keys
    assert not undefined, f"screens reference undefined i18n keys: {sorted(undefined)}"


def test_scanned_screens_have_no_bare_user_facing_literals() -> None:
    offenders: list[str] = []
    for p in _scanned():
        for hit in _bare_literals(p.read_text(encoding="utf-8")):
            offenders.append(f"{_rel(p)}: {hit!r}")
    assert not offenders, (
        "hardcoded user-facing literals (route through strings.ts, or add to the legacy "
        "exclusion list ONLY if genuinely legacy):\n" + "\n".join(offenders)
    )


def test_legacy_exclusions_are_shrink_only() -> None:
    """Every excluded file must (a) exist and (b) still contain a bare literal. A cleaned file
    that stays on the list fails here — forcing removal, so the exclusion set can only shrink."""
    stale: list[str] = []
    for rel in sorted(_LEGACY_EXCLUDED):
        f = _FE / rel
        if not f.exists():
            stale.append(f"{rel}: no longer exists — remove from the exclusion list")
        elif not _bare_literals(f.read_text(encoding="utf-8")):
            stale.append(f"{rel}: now clean — remove from the exclusion list (debt drained)")
    assert not stale, "legacy exclusion list must shrink:\n" + "\n".join(stale)
