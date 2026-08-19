"""Reconciliation worker entry point.

Runs ReconciliationService.run_all() on a fixed interval (default 15 min).
Also runs token refresh and oauth_states cleanup on the same tick.

Usage:
  python -m app.workers.reconciliation_worker
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.modules.pos.reconciliation import ReconciliationService
from app.modules.pos.schedule import RECONCILIATION_INTERVAL_SECONDS
from app.modules.pos.token_refresh import refresh_expiring_tokens

log = get_logger(__name__)

# Re-exported for the runner; the canonical value lives in pos.schedule so
# insights freshness stays derived from the same cadence.
INTERVAL_SECONDS = RECONCILIATION_INTERVAL_SECONDS


async def _main() -> None:
    configure_logging()
    # Clover-less deployments: reconciliation and token refresh only serve the
    # Clover integration — exit cleanly (status 0) when it is off.
    if not get_settings().clover_enabled:
        log.info("reconciliation_worker.disabled", reason="CLOVER_ENABLED is not true")
        return
    # Fail loudly on unsafe env BEFORE touching POS state (names only) — the same
    # boot gate every other worker has (this one was historically missing).
    if (os.environ.get("APP_ENV") or "").strip().lower() == "production":
        from app.ops.env_check import check_env

        report = check_env("reconciliation_worker")
        if not report.ready:
            log.error("reconciliation_worker.env_not_ready", failures=report.failures)
            raise SystemExit(1)
        log.info("reconciliation_worker.env_ready")
    # Gated on RESTRICTED_RUNTIME_ROLES_ENABLED (not APP_ENV): fail-closed
    # service_worker assertion when the cutover flag is on; no-op otherwise.
    from app.core.rls_assert import assert_service_pool_role_if_enabled

    # Asserted role travels IN the `.starting` record — consumed by scripts/retire_verify.py.
    service_user = await assert_service_pool_role_if_enabled()
    log.info(
        "reconciliation_worker.starting",
        interval_s=INTERVAL_SECONDS,
        source_commit=os.environ.get("SOURCE_COMMIT", "unknown"),
        service_user=service_user,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        log.info("reconciliation_worker.shutdown_requested")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)

    service = ReconciliationService()

    while not stop_event.is_set():
        try:
            log.info("reconciliation_worker.tick_start")
            await refresh_expiring_tokens()
            await service.run_all()
            log.info("reconciliation_worker.tick_done")
        except Exception as exc:
            log.error("reconciliation_worker.tick_error", error=str(exc)[:500])

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=INTERVAL_SECONDS)

    log.info("reconciliation_worker.stopped")


if __name__ == "__main__":
    asyncio.run(_main())
