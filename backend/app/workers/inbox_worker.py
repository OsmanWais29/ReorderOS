"""Inbox worker entry point.

Runs the InboxWorker claim-process loop indefinitely.
Designed to run as a single process on DigitalOcean App Platform
(Worker component, one instance in Sprint 4).

Usage:
  python -m app.workers.inbox_worker
"""

from __future__ import annotations

import asyncio
import os
import signal

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.modules.pos.worker import InboxWorker
from app.ops.env_check import check_env

log = get_logger(__name__)


async def _main() -> None:
    configure_logging()
    # Clover-less deployments: the inbox worker exists solely to drain the
    # Clover event inbox — exit cleanly (status 0) when the integration is off.
    if not get_settings().clover_enabled:
        log.info("inbox_worker.disabled", reason="CLOVER_ENABLED is not true")
        return
    # Fail loudly on unsafe env BEFORE touching the queue (names only).
    if (os.environ.get("APP_ENV") or "").strip().lower() == "production":
        report = check_env("inbox_worker")
        if not report.ready:
            log.error("inbox_worker.env_not_ready", failures=report.failures)
            raise SystemExit(1)
        log.info("inbox_worker.env_ready")
    # Gated on RESTRICTED_RUNTIME_ROLES_ENABLED (not APP_ENV): fail-closed
    # service_worker assertion when the cutover flag is on; no-op otherwise.
    from app.core.rls_assert import assert_service_pool_role_if_enabled

    # The ASSERTED role is logged in the `.starting` record itself: the retirement gate
    # (scripts/retire_verify.py) accepts only a record carrying service_user == the
    # expected role — source-code ordering alone is not evidence.
    service_user = await assert_service_pool_role_if_enabled()
    log.info(
        "inbox_worker.starting",
        source_commit=os.environ.get("SOURCE_COMMIT", "unknown"),
        service_user=service_user,
    )

    worker = InboxWorker()
    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        log.info("inbox_worker.shutdown_requested")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)

    async def _run_until_stop() -> None:
        while not stop_event.is_set():
            events = await worker.claim_batch(batch_size=10)
            if not events:
                await asyncio.sleep(2)
                continue
            for event in events:
                if stop_event.is_set():
                    break
                try:
                    await worker.process_event(event)
                except Exception as exc:
                    await worker.mark_failed(event, f"Unhandled: {type(exc).__name__}: {exc!s}")

    await _run_until_stop()
    log.info("inbox_worker.stopped")


if __name__ == "__main__":
    asyncio.run(_main())
