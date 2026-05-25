"""Reconciliation worker entry point.

Runs ReconciliationService.run_all() on a fixed interval (default 15 min).
Also runs token refresh and oauth_states cleanup on the same tick.

Usage:
  python -m app.workers.reconciliation_worker
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.core.logging import configure_logging, get_logger
from app.modules.pos.reconciliation import ReconciliationService
from app.modules.pos.token_refresh import refresh_expiring_tokens

log = get_logger(__name__)

INTERVAL_SECONDS = 900  # 15 minutes


async def _main() -> None:
    configure_logging()
    log.info("reconciliation_worker.starting", interval_s=INTERVAL_SECONDS)

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
