"""Privacy sentinel — worker unhandled-error logs must be CONTENT-FREE.

WHY THIS EXISTS: the 2026-07-14 staging smoke test logged str(IntegrityError)
from the worker's catch-all, and SQLAlchemy/asyncpg embed the failing row and
bind parameters in the exception string — extracted invoice content landed in
platform logs, violating D-606-15. The unhandled path logs the class name only.
"""

from __future__ import annotations

from structlog.testing import capture_logs

from app.workers.receipt_extraction_worker import log_unhandled


def test_unhandled_log_carries_class_only_no_exception_body() -> None:
    exc = RuntimeError(
        "INSERT INTO receipt_lines ... [parameters: ('SENTINEL-INVOICE-LINE-TEXT', 1140)]"
    )
    with capture_logs() as logs:
        log_unhandled(exc)

    [event] = logs
    assert event["event"] == "receipt_extraction_worker.unhandled"
    assert event["error_class"] == "RuntimeError"
    flat = str(logs)
    assert "SENTINEL-INVOICE-LINE-TEXT" not in flat
    assert "parameters" not in flat
