"""i18n completeness guard (Sprint 5 Phase 16, Appendix F.4 / FE-8).

Three mechanical checks (no renderer needed) that together enforce "every user-facing string
in the new screens resolves through strings.ts with EN and FR present":

  1. EN/FR key PARITY — neither locale is missing a key (a string present in EN but absent in
     FR ships an English fragment to a French user; for a Québec pilot under Law 25 that is the
     axis where gaps show). Parity is key-presence, not translation quality.
  2. USED ⊆ DEFINED — every `t.<key>` referenced by the new screens exists in strings.ts (a
     reference to an undefined key is a silent runtime fallback).
  3. NO BARE LITERALS — the new screen files carry no hardcoded user-facing string (the legacy
     screens hardcode English; the new ones must not). Parity alone can't see a literal that
     never went through strings.ts at all.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_STRINGS_TS = _ROOT / "frontend" / "src" / "i18n" / "strings.ts"

# The Sprint-5 Phase-16 frontend files (the "new screens"); the bare-literal + used-key
# checks apply to these.
_NEW_FILES = [
    _ROOT / "frontend" / "app" / "onboarding" / "recipes.tsx",
    _ROOT / "frontend" / "src" / "components" / "IngredientRow.tsx",
    _ROOT / "frontend" / "src" / "components" / "RecipeBits.tsx",
    _ROOT / "frontend" / "src" / "components" / "MenuItemAccordion.tsx",
]


def _locale_keys(block: str) -> set[str]:
    return set(re.findall(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*:", block, re.M))


def _locale_blocks() -> tuple[set[str], set[str]]:
    text = _STRINGS_TS.read_text(encoding="utf-8")
    en = re.search(r"\ben\s*:\s*\{(.*?)\n\s*\},\s*\n\s*fr\s*:", text, re.S)
    fr = re.search(r"\bfr\s*:\s*\{(.*?)\n\s*\},\s*\n\}\s*as const", text, re.S)
    assert en, "could not isolate the `en` block in strings.ts"
    assert fr, "could not isolate the `fr` block in strings.ts"
    return _locale_keys(en.group(1)), _locale_keys(fr.group(1))


def test_en_fr_string_keys_are_at_parity() -> None:
    en_keys, fr_keys = _locale_blocks()
    assert en_keys == fr_keys, (
        "EN/FR string keys are not at parity (FE-8 / Law 25) — "
        f"EN-only={sorted(en_keys - fr_keys)}, FR-only={sorted(fr_keys - en_keys)}"
    )


def test_new_screen_keys_used_are_defined() -> None:
    """Every t.<key> referenced by the new screens must be defined (used ⊆ defined)."""
    en_keys, _ = _locale_blocks()
    used: set[str] = set()
    for f in _NEW_FILES:
        if not f.exists():
            continue
        # t.<key>, not preceded by an identifier char or '.' (so `it.x` / `x.t.y` don't match)
        used |= set(re.findall(r"(?<![A-Za-z0-9_.])t\.([A-Za-z][A-Za-z0-9_]*)", f.read_text()))
    undefined = used - en_keys
    assert not undefined, f"new screens reference undefined i18n keys: {sorted(undefined)}"


def test_new_screens_have_no_bare_user_facing_literals() -> None:
    """The new screen files must route user-facing text through strings.ts — no hardcoded
    placeholder/accessibilityLabel string literals and no literal JSX text nodes."""
    # placeholder="..." / accessibilityLabel="..." with a 2+ letter word (placeholder="0" ok)
    prop_re = re.compile(
        r"(?:placeholder|accessibilityLabel)\s*=\s*[\"']([^\"']*[A-Za-z]{2,}[^\"']*)[\"']"
    )
    # JSX text between > and < that is purely word-like (letters/space/sentence punctuation) —
    # excludes code like `> 0 ? Math.round(` (digits/operators) and `{expr}` (braces). The
    # (?<!=) drops arrow/return types like `=> Promise<unknown>` (the `=>` is not JSX text).
    jsx_text_re = re.compile(r"(?<!=)>\s*([A-Za-z][A-Za-z .,!?'-]*[A-Za-z])\s*<")

    offenders: list[str] = []
    for f in _NEW_FILES:
        if not f.exists():
            continue
        src = f.read_text(encoding="utf-8")
        for m in prop_re.finditer(src):
            offenders.append(f"{f.name}: bare prop literal {m.group(1)!r}")
        for m in jsx_text_re.finditer(src):
            offenders.append(f"{f.name}: bare JSX text {m.group(1)!r}")
    assert not offenders, "hardcoded user-facing literals (should use strings.ts):\n" + "\n".join(
        offenders
    )
