"""Regression test against a REAL captured Clover sandbox order payload.

tests/fixtures/clover_real_order.json is the actual `fetched_payload` from a real
Clover sandbox cash sale (order 3W9V62MQ043B2) captured during V7 live certification
on staging. It locks the real-Clover order shape as a PERMANENT parse target, so future
parser changes are tested against reality — not the hand-authored shapes the sims assume.

Skips until the fixture is captured (keeps the suite green pre-capture). Once the JSON
is dropped in, these assertions run against the genuine payload.

What this guards (the fields the worker/_derive_payment_state actually depend on):
  * top-level state == 'locked'                       (worker.py:217 gate)
  * top-level paymentState == 'PAID'                  (_derive_payment_state step 1)
  * lineItems.elements[] is a non-empty list          (line iteration)
  * each line has id + item.id                         (pos_item_id mapping)
  * payments.elements[] is a list when present         (derivation fallback path)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.modules.pos.worker import InboxWorker

_FIXTURE = Path(__file__).parent / "fixtures" / "clover_real_order.json"

pytestmark = pytest.mark.skipif(
    not _FIXTURE.exists(),
    reason="real Clover payload not captured yet — drop it at tests/fixtures/clover_real_order.json",
)


def _payload() -> dict:
    return json.loads(_FIXTURE.read_text())


def test_real_payload_state_is_locked() -> None:
    assert (_payload().get("state") or "").lower() == "locked"


def test_real_payload_derives_paid() -> None:
    # The real order carries top-level paymentState=PAID, so the parser's first
    # derivation branch hits directly — no fallback needed. If Clover ever stops
    # sending it, this catches the regression.
    assert InboxWorker()._derive_payment_state(_payload()) == "PAID"


def test_real_payload_line_items_shape() -> None:
    lines = (_payload().get("lineItems") or {}).get("elements") or []
    assert lines, "lineItems.elements must be a non-empty list"
    for li in lines:
        assert li.get("id"), "each line item needs an id"
        assert (li.get("item") or {}).get("id"), "each line needs item.id for pos_item_id mapping"


def test_real_payload_payments_shape() -> None:
    payments = (_payload().get("payments") or {}).get("elements")
    assert payments is None or isinstance(payments, list)
