"""Inbox worker entry point.

Runs the InboxWorker claim-process loop indefinitely.
Designed to run as a single process on DigitalOcean App Platform
(Worker component, one instance in Sprint 4).

Usage:
  python -m app.workers.inbox_worker
"""

from __future__ import annotations

import asyncio
import signal

from app.core.logging import configure_logging, get_logger
from app.modules.pos.worker import InboxWorker

log = get_logger(__name__)


async def _main() -> None:
    configure_logging()
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
                except Exception as exc:  # noqa: BLE001
                    await worker.mark_failed(
                        event, f"Unhandled: {type(exc).__name__}: {exc!s}"
                    )

    await _run_until_stop()
    log.info("inbox_worker.stopped")


if __name__ == "__main__":
    asyncio.run(_main())
