"""Single source of truth for the POS reconciliation schedule.

Both the reconciliation worker (how often it polls) and Stock Insights (how it
judges reconciliation freshness) derive their timing from here, so changing the
cadence can never silently leave insights using a stale threshold.
"""

from __future__ import annotations

# How often the reconciliation worker polls each active connection.
RECONCILIATION_INTERVAL_SECONDS = 900  # 15 minutes

# Grace multiple before a last-run timestamp is considered 'stale' by insights.
RECONCILIATION_STALE_GRACE_INTERVALS = 3  # 3 missed cycles ⇒ 45 min

# Derived: a reconciliation older than this is 'stale', not 'recent'.
RECONCILIATION_STALE_AFTER_SECONDS = (
    RECONCILIATION_INTERVAL_SECONDS * RECONCILIATION_STALE_GRACE_INTERVALS
)
