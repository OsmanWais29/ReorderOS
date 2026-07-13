"""Receipt extraction worker entry point (Sprint 6 S3).

Runs the ExtractionWorker claim-process loop indefinitely. Single DigitalOcean App
Platform Worker component. Requires ANTHROPIC_API_KEY (the concrete client) and the
Spaces + SERVICE_DATABASE_URL (service_worker) env.

Usage:
  python -m app.workers.receipt_extraction_worker
"""

from __future__ import annotations

import asyncio
import signal

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.modules.receipts.extraction_llm import AnthropicExtractionClient
from app.modules.receipts.extraction_worker import ExtractionWorker

log = get_logger(__name__)


async def _main() -> None:
    configure_logging()
    settings = get_settings()
    if not settings.anthropic_api_key:
        log.error("receipt_extraction_worker.no_api_key")
        raise SystemExit(1)

    # Boot-time RUNTIME readiness — booleans only, never values (D-606-15). The worker
    # has no HTTP surface, so this log is the only way to confirm its OWN process env
    # injected (the api's /health/storage can't see the worker process). Mirrors the
    # storage fields extraction needs: a False here means a job will fail at download.
    log.info(
        "receipt_extraction_worker.storage_readiness",
        endpoint=bool(settings.spaces_endpoint),
        region=bool(settings.spaces_region),
        bucket=bool(settings.spaces_bucket),
        key=bool(settings.spaces_key),
        secret=bool(settings.spaces_secret),
        anthropic_key=bool(settings.anthropic_api_key),
        service_db=bool(settings.service_database_url),
        configured=all(
            (
                settings.spaces_endpoint,
                settings.spaces_region,
                settings.spaces_bucket,
                settings.spaces_key,
                settings.spaces_secret,
            )
        ),
    )

    worker = ExtractionWorker(
        AnthropicExtractionClient(settings.anthropic_api_key, settings.anthropic_model)
    )
    log.info("receipt_extraction_worker.starting")
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        log.info("receipt_extraction_worker.shutdown_requested")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)

    while not stop_event.is_set():
        try:
            did_work = await worker.process_once()
        except Exception as exc:  # never let one job kill the loop
            log.error("receipt_extraction_worker.unhandled", error=f"{type(exc).__name__}: {exc!s}")
            did_work = False
        if not did_work:
            await asyncio.sleep(2)

    log.info("receipt_extraction_worker.stopped")


if __name__ == "__main__":
    asyncio.run(_main())
