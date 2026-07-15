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

from app.core.logging import configure_logging, get_logger
from app.modules.pos.worker import InboxWorker
from app.ops.env_check import check_env

log = get_logger(__name__)


async def _main() -> None:
    configure_logging()
    # Fail loudly on unsafe env BEFORE touching the queue (names only).
    if (os.environ.get("APP_ENV") or "").strip().lower() == "production":
        report = check_env("inbox_worker")
        if not report.ready:
            log.error("inbox_worker.env_not_ready", failures=report.failures)
            raise SystemExit(1)
        log.info("inbox_worker.env_ready")
    log.info("inbox_worker.starting")

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
