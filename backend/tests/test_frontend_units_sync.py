"""Cross-repo drift guard: the frontend unit picker must offer EXACTLY the backend's
canonical allowlist (Sprint 5 Phase 16, gate 36 — UI layer).

The picker (frontend/src/api/units.ts) is a hand-maintained TS constant; the backend
(app/modules/inventory/depletion/units.py + the 0014/0016 DB CHECK) is the source of truth.
"Client validation is UX; the DB CHECK + API 400 is enforcement" — but a drifted picker is a
pilot operator's mystery 400 (the UI offered a unit the API rejects). This test makes the
agreement a SUITE ASSERTION, not a comment: if either side changes alone, CI fails here —
the same move as the depletion guard's real-tree test (don't document that two things must
agree; assert it).
"""

from __future__ import annotations

import re
from pathlib import Path

from app.modules.inventory.depletion.units import CANONICAL_UNITS, DIMENSION_OF

_UNITS_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "api" / "units.ts"


def _by_dimension_body() -> str:
    text = _UNITS_TS.read_text(encoding="utf-8")
    m = re.search(r"CANONICAL_UNITS_BY_DIMENSION[^=]*=\s*\{(.*?)\};", text, re.S)
    assert m, "could not locate CANONICAL_UNITS_BY_DIMENSION in units.ts"
    return m.group(1)


def test_frontend_units_ts_matches_backend_canonical() -> None:
    """Set equality BOTH directions — an extra unit the backend rejects is drift exactly as
    much as a missing one (a subset check would miss it)."""
    assert _UNITS_TS.exists(), f"frontend units.ts not found at {_UNITS_TS}"
    # dimension keys are UNQUOTED, so single-quoted tokens are exactly the unit strings.
    frontend_units = set(re.findall(r"'([^']+)'", _by_dimension_body()))
    backend_units = set(CANONICAL_UNITS)
    assert frontend_units == backend_units, (
        "frontend units.ts drifted from backend CANONICAL_UNITS — "
        f"frontend-only={sorted(frontend_units - backend_units)}, "
        f"backend-only={sorted(backend_units - frontend_units)}"
    )


def test_frontend_unit_dimension_grouping_matches_backend() -> None:
    """The flat-list equality can't see a GROUPING bug — if 'g' were grouped under volume the
    set test still passes while the picker shows weight units in the wrong section. Assert each
    unit's group in units.ts equals its backend DIMENSION_OF entry."""
    body = _by_dimension_body()
    for dim in ("weight", "volume", "count"):
        grp = re.search(rf"{dim}:\s*\[([^\]]*)\]", body)
        assert grp, f"no {dim!r} group in units.ts"
        for unit in re.findall(r"'([^']+)'", grp.group(1)):
            assert DIMENSION_OF[unit] == dim, (
                f"units.ts groups {unit!r} under {dim!r} but backend DIMENSION_OF says "
                f"{DIMENSION_OF[unit]!r}"
            )
