"""Inbound email fan-out worker entry point (Sprint 6 Phase 3b).

Runs the InboundEmailWorker claim-process loop indefinitely. Single DigitalOcean
App Platform Worker component. Requires SERVICE_DATABASE_URL (service_worker);
no Spaces or Anthropic access — the webhook uploaded the bytes, the extraction
worker downloads them.

Usage:
  python -m app.workers.inbound_email_worker
"""

from __future__ import annotations

import asyncio
import os
import signal

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.modules.receipts.inbound_email_worker import InboundEmailWorker
from app.ops.env_check import check_env

log = get_logger(__name__)


def log_unhandled(exc: Exception) -> None:
    """Class only (D-606-15) — driver exception strings embed row content."""
    log.error("inbound_email_worker.unhandled", error_class=type(exc).__name__)


async def _main() -> None:
    configure_logging()
    # Postmark-less deployments: this worker exists solely to fan out Postmark
    # inbox rows — exit cleanly (status 0) when the channel is off (Clover
    # inbox_worker pattern; Gmail will ship its own sync worker).
    if not get_settings().postmark_inbound_enabled:
        log.info("inbound_email_worker.disabled", reason="POSTMARK_INBOUND_ENABLED is not true")
        return
    if (os.environ.get("APP_ENV") or "").strip().lower() == "production":
        report = check_env("inbound_email_worker")
        if not report.ready:
            log.error("inbound_email_worker.env_not_ready", failures=report.failures)
            raise SystemExit(1)
        log.info("inbound_email_worker.env_ready")
    # Gated on RESTRICTED_RUNTIME_ROLES_ENABLED (not APP_ENV): fail-closed
    # service_worker assertion when the cutover flag is on; no-op otherwise.
    from app.core.rls_assert import assert_service_pool_role_if_enabled

    # Asserted role travels IN the `.starting` record — consumed by scripts/retire_verify.py.
    service_user = await assert_service_pool_role_if_enabled()

    worker = InboundEmailWorker()
    log.info(
        "inbound_email_worker.starting",
        source_commit=os.environ.get("SOURCE_COMMIT", "unknown"),
        service_user=service_user,
    )
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        log.info("inbound_email_worker.shutdown_requested")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)

    while not stop_event.is_set():
        try:
            did_work = await worker.process_once()
        except Exception as exc:  # never let one row kill the loop
            log_unhandled(exc)
            did_work = False
        if not did_work:
            await asyncio.sleep(2)

    log.info("inbound_email_worker.stopped")


if __name__ == "__main__":
    asyncio.run(_main())
