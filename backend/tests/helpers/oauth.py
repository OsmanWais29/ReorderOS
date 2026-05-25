"""Helpers for Phase 4 tests: oauth_states row factory."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta


def make_oauth_state_row(overrides: dict | None = None) -> dict:
    defaults = {
        "state": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "expires_at": datetime.now(UTC) + timedelta(minutes=10),
    }
    if overrides:
        defaults.update(overrides)
    return defaults
